"use client";

import { ArrowLeft, CheckCircle2, FileCode2, FileUp, Link2, Loader2, Server, X } from "lucide-react";
import { useCallback, useId, useMemo, useState, type ChangeEvent, type DragEvent, type FormEvent } from "react";
import { ApiError, apiFetch, errorMessage } from "@/lib/api-client";
import {
  OPENAPI_FILE_ACCEPT,
  OPENAPI_MAX_DOCUMENT_BYTES,
  buildSelections,
  checkSpecFile,
  checkSpecText,
  checkSpecUrl,
  exampleCallUrl,
  formatBytes,
  methodTone,
  rowPriceError,
  type ImportRowState,
} from "@/lib/openapi-import";
import { curlImported } from "@/lib/snippets";
import { formatKas, formatSompi, meetsOutputFloor, minPriceReason, parseKasToSompi, sompiToKasPlain } from "@/lib/sompi";
import type { EndpointRead, OpenApiImportBody, OpenApiImportCreated, OpenApiImportMode, OpenApiImportPreview, OpenApiOperationPreview } from "@/lib/types";
import { usePricing } from "@/lib/use-pricing";
import { checkUpstreamUrl } from "@/lib/validation";
import { CodeBlock } from "./code-block";
import { CopyButton } from "./copy-button";
import { FeeSplitView } from "./fee-split";
import { ErrorBanner, Field, Toggle } from "./ui";

const HEADER_NAME_RE = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/;

type SourceKind = "url" | "file";
type Step = "source" | "preview" | "done";
interface LoadedFile {
  name: string;
  size: number;
  text: string;
}

interface Props {
  onClose: () => void;
  onImported: () => void;
  onUnauthorized: () => void;
}

export function MethodBadge({ method }: { method: string }) {
  return <span className={`badge font-mono ${methodTone(method)}`}>{method}</span>;
}

/** Three-step "Import from OpenAPI" flow: source -> preview table (select, price per row) -> created endpoints. */
export function OpenApiImport({ onClose, onImported, onUnauthorized }: Props) {
  const uid = useId();
  const pricing = usePricing();
  const [step, setStep] = useState<Step>("source");
  const [sourceKind, setSourceKind] = useState<SourceKind>("url");
  const [specUrl, setSpecUrl] = useState("");
  const [file, setFile] = useState<LoadedFile | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [reading, setReading] = useState(false);
  const [baseUrl, setBaseUrl] = useState("");
  const [priceKas, setPriceKas] = useState("1");
  const [category, setCategory] = useState("");
  const [listed, setListed] = useState(true);
  const [headerName, setHeaderName] = useState("");
  const [headerValue, setHeaderValue] = useState("");
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<OpenApiImportPreview | null>(null);
  const [rows, setRows] = useState<Record<string, ImportRowState>>({});
  const [serverRowErrors, setServerRowErrors] = useState<Record<string, string>>({});
  const [created, setCreated] = useState<EndpointRead[]>([]);

  const defaultSompi = useMemo(() => parseKasToSompi(priceKas), [priceKas]);

  const errors = useMemo(() => {
    const out: Partial<Record<"source" | "base" | "price" | "header", string>> = {};
    if (sourceKind === "url") {
      const u = checkSpecUrl(specUrl);
      if (!u.ok) out.source = u.error;
    } else if (fileError) out.source = fileError;
    else if (!file) out.source = "Choose a .json, .yaml or .yml OpenAPI file";
    const b = checkUpstreamUrl(baseUrl);
    if (!b.ok) out.base = b.error.replace(/^Upstream URL/, "Base upstream URL");
    if (defaultSompi === null) out.price = "Enter a price in KAS, e.g. 1 (up to 8 decimals)";
    else if (pricing.status === "ready" && !meetsOutputFloor(defaultSompi, pricing.minOutput))
      out.price = `Minimum price is ${formatKas(pricing.minPrice)} KAS (${formatSompi(pricing.minPrice)} sompi): ${minPriceReason(pricing.minOutput)}`;
    else if (pricing.status === "error") out.price = `Cannot verify the minimum price: ${pricing.message}`;
    else if (pricing.status === "loading") out.price = "Loading the minimum price from the gateway…";
    if (headerName.trim() !== "" || headerValue !== "") {
      if (!HEADER_NAME_RE.test(headerName.trim())) out.header = "Enter a valid header name";
      else if (headerValue === "" || /[\r\n]/.test(headerValue)) out.header = "Enter a single-line header value";
    }
    return out;
  }, [sourceKind, specUrl, file, fileError, baseUrl, defaultSompi, pricing, headerName, headerValue]);
  const show = (key: keyof typeof errors) => (touched ? (errors[key] ?? null) : null);

  const fail = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) {
        onUnauthorized();
        return;
      }
      setError(errorMessage(e));
    },
    [onUnauthorized],
  );

  async function loadFile(picked: File | undefined) {
    setFile(null);
    setFileError(null);
    if (!picked) return;
    const checked = checkSpecFile(picked);
    if (!checked.ok) {
      setFileError(checked.error);
      return;
    }
    setReading(true);
    try {
      const text = await picked.text();
      const valid = checkSpecText(text);
      if (!valid.ok) setFileError(valid.error);
      else setFile({ name: picked.name, size: picked.size, text });
    } catch {
      setFileError("Could not read the file");
    } finally {
      setReading(false);
    }
  }

  function onFileInput(event: ChangeEvent<HTMLInputElement>) {
    void loadFile(event.target.files?.[0]);
  }

  function onDrop(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    void loadFile(event.dataTransfer.files[0]);
  }

  function requestBody(mode: OpenApiImportMode, base: string): OpenApiImportBody | null {
    if (defaultSompi === null) return null;
    const body: OpenApiImportBody = {
      mode,
      upstream_base_url: base.trim(),
      default_price_sompi: defaultSompi.toString(),
      is_public_listed: listed,
      upstream_headers: headerName.trim() !== "" ? { [headerName.trim()]: headerValue } : {},
    };
    if (category.trim()) body.category = category.trim();
    if (sourceKind === "url") body.spec_url = specUrl.trim();
    else if (file) body.spec_text = file.text;
    else return null;
    return body;
  }

  async function runPreview(baseOverride?: string) {
    const base = baseOverride ?? baseUrl;
    const body = requestBody("preview", base);
    if (!body || defaultSompi === null) return;
    setBusy(true);
    setError(null);
    setServerRowErrors({});
    try {
      const result = await apiFetch<OpenApiImportPreview>("/api/endpoints/import", { method: "POST", body });
      const kas = sompiToKasPlain(defaultSompi);
      const initial: Record<string, ImportRowState> = {};
      for (const operation of result.operations) initial[operation.key] = { selected: operation.importable, priceKas: kas };
      if (baseOverride !== undefined) setBaseUrl(baseOverride);
      setPreview(result);
      setRows(initial);
      setStep("preview");
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }

  function submitSource(event: FormEvent) {
    event.preventDefault();
    setTouched(true);
    if (Object.keys(errors).length > 0) return;
    void runPreview();
  }

  const importable = useMemo(() => preview?.operations.filter((operation) => operation.importable) ?? [], [preview]);
  const selectedKeys = useMemo(() => importable.filter((operation) => rows[operation.key]?.selected).map((operation) => operation.key), [importable, rows]);
  const rowErrors = useMemo(() => {
    const out: Record<string, string> = {};
    if (pricing.status !== "ready") return out;
    for (const key of selectedKeys) {
      const row = rows[key];
      const message = row ? rowPriceError(row.priceKas, pricing.minOutput) : null;
      if (message) out[key] = message;
    }
    return out;
  }, [selectedKeys, rows, pricing]);
  const allSelected = importable.length > 0 && selectedKeys.length === importable.length;
  const someSelected = selectedKeys.length > 0 && !allSelected;

  function setRow(key: string, patch: Partial<ImportRowState>) {
    setRows((current) => {
      const row = current[key];
      return row ? { ...current, [key]: { ...row, ...patch } } : current;
    });
    setServerRowErrors((current) => {
      if (!(key in current)) return current;
      const next = { ...current };
      delete next[key];
      return next;
    });
  }

  function selectAll(selected: boolean) {
    setRows((current) => {
      const next = { ...current };
      for (const operation of importable) {
        const row = next[operation.key];
        if (row) next[operation.key] = { ...row, selected };
      }
      return next;
    });
  }

  async function runCreate() {
    if (!preview || defaultSompi === null || selectedKeys.length === 0 || Object.keys(rowErrors).length > 0) return;
    const selections = buildSelections(preview.operations, rows, defaultSompi);
    const body = requestBody("create", baseUrl);
    if (!selections || !body) return;
    body.selections = selections;
    setBusy(true);
    setError(null);
    setServerRowErrors({});
    try {
      const result = await apiFetch<OpenApiImportCreated>("/api/endpoints/import", { method: "POST", body });
      setCreated(result.created);
      setStep("done");
      onImported();
    } catch (e) {
      if (e instanceof ApiError && Object.keys(e.rowErrors).length > 0) {
        const mapped: Record<string, string> = {};
        for (const [index, message] of Object.entries(e.rowErrors)) {
          const key = selectedKeys[Number(index)];
          if (key) mapped[key] = message;
        }
        setServerRowErrors(mapped);
      }
      fail(e);
    } finally {
      setBusy(false);
    }
  }

  const proxyPaths = useMemo(() => {
    const out = new Map<string, string | null>();
    for (const operation of preview?.operations ?? []) if (operation.slug) out.set(operation.slug, operation.proxy_path);
    return out;
  }, [preview]);

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/70 p-4 backdrop-blur-sm" role="dialog" aria-modal="true" aria-labelledby={`${uid}-title`}>
      <div className="card my-8 w-full max-w-5xl bg-zinc-950 p-6">
        <div className="mb-5 flex items-start justify-between gap-4">
          <div>
            <h3 id={`${uid}-title`} className="flex items-center gap-2 text-lg font-medium text-zinc-100">
              <FileCode2 className="h-5 w-5 text-kas-400" aria-hidden />
              Import from OpenAPI
            </h3>
            <ol className="mt-2 flex flex-wrap gap-2 text-xs" aria-label="Progress">
              {(["source", "preview", "done"] as const).map((name, index) => (
                <li key={name} className={`badge ${step === name ? "border-kas-500 bg-kas-500/15 text-kas-300" : "border-zinc-800 bg-zinc-900 text-zinc-500"}`} aria-current={step === name ? "step" : undefined}>
                  {index + 1}. {name === "source" ? "Document" : name === "preview" ? "Preview & price" : "Created"}
                </li>
              ))}
            </ol>
          </div>
          <button type="button" className="btn-ghost px-2 py-1" onClick={onClose} aria-label="Close">
            <X className="h-4 w-4" aria-hidden />
          </button>
        </div>

        {step === "source" ? (
          <form onSubmit={submitSource} className="space-y-5" noValidate>
            <div className="inline-flex rounded-lg border border-zinc-800 bg-zinc-950 p-1" role="tablist" aria-label="Document source">
              {(
                [
                  ["url", "Paste URL", Link2],
                  ["file", "Upload file", FileUp],
                ] as const
              ).map(([kind, label, Icon]) => (
                <button
                  key={kind}
                  type="button"
                  role="tab"
                  aria-selected={sourceKind === kind}
                  className={`inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-sm transition ${sourceKind === kind ? "bg-zinc-800 text-zinc-100" : "text-zinc-400 hover:text-zinc-200"}`}
                  onClick={() => setSourceKind(kind)}
                >
                  <Icon className="h-4 w-4" aria-hidden />
                  {label}
                </button>
              ))}
            </div>

            {sourceKind === "url" ? (
              <Field label="OpenAPI document URL" htmlFor={`${uid}-spec`} error={show("source")} hint="OpenAPI 3.0 or 3.1, JSON or YAML, up to 2 MB. The gateway fetches it from a public address only.">
                <input id={`${uid}-spec`} className="input font-mono" value={specUrl} onChange={(e) => setSpecUrl(e.target.value)} placeholder="https://api.example.com/openapi.json" inputMode="url" autoComplete="off" spellCheck={false} />
              </Field>
            ) : (
              <div>
                <span className="label">OpenAPI file</span>
                <label
                  htmlFor={`${uid}-file`}
                  onDragOver={(e) => e.preventDefault()}
                  onDrop={onDrop}
                  className="flex cursor-pointer flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-zinc-700 bg-zinc-950 px-4 py-8 text-center text-sm text-zinc-400 transition hover:border-kas-500 hover:text-zinc-200"
                >
                  {reading ? <Loader2 className="h-6 w-6 animate-spin" aria-hidden /> : <FileUp className="h-6 w-6" aria-hidden />}
                  {file ? (
                    <span className="font-mono text-zinc-200">
                      {file.name} · {formatBytes(file.size)}
                    </span>
                  ) : (
                    <span>Drop a .json, .yaml or .yml file here, or click to choose (max {formatBytes(OPENAPI_MAX_DOCUMENT_BYTES)})</span>
                  )}
                  <input id={`${uid}-file`} type="file" accept={OPENAPI_FILE_ACCEPT} className="sr-only" onChange={onFileInput} />
                </label>
                {show("source") || fileError ? (
                  <p className="mt-1.5 text-xs text-red-400" role="alert">
                    {fileError ?? show("source")}
                  </p>
                ) : null}
              </div>
            )}

            <div className="grid gap-5 sm:grid-cols-2">
              <Field label="Base upstream URL" htmlFor={`${uid}-base`} error={show("base")} hint="Operation paths are appended to this. Must be public (no localhost / private IPs).">
                <input id={`${uid}-base`} className="input font-mono" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://api.example.com/v1" inputMode="url" autoComplete="off" spellCheck={false} />
              </Field>
              <Field
                label="Default price per call (KAS)"
                htmlFor={`${uid}-price`}
                error={show("price")}
                hint={pricing.status === "ready" ? `Applied to every operation; override per row in the preview. Minimum ${formatKas(pricing.minPrice)} KAS.` : "Applied to every operation; override per row in the preview."}
              >
                <div className="relative">
                  <input id={`${uid}-price`} className="input pr-14 font-mono" value={priceKas} onChange={(e) => setPriceKas(e.target.value)} placeholder="1" inputMode="decimal" autoComplete="off" />
                  <span className="pointer-events-none absolute inset-y-0 right-3 flex items-center font-mono text-xs text-zinc-500">KAS</span>
                </div>
              </Field>
            </div>

            {defaultSompi !== null && pricing.status === "ready" && meetsOutputFloor(defaultSompi, pricing.minOutput) ? <FeeSplitView totalSompi={defaultSompi} minOutput={pricing.minOutput} /> : null}

            <div className="grid gap-5 sm:grid-cols-3">
              <Field label="Secret upstream header (optional)" htmlFor={`${uid}-hn`} error={show("header")} hint="Stored encrypted on every imported endpoint.">
                <input id={`${uid}-hn`} className="input font-mono" value={headerName} onChange={(e) => setHeaderName(e.target.value)} placeholder="X-Api-Key" autoComplete="off" spellCheck={false} />
              </Field>
              <Field label="Header value" htmlFor={`${uid}-hv`}>
                <input id={`${uid}-hv`} type="password" className="input font-mono" value={headerValue} onChange={(e) => setHeaderValue(e.target.value)} placeholder="••••••••" autoComplete="new-password" />
              </Field>
              <Field label="Category (optional)" htmlFor={`${uid}-cat`} hint="Defaults to each operation's first tag.">
                <input id={`${uid}-cat`} className="input" value={category} onChange={(e) => setCategory(e.target.value)} placeholder="data" maxLength={60} autoComplete="off" />
              </Field>
            </div>

            <Toggle id={`${uid}-listed`} checked={listed} onChange={setListed} label="List imported endpoints in the public AI-agent directory and manifest" />

            <ErrorBanner message={error} />

            <div className="flex flex-wrap items-center gap-3">
              <button type="submit" className="btn-primary" disabled={busy || reading}>
                {busy ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : null}
                {busy ? "Reading document…" : "Preview operations"}
              </button>
              <button type="button" className="btn-ghost" onClick={onClose} disabled={busy}>
                Cancel
              </button>
              <span className="text-xs text-zinc-500">Nothing is created until you confirm in the next step.</span>
            </div>
          </form>
        ) : null}

        {step === "preview" && preview ? (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-zinc-800 bg-zinc-950/60 px-4 py-3 text-sm">
              <div>
                <div className="font-medium text-zinc-100">
                  {preview.title ?? "Untitled API"}
                  {preview.api_version ? <span className="ml-2 font-mono text-xs text-zinc-500">v{preview.api_version}</span> : null}
                </div>
                <div className="font-mono text-xs text-zinc-500">
                  OpenAPI {preview.openapi_version} · {preview.operation_count} operations · {preview.importable_count} importable · base {baseUrl}
                </div>
              </div>
              {preview.servers.length > 0 ? (
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <Server className="h-3.5 w-3.5 text-zinc-500" aria-hidden />
                  {preview.servers.map((server) =>
                    checkUpstreamUrl(server).ok && server !== baseUrl ? (
                      <button key={server} type="button" className="btn-secondary px-2 py-1 font-mono text-xs" onClick={() => void runPreview(server)} disabled={busy} title="Use this server as the base upstream URL">
                        {server}
                      </button>
                    ) : (
                      <span key={server} className="badge border-zinc-800 bg-zinc-900 font-mono text-zinc-500">
                        {server}
                      </span>
                    ),
                  )}
                </div>
              ) : null}
            </div>

            {importable.length === 0 ? (
              <div className="card p-6 text-center text-sm text-zinc-400">No operation in this document can be imported. Each row below says why.</div>
            ) : null}

            <div className="overflow-x-auto rounded-lg border border-zinc-800">
              <table className="w-full min-w-[860px] text-left text-sm">
                <thead className="bg-zinc-900/70 text-xs uppercase tracking-wide text-zinc-500">
                  <tr>
                    <th className="w-10 px-3 py-2">
                      <input
                        type="checkbox"
                        aria-label="Select all importable operations"
                        className="h-4 w-4 accent-kas-400"
                        checked={allSelected}
                        ref={(node) => {
                          if (node) node.indeterminate = someSelected;
                        }}
                        onChange={(e) => selectAll(e.target.checked)}
                        disabled={importable.length === 0}
                      />
                    </th>
                    <th className="px-3 py-2">Operation</th>
                    <th className="px-3 py-2">operationId · summary</th>
                    <th className="px-3 py-2">Buyer route</th>
                    <th className="w-44 px-3 py-2">Price (KAS)</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-zinc-800">
                  {preview.operations.map((operation) => (
                    <PreviewRow
                      key={operation.key}
                      uid={uid}
                      operation={operation}
                      row={rows[operation.key]}
                      error={rowErrors[operation.key] ?? serverRowErrors[operation.key] ?? null}
                      onToggle={(selected) => setRow(operation.key, { selected })}
                      onPrice={(priceKas) => setRow(operation.key, { priceKas })}
                    />
                  ))}
                </tbody>
              </table>
            </div>

            <ErrorBanner message={error} />

            <div className="flex flex-wrap items-center gap-3">
              <button type="button" className="btn-primary" onClick={() => void runCreate()} disabled={busy || selectedKeys.length === 0 || Object.keys(rowErrors).length > 0}>
                {busy ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : <CheckCircle2 className="h-4 w-4" aria-hidden />}
                {busy ? "Creating…" : `Create ${selectedKeys.length} endpoint${selectedKeys.length === 1 ? "" : "s"}`}
              </button>
              <button
                type="button"
                className="btn-ghost"
                onClick={() => {
                  setError(null);
                  setStep("source");
                }}
                disabled={busy}
              >
                <ArrowLeft className="h-4 w-4" aria-hidden />
                Back
              </button>
              <span className="text-xs text-zinc-500">
                {selectedKeys.length} of {importable.length} selected · all-or-nothing: if any row fails, nothing is created.
              </span>
            </div>
          </div>
        ) : null}

        {step === "done" ? (
          <div className="space-y-4">
            <div className="flex items-start gap-2 rounded-lg border border-emerald-800/70 bg-emerald-950/40 px-3 py-2 text-sm text-emerald-200" role="status">
              <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
              <span>
                Created {created.length} paid endpoint{created.length === 1 ? "" : "s"}. They are live now and listed in your endpoints.
              </span>
            </div>
            <ul className="grid gap-3">
              {created.map((endpoint) => {
                const method = endpoint.allowed_methods[0] ?? "GET";
                const example = exampleCallUrl(endpoint.proxy_url, proxyPaths.get(endpoint.slug) ?? null);
                return (
                  <li key={endpoint.id} className="card space-y-3 p-4">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div className="flex min-w-0 flex-wrap items-center gap-2">
                        <MethodBadge method={method} />
                        <span className="font-medium text-zinc-100">{endpoint.name}</span>
                      </div>
                      <div className="text-right font-mono text-sm text-zinc-100">
                        {formatKas(endpoint.price_sompi)} KAS <span className="text-xs text-zinc-500">({formatSompi(endpoint.price_sompi)} sompi)</span>
                      </div>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      <code className="max-w-full overflow-x-auto rounded border border-zinc-800 bg-zinc-950 px-2 py-1 font-mono text-xs text-kas-300">{endpoint.proxy_url}</code>
                      <CopyButton value={endpoint.proxy_url} label="Copy" />
                    </div>
                    <CodeBlock code={curlImported(endpoint.proxy_url, method, example.url, example.variables)} title="curl" />
                  </li>
                );
              })}
            </ul>
            <div className="flex flex-wrap gap-3">
              <button type="button" className="btn-primary" onClick={onClose}>
                Done
              </button>
              <button
                type="button"
                className="btn-secondary"
                onClick={() => {
                  setCreated([]);
                  setPreview(null);
                  setStep("source");
                }}
              >
                Import another document
              </button>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}

function PreviewRow({
  uid,
  operation,
  row,
  error,
  onToggle,
  onPrice,
}: {
  uid: string;
  operation: OpenApiOperationPreview;
  row: ImportRowState | undefined;
  error: string | null;
  onToggle: (selected: boolean) => void;
  onPrice: (priceKas: string) => void;
}) {
  const disabled = !operation.importable;
  const selected = Boolean(row?.selected) && !disabled;
  const inputId = `${uid}-row-${operation.key.replace(/[^A-Za-z0-9]+/g, "-")}`;
  return (
    <tr className={disabled ? "bg-zinc-950/40 text-zinc-600" : selected ? "bg-kas-500/5" : ""} data-operation={operation.key}>
      <td className="px-3 py-2 align-top">
        <input type="checkbox" className="h-4 w-4 accent-kas-400" aria-label={`Import ${operation.key}`} checked={selected} disabled={disabled} onChange={(e) => onToggle(e.target.checked)} />
      </td>
      <td className="px-3 py-2 align-top">
        <div className="flex items-center gap-2">
          <MethodBadge method={operation.method} />
          <code className={`break-all font-mono text-xs ${disabled ? "text-zinc-600" : "text-zinc-200"}`}>{operation.path}</code>
        </div>
        {disabled && operation.skip_reason ? <p className="mt-1 text-xs text-amber-400/80">Skipped: {operation.skip_reason}</p> : null}
      </td>
      <td className="max-w-xs px-3 py-2 align-top">
        <div className="truncate font-mono text-xs text-zinc-400">{operation.operation_id ?? "—"}</div>
        <div className={`truncate text-xs ${disabled ? "text-zinc-600" : "text-zinc-300"}`} title={operation.summary ?? undefined}>
          {operation.summary ?? ""}
        </div>
      </td>
      <td className="px-3 py-2 align-top">
        {operation.proxy_path ? (
          <>
            <code className="break-all font-mono text-xs text-kas-300">{operation.proxy_path}</code>
            {operation.upstream_url ? <div className="mt-0.5 break-all font-mono text-[11px] text-zinc-500">→ {operation.upstream_url}</div> : null}
          </>
        ) : (
          <span className="text-xs">—</span>
        )}
      </td>
      <td className="px-3 py-2 align-top">
        {disabled ? (
          <span className="text-xs">—</span>
        ) : (
          <>
            <label htmlFor={inputId} className="sr-only">
              Price in KAS for {operation.key}
            </label>
            <input
              id={inputId}
              className={`input py-1.5 font-mono text-xs ${error ? "border-red-700 focus:border-red-500 focus:ring-red-500" : ""}`}
              value={row?.priceKas ?? ""}
              onChange={(e) => onPrice(e.target.value)}
              disabled={!selected}
              inputMode="decimal"
              autoComplete="off"
              aria-invalid={error ? true : undefined}
            />
            {error && selected ? (
              <p className="mt-1 text-xs text-red-400" role="alert">
                {error}
              </p>
            ) : null}
          </>
        )}
      </td>
    </tr>
  );
}
