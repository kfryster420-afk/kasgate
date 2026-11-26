import { parseUsdToMicros } from "./format";
import { formatKas, formatSompi, meetsOutputFloor, minPriceSompi, parseKasToSompi } from "./sompi";
import { HTTP_METHODS, type HttpMethod, type OpenApiImportSelection, type OpenApiOperationPreview, type PriceCurrency } from "./types";
import { checkUpstreamUrl, type Check } from "./validation";

/** Mirrors schemas.OPENAPI_MAX_DOCUMENT_BYTES (2 MiB): the largest OpenAPI document the gateway imports. */
export const OPENAPI_MAX_DOCUMENT_BYTES = 2 * 1024 * 1024;
/** Mirrors schemas.OPENAPI_MAX_OPERATIONS. */
export const OPENAPI_MAX_OPERATIONS = 500;
/** Mirrors schemas.OPENAPI_MAX_PATH_LENGTH. */
export const OPENAPI_MAX_PATH_LENGTH = 512;
/** Request-body cap for POST /api/endpoints/import: a JSON-escaped 2 MiB document plus the other fields. */
export const OPENAPI_MAX_REQUEST_BYTES = 2 * OPENAPI_MAX_DOCUMENT_BYTES + 256 * 1024;

export const OPENAPI_FILE_ACCEPT = ".json,.yaml,.yml,application/json,application/yaml,application/x-yaml,text/yaml";
const FILE_EXTENSION_RE = /\.(json|ya?ml)$/i;

export function utf8ByteLength(text: string): number {
  return new TextEncoder().encode(text).length;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

const TOO_LARGE = `The document is larger than ${formatBytes(OPENAPI_MAX_DOCUMENT_BYTES)}`;

export function checkSpecText(input: unknown): Check<string> {
  if (typeof input !== "string" || input.trim() === "") return fail("Paste or upload an OpenAPI 3.0 or 3.1 document");
  if (utf8ByteLength(input) > OPENAPI_MAX_DOCUMENT_BYTES) return fail(TOO_LARGE);
  return ok(input);
}

/** Name and size check before the browser reads an uploaded file. */
export function checkSpecFile(file: { name: string; size: number }): Check<null> {
  if (!FILE_EXTENSION_RE.test(file.name)) return fail("Choose a .json, .yaml or .yml file");
  if (file.size === 0) return fail("The file is empty");
  if (file.size > OPENAPI_MAX_DOCUMENT_BYTES) return fail(TOO_LARGE);
  return ok(null);
}

/** Same public-host rules as an upstream URL; the gateway re-checks the URL and every redirect when it fetches. */
export function checkSpecUrl(input: unknown): Check<string> {
  const result = checkUpstreamUrl(input);
  if (!result.ok) return fail(result.error.replace(/^Upstream URL/, "Document URL"));
  return result;
}

export function isHttpMethod(value: string): value is HttpMethod {
  return (HTTP_METHODS as readonly string[]).includes(value);
}

/** Inline per-row error for a price input in the import currency, or null when the value is valid. */
export function rowPriceError(input: string, currency: PriceCurrency, minOutput: bigint): string | null {
  if (currency === "USD") {
    return parseUsdToMicros(input) === null ? "Enter a USD amount (up to 6 decimals)" : null;
  }
  const sompi = parseKasToSompi(input);
  if (sompi === null) return "Enter a KAS amount (up to 8 decimals)";
  if (!meetsOutputFloor(sompi, minOutput)) {
    const floorPrice = minPriceSompi(minOutput);
    return `Minimum ${formatKas(floorPrice)} KAS (${formatSompi(floorPrice)} sompi)`;
  }
  return null;
}

export interface ImportRowState {
  selected: boolean;
  priceInput: string;
}

/**
 * Selections for mode "create" from the preview rows and their UI state. A per-row override is sent only when the
 * row's price differs from the default; returns null when any selected row has an unparsable price.
 */
export function buildSelections(
  operations: readonly OpenApiOperationPreview[],
  rows: Readonly<Record<string, ImportRowState>>,
  currency: PriceCurrency,
  defaultAmount: bigint,
): OpenApiImportSelection[] | null {
  const out: OpenApiImportSelection[] = [];
  for (const operation of operations) {
    const row = rows[operation.key];
    if (!operation.importable || !row?.selected || !isHttpMethod(operation.method)) continue;
    const amount = currency === "USD" ? parseUsdToMicros(row.priceInput) : parseKasToSompi(row.priceInput);
    if (amount === null) return null;
    const selection: OpenApiImportSelection = { method: operation.method, path: operation.path };
    if (amount !== defaultAmount) {
      if (currency === "USD") selection.price_usd_micros = amount.toString();
      else selection.price_sompi = amount.toString();
    }
    out.push(selection);
  }
  return out;
}

const METHOD_TONES: Record<string, string> = {
  GET: "border-emerald-800 bg-emerald-950/50 text-emerald-300",
  POST: "border-sky-800 bg-sky-950/50 text-sky-300",
  PUT: "border-amber-800 bg-amber-950/50 text-amber-300",
  PATCH: "border-violet-800 bg-violet-950/50 text-violet-300",
  DELETE: "border-red-900 bg-red-950/50 text-red-300",
};

export function methodTone(method: string): string {
  return METHOD_TONES[method] ?? "border-zinc-700 bg-zinc-900 text-zinc-500";
}

/** `{petId}` -> `PET_ID`: a shell variable name for a path parameter in copyable snippets. */
export function paramVariable(name: string): string {
  const snake = name
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .replace(/[^A-Za-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .toUpperCase();
  const safe = snake === "" ? "PARAM" : snake;
  return /^[0-9]/.test(safe) ? `P_${safe}` : safe;
}

/** Proxy URL with the path-parameter suffix filled by shell variables, e.g. ".../p/show-pet/$PET_ID". */
export function exampleCallUrl(proxyUrl: string, proxyPath: string | null): { url: string; variables: string[] } {
  if (!proxyPath) return { url: proxyUrl, variables: [] };
  const slugPrefix = /^\/p\/[^/]+/.exec(proxyPath)?.[0] ?? "";
  const suffix = proxyPath.slice(slugPrefix.length);
  const variables: string[] = [];
  const filled = suffix.replace(/\{([^{}/]+)\}/g, (_match, name: string) => {
    const variable = paramVariable(name);
    if (!variables.includes(variable)) variables.push(variable);
    return `$${variable}`;
  });
  return { url: `${proxyUrl}${filled}`, variables };
}

function ok<T>(value: T): Check<T> {
  return { ok: true, value };
}

function fail<T>(error: string): Check<T> {
  return { ok: false, error };
}
