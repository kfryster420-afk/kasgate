"""One-click OpenAPI import: load, validate and flatten an OpenAPI 3.0/3.1 document into importable operations.

Parsing is deliberately defensive: documents are untrusted merchant input (or fetched from a merchant-supplied URL).
- Size: at most OPENAPI_MAX_DOCUMENT_BYTES, enforced on the text and while streaming a URL fetch.
- YAML: libyaml/PyYAML *safe* loading only, after an event pre-pass that caps nesting depth and alias count, so
  alias bombs and stack-exhausting nesting are refused before any object is built. Recursive aliases are refused.
- `$ref` / `$dynamicRef`: only local `#...` references are accepted; nothing is ever resolved over the network.
- Remote documents are fetched through the gateway's guarded upstream client (DNS resolved and pinned to a public
  IP on every connection), with redirects followed manually and every hop re-validated.
"""

import asyncio
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import httpx
import yaml
from pydantic import HttpUrl, TypeAdapter, ValidationError

from app.schemas import (
    OPENAPI_MAX_DOCUMENT_BYTES,
    OPENAPI_MAX_OPERATIONS,
    OPENAPI_MAX_PATH_LENGTH,
    OPENAPI_PREVIEW_TEXT_LENGTH,
    SLUG_RE,
    _reject_private_host,
)

OPENAPI_METHODS: Final = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
ALLOWED_METHODS: Final = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
SUPPORTED_VERSION_RE: Final = re.compile(r"^3\.[01](?:\.\d+)?(?:-[0-9A-Za-z.-]+)?$")
MAX_NESTING_DEPTH: Final = 100
MAX_YAML_ALIASES: Final = 1000
MAX_NODES: Final = 2_000_000
MAX_REDIRECTS: Final = 3
FETCH_TIMEOUT_SECONDS: Final = 10.0
SLUG_MAX_LENGTH: Final = 64
NAME_MAX_LENGTH: Final = 120
DESCRIPTION_MAX_LENGTH: Final = 2000
CATEGORY_MAX_LENGTH: Final = 60
MAX_SERVERS: Final = 5
SERVER_URL_MAX_LENGTH: Final = 300
REF_KEYS: Final = frozenset({"$ref", "$dynamicRef"})
PATH_SEGMENT_SAFE: Final = "/:@!$&'()*+,;=-._~%"

_SLUG_PATTERN = re.compile(SLUG_RE)
_NON_SLUG_CHARS = re.compile(r"[^a-z0-9]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_PARAM_RE = re.compile(r"\{([^{}/]+)\}")
_HTTP_URL = TypeAdapter(HttpUrl)


class OpenApiImportError(Exception):
    """A document or URL the importer refuses. `status_code` is 413 for size limits, otherwise 422."""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def truncate(value: str, limit: int = OPENAPI_PREVIEW_TEXT_LENGTH) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "\u2026"


def _optional_text(value: object, limit: int = OPENAPI_PREVIEW_TEXT_LENGTH) -> str | None:
    if isinstance(value, str) and value.strip():
        return truncate(value, limit)
    return None


# ---------------------------------------------------------------- loading


def _check_yaml_events(text: str) -> None:
    """Walk the YAML event stream (no objects built) and refuse deep nesting and alias-heavy documents."""
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    depth = 0
    aliases = 0
    documents = 0
    for event in yaml.parse(text, Loader=loader):
        if isinstance(event, yaml.DocumentStartEvent):
            documents += 1
            if documents > 1:
                raise OpenApiImportError("the YAML stream must contain exactly one document")
        elif isinstance(event, yaml.MappingStartEvent | yaml.SequenceStartEvent):
            depth += 1
            if depth > MAX_NESTING_DEPTH:
                raise OpenApiImportError(f"the document nests deeper than {MAX_NESTING_DEPTH} levels")
        elif isinstance(event, yaml.MappingEndEvent | yaml.SequenceEndEvent):
            depth -= 1
        elif isinstance(event, yaml.AliasEvent):
            aliases += 1
            if aliases > MAX_YAML_ALIASES:
                raise OpenApiImportError(f"the YAML document uses more than {MAX_YAML_ALIASES} aliases")


def _load_yaml(text: str) -> object:
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    _check_yaml_events(text)
    return yaml.load(text, Loader=loader)  # noqa: S506 - always a SafeLoader (CSafeLoader or SafeLoader)


def _scan(document: object) -> None:
    """Depth, expanded-size and cycle checks over the loaded tree, and refusal of any non-local `$ref`.

    Shared (aliased) nodes are visited once, but their size counts at every place they are used, so a YAML alias
    bomb that would expand to billions of nodes is refused in linear time without ever being expanded.
    """
    sizes: dict[int, int] = {}
    on_path: set[int] = set()

    def visit(node: object, depth: int) -> int:
        if depth > MAX_NESTING_DEPTH:
            raise OpenApiImportError(f"the document nests deeper than {MAX_NESTING_DEPTH} levels")
        if not isinstance(node, dict | list):
            return 1
        ident = id(node)
        if ident in on_path:
            raise OpenApiImportError("the document contains a recursive YAML alias")
        if ident in sizes:
            return sizes[ident]
        on_path.add(ident)
        total = 1
        if isinstance(node, dict):
            for key, value in node.items():
                if key in REF_KEYS and isinstance(value, str) and not value.startswith("#"):
                    raise OpenApiImportError(
                        f"only local '#/...' references are allowed; found {key}: {truncate(value, 120)!r}"
                    )
                total += visit(value, depth + 1)
                if total > MAX_NODES:
                    raise OpenApiImportError(f"the document expands to more than {MAX_NODES} nodes")
        else:
            for value in node:
                total += visit(value, depth + 1)
                if total > MAX_NODES:
                    raise OpenApiImportError(f"the document expands to more than {MAX_NODES} nodes")
        on_path.discard(ident)
        sizes[ident] = total
        return total

    visit(document, 0)


def load_document(text: str) -> dict[str, Any]:
    """Parse JSON or YAML (detected by content), then validate the OpenAPI envelope. Raises OpenApiImportError."""
    if len(text.encode("utf-8", errors="surrogatepass")) > OPENAPI_MAX_DOCUMENT_BYTES:
        raise OpenApiImportError(f"the document is larger than {OPENAPI_MAX_DOCUMENT_BYTES} bytes", status_code=413)
    body = text.lstrip("\ufeff").strip()
    if not body:
        raise OpenApiImportError("the document is empty")
    try:
        if body[0] in "{[":
            try:
                document: object = json.loads(body)
            except json.JSONDecodeError as exc:
                raise OpenApiImportError(f"invalid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}") from exc
        else:
            try:
                document = _load_yaml(body)
            except yaml.YAMLError as exc:
                mark = getattr(exc, "problem_mark", None)
                where = f" at line {mark.line + 1} column {mark.column + 1}" if mark is not None else ""
                problem = getattr(exc, "problem", None) or "could not parse"
                raise OpenApiImportError(f"invalid YAML: {problem}{where}") from exc
    except RecursionError as exc:
        raise OpenApiImportError(f"the document nests deeper than {MAX_NESTING_DEPTH} levels") from exc
    if not isinstance(document, dict):
        raise OpenApiImportError("the document must be a JSON/YAML object (an OpenAPI document)")
    _scan(document)
    if "swagger" in document and "openapi" not in document:
        raise OpenApiImportError(
            "Swagger 2.0 documents are not supported; convert the document to OpenAPI 3.0 or 3.1 first"
        )
    version = document.get("openapi")
    if isinstance(version, int | float) and not isinstance(version, bool):
        version = str(version)
    if not isinstance(version, str) or not SUPPORTED_VERSION_RE.fullmatch(version.strip()):
        shown = truncate(str(version), 40) if version is not None else "missing"
        raise OpenApiImportError(f"unsupported OpenAPI version ({shown}); only 3.0.x and 3.1.x are supported")
    document["openapi"] = version.strip()
    paths = document.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise OpenApiImportError("the document has no 'paths' object with operations")
    return document


# ---------------------------------------------------------------- operations


@dataclass(frozen=True)
class ParsedOperation:
    method: str
    path: str
    operation_id: str | None
    summary: str | None
    description: str | None
    tag: str | None
    path_params: tuple[str, ...]
    skip_reason: str | None

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"


def _resolve_local(document: dict[str, Any], ref: str) -> object:
    if not ref.startswith("#/"):
        raise OpenApiImportError(f"unsupported path item reference {truncate(ref, 120)!r}")
    node: object = document
    for raw in ref[2:].split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            raise OpenApiImportError(f"path item reference {truncate(ref, 120)!r} does not resolve")
        node = node[part]
    return node


def _path_item(document: dict[str, Any], item: object) -> dict[str, Any] | None:
    hops = 0
    while isinstance(item, dict) and isinstance(item.get("$ref"), str):
        hops += 1
        if hops > 8:
            raise OpenApiImportError("path item references nest too deeply")
        item = _resolve_local(document, item["$ref"])
    return item if isinstance(item, dict) else None


def _skip_reason_for_path(path: str) -> str | None:
    if len(path) > OPENAPI_MAX_PATH_LENGTH:
        return f"path is longer than {OPENAPI_MAX_PATH_LENGTH} characters"
    if not path.startswith("/"):
        return "path must start with '/'"
    if any(ch in path for ch in "?#\\\x00") or any(ch.isspace() for ch in path):
        return "path contains characters that cannot be routed"
    segments = path.split("/")
    if any(segment in {".", ".."} for segment in segments):
        return "path contains '.' or '..' segments"
    if path.count("{") != path.count("}") or "{}" in path:
        return "path has malformed {parameter} templates"
    return None


def iter_operations(document: dict[str, Any]) -> Iterator[ParsedOperation]:
    paths: dict[Any, Any] = document["paths"]
    count = 0
    for raw_path, raw_item in paths.items():
        if not isinstance(raw_path, str):
            continue
        item = _path_item(document, raw_item)
        if item is None:
            continue
        for method_name in OPENAPI_METHODS:
            operation = item.get(method_name)
            if not isinstance(operation, dict):
                continue
            count += 1
            if count > OPENAPI_MAX_OPERATIONS:
                raise OpenApiImportError(f"the document has more than {OPENAPI_MAX_OPERATIONS} operations")
            method = method_name.upper()
            skip = _skip_reason_for_path(raw_path)
            if skip is None and method not in ALLOWED_METHODS:
                skip = f"method {method} is not supported (allowed: {', '.join(sorted(ALLOWED_METHODS))})"
            tags = operation.get("tags")
            tag = tags[0] if isinstance(tags, list) and tags and isinstance(tags[0], str) else None
            yield ParsedOperation(
                method=method,
                path=raw_path if len(raw_path) <= OPENAPI_MAX_PATH_LENGTH else truncate(raw_path, 120),
                operation_id=_optional_text(operation.get("operationId")),
                summary=_optional_text(operation.get("summary")),
                description=_optional_text(operation.get("description"), DESCRIPTION_MAX_LENGTH),
                tag=_optional_text(tag, CATEGORY_MAX_LENGTH),
                path_params=tuple(_PARAM_RE.findall(raw_path)) if skip is None else (),
                skip_reason=skip,
            )


def parse_operations(document: dict[str, Any]) -> list[ParsedOperation]:
    operations = list(iter_operations(document))
    if not operations:
        raise OpenApiImportError("the document has no operations under 'paths'")
    return operations


def document_servers(document: dict[str, Any]) -> list[str]:
    servers = document.get("servers")
    found: list[str] = []
    if isinstance(servers, list):
        for server in servers:
            url = server.get("url") if isinstance(server, dict) else None
            if isinstance(url, str) and url.strip() and len(url) <= SERVER_URL_MAX_LENGTH:
                found.append(url.strip())
            if len(found) >= MAX_SERVERS:
                break
    return found


def document_info(document: dict[str, Any]) -> tuple[str | None, str | None]:
    info = document.get("info")
    if not isinstance(info, dict):
        return None, None
    version = info.get("version")
    return _optional_text(info.get("title")), _optional_text(version if isinstance(version, str) else None, 60)


# ---------------------------------------------------------------- mapping onto KasGate endpoints


def static_prefix(path: str) -> str:
    """Path up to (not including) the first segment that contains a {parameter}; "" when the first one does.

    A path without parameters is kept exactly (including a trailing slash); "/" maps to the base URL itself.
    """
    if "{" not in path:
        return "" if path == "/" else path
    kept: list[str] = []
    for segment in path.split("/")[1:]:
        if "{" in segment:
            break
        kept.append(segment)
    prefix = "/".join(kept).rstrip("/")
    return "/" + prefix if prefix else ""


def proxy_suffix(path: str) -> str:
    """The part of the operation path the buyer appends after /p/{slug}, keeping the {param} placeholders."""
    if "{" not in path:
        return ""
    rest = path[len(static_prefix(path)) :].rstrip("/")
    return rest if rest.startswith("/") else "/" + rest


def upstream_url_for(base_url: str, path: str) -> str:
    """Base upstream URL + the static path prefix; the proxy appends the buyer's path suffix at request time."""
    parts = urlsplit(base_url)
    prefix = quote(static_prefix(path), safe=PATH_SEGMENT_SAFE)
    joined = (parts.path.rstrip("/") + prefix) if prefix else (parts.path or "/")
    return urlunsplit((parts.scheme, parts.netloc, joined, parts.query, ""))


def slug_base(operation: ParsedOperation) -> str:
    source = operation.operation_id or f"{operation.method} {operation.path}"
    source = _CAMEL_BOUNDARY.sub("-", source)
    base = _NON_SLUG_CHARS.sub("-", source.lower()).strip("-")[:SLUG_MAX_LENGTH].strip("-")
    if len(base) < 3:
        base = f"{operation.method.lower()}-{base}".strip("-") if base else operation.method.lower()
    if len(base) < 3:
        base = f"{base}-api"
    return base


def allocate_slug(base: str, taken: set[str]) -> str:
    """`base`, or `base-2`, `base-3`, ... (trimmed to the 64-char slug limit) - the first one not in `taken`."""
    if base not in taken and _SLUG_PATTERN.fullmatch(base):
        return base
    counter = 2
    while True:
        suffix = f"-{counter}"
        candidate = base[: SLUG_MAX_LENGTH - len(suffix)].rstrip("-") + suffix
        if candidate not in taken and _SLUG_PATTERN.fullmatch(candidate):
            return candidate
        counter += 1


def endpoint_name(operation: ParsedOperation) -> str:
    name = operation.summary or operation.operation_id or f"{operation.method} {operation.path}"
    return truncate(name, NAME_MAX_LENGTH)


# ---------------------------------------------------------------- fetching


def _validated_fetch_url(raw: str) -> str:
    try:
        url = _reject_private_host(_HTTP_URL.validate_python(raw))
    except (ValidationError, ValueError) as exc:
        message = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
        raise OpenApiImportError(f"spec_url is not allowed: {message}") from exc
    if url.scheme not in {"http", "https"}:
        raise OpenApiImportError("spec_url must use http or https")
    return str(url)


async def fetch_document(client: httpx.AsyncClient, spec_url: str) -> str:
    """GET the document through the guarded upstream client; redirects are re-validated hop by hop."""
    url = _validated_fetch_url(spec_url)
    try:
        async with asyncio.timeout(FETCH_TIMEOUT_SECONDS):
            for _ in range(MAX_REDIRECTS + 1):
                request = client.build_request(
                    "GET",
                    url,
                    headers={
                        "Accept": "application/json, application/yaml, application/x-yaml, text/yaml, text/plain, */*",
                        "User-Agent": "KasGate-OpenAPI-Import/1",
                    },
                )
                response = await client.send(request, stream=True, follow_redirects=False)
                try:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise OpenApiImportError("spec_url redirected without a Location header")
                        url = _validated_fetch_url(urljoin(url, location))
                        continue
                    if response.status_code != 200:
                        raise OpenApiImportError(f"fetching spec_url returned HTTP {response.status_code}")
                    declared = response.headers.get("content-length")
                    if declared is not None and declared.isdigit() and int(declared) > OPENAPI_MAX_DOCUMENT_BYTES:
                        raise OpenApiImportError(
                            f"the document is larger than {OPENAPI_MAX_DOCUMENT_BYTES} bytes", status_code=413
                        )
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > OPENAPI_MAX_DOCUMENT_BYTES:
                            raise OpenApiImportError(
                                f"the document is larger than {OPENAPI_MAX_DOCUMENT_BYTES} bytes", status_code=413
                            )
                        chunks.append(chunk)
                finally:
                    await response.aclose()
                try:
                    return b"".join(chunks).decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise OpenApiImportError("the fetched document is not UTF-8 text") from exc
            raise OpenApiImportError(f"spec_url redirected more than {MAX_REDIRECTS} times")
    except TimeoutError as exc:
        raise OpenApiImportError("fetching spec_url timed out") from exc
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise OpenApiImportError(
            f"spec_url could not be fetched ({type(exc).__name__}): {truncate(str(exc), 160)}"
        ) from exc
