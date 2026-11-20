"use client";

import { Loader2 } from "lucide-react";
import { useId, useMemo, useState, type FormEvent } from "react";
import { ADDRESS_PREFIX } from "@/lib/config";
import { formatKas, formatSompi, meetsOutputFloor, minPriceReason, parseKasToSompi, sompiToKasPlain } from "@/lib/sompi";
import { usePricing } from "@/lib/use-pricing";
import { HTTP_METHODS, type EndpointCreate, type EndpointRead, type EndpointUpdate, type HttpMethod } from "@/lib/types";
import { checkEndpointName, checkOptionalSlug, checkUpstreamUrl } from "@/lib/validation";
import { FeeSplitView } from "./fee-split";
import { ErrorBanner, Field, Toggle } from "./ui";

const CATEGORIES = ["data", "ai", "finance", "weather", "geo", "media", "search", "dev-tools", "other"] as const;
const HEADER_NAME_RE = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/;

type Props =
  | { mode: "create"; busy: boolean; error: string | null; onSubmit: (payload: EndpointCreate) => void; onCancel?: undefined; initial?: undefined }
  | { mode: "edit"; busy: boolean; error: string | null; onSubmit: (payload: EndpointUpdate) => void; onCancel: () => void; initial: EndpointRead };

export function EndpointForm(props: Props) {
  const uid = useId();
  const pricing = usePricing();
  const initial = props.initial;
  const [name, setName] = useState(initial?.name ?? "");
  const [upstreamUrl, setUpstreamUrl] = useState(initial?.upstream_url ?? "");
  const [priceKas, setPriceKas] = useState(initial ? sompiToKasPlain(initial.price_sompi) : "");
  const [methods, setMethods] = useState<HttpMethod[]>(
    initial ? initial.allowed_methods.filter((m): m is HttpMethod => (HTTP_METHODS as readonly string[]).includes(m)) : ["GET"],
  );
  const [category, setCategory] = useState(initial?.category ?? "");
  const [description, setDescription] = useState(initial?.description ?? "");
  const [listed, setListed] = useState(initial?.is_public_listed ?? true);
  const [slug, setSlug] = useState("");
  const [headerName, setHeaderName] = useState("");
  const [headerValue, setHeaderValue] = useState("");
  const [touched, setTouched] = useState(false);

  const sompi = useMemo(() => parseKasToSompi(priceKas), [priceKas]);

  const errors = useMemo(() => {
    const out: Partial<Record<"name" | "url" | "price" | "methods" | "slug" | "header", string>> = {};
    const n = checkEndpointName(name);
    if (!n.ok) out.name = n.error;
    const u = checkUpstreamUrl(upstreamUrl);
    if (!u.ok) out.url = u.error;
    if (sompi === null) out.price = "Enter a price in KAS, e.g. 6.67 (up to 8 decimals)";
    else if (pricing.status === "ready" && !meetsOutputFloor(sompi, pricing.minOutput))
      out.price = `Minimum price is ${formatKas(pricing.minPrice)} KAS (${formatSompi(pricing.minPrice)} sompi): ${minPriceReason(pricing.minOutput)}`;
    else if (pricing.status === "error") out.price = `Cannot verify the minimum price: ${pricing.message}`;
    else if (pricing.status === "loading") out.price = "Loading the minimum price from the gateway…";
    if (methods.length === 0) out.methods = "Select at least one method";
    if (props.mode === "create") {
      const s = checkOptionalSlug(slug);
      if (!s.ok) out.slug = s.error;
    }
    if (headerName.trim() !== "" || headerValue !== "") {
      if (!HEADER_NAME_RE.test(headerName.trim())) out.header = "Enter a valid header name";
      else if (headerValue === "" || /[\r\n]/.test(headerValue)) out.header = "Enter a single-line header value";
    }
    return out;
  }, [name, upstreamUrl, sompi, methods, slug, headerName, headerValue, props.mode, pricing]);

  const minPriceHint =
    pricing.status === "ready"
      ? `Fixed price per call. Minimum ${formatKas(pricing.minPrice)} KAS (${formatSompi(pricing.minPrice)} sompi): ${minPriceReason(pricing.minOutput)}.`
      : "Fixed price per call, entered in KAS and stored as sompi. The minimum is read from the gateway.";
  const hasErrors = Object.keys(errors).length > 0;
  const show = (key: keyof typeof errors) => (touched ? (errors[key] ?? null) : null);

  function toggleMethod(method: HttpMethod) {
    setMethods((current) => (current.includes(method) ? current.filter((m) => m !== method) : [...current, method]));
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    setTouched(true);
    if (hasErrors || sompi === null) return;
    const headers = headerName.trim() !== "" ? { [headerName.trim()]: headerValue } : undefined;
    if (props.mode === "create") {
      const payload: EndpointCreate = {
        name: name.trim(),
        upstream_url: upstreamUrl.trim(),
        price_sompi: sompi.toString(),
        allowed_methods: methods,
        is_public_listed: listed,
        upstream_headers: headers ?? {},
      };
      if (category.trim()) payload.category = category.trim();
      if (description.trim()) payload.description = description.trim();
      if (slug.trim()) payload.slug = slug.trim().toLowerCase();
      props.onSubmit(payload);
    } else {
      const payload: EndpointUpdate = {
        name: name.trim(),
        upstream_url: upstreamUrl.trim(),
        price_sompi: sompi.toString(),
        allowed_methods: methods,
        is_public_listed: listed,
        category: category.trim() === "" ? null : category.trim(),
        description: description.trim() === "" ? null : description.trim(),
      };
      if (headers) payload.upstream_headers = headers;
      props.onSubmit(payload);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-5" noValidate>
      <div className="grid gap-5 sm:grid-cols-2">
        <Field label="Endpoint name" htmlFor={`${uid}-name`} error={show("name")}>
          <input id={`${uid}-name`} className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="Weather API" maxLength={120} autoComplete="off" />
        </Field>
        <Field label="Category" htmlFor={`${uid}-cat`} hint="Used for filtering in the public directory">
          <input id={`${uid}-cat`} className="input" list={`${uid}-cats`} value={category} onChange={(e) => setCategory(e.target.value)} placeholder="data" maxLength={60} autoComplete="off" />
          <datalist id={`${uid}-cats`}>
            {CATEGORIES.map((c) => (
              <option key={c} value={c} />
            ))}
          </datalist>
        </Field>
      </div>

      <Field label="Upstream API URL" htmlFor={`${uid}-url`} error={show("url")} hint="The API KasGate forwards paid calls to. Must be public (no localhost / private IPs).">
        <input id={`${uid}-url`} className="input font-mono" value={upstreamUrl} onChange={(e) => setUpstreamUrl(e.target.value)} placeholder="https://api.example.com/v1" inputMode="url" autoComplete="off" spellCheck={false} />
      </Field>

      <div className="grid gap-5 sm:grid-cols-2">
        <Field
          label="Secret upstream header (optional)"
          htmlFor={`${uid}-hn`}
          error={show("header")}
          hint={props.mode === "edit" ? "Leave blank to keep the stored headers. Entering one replaces them all." : "Stored encrypted; injected by KasGate so buyers never see it."}
        >
          <input id={`${uid}-hn`} className="input font-mono" value={headerName} onChange={(e) => setHeaderName(e.target.value)} placeholder="X-Api-Key" autoComplete="off" spellCheck={false} />
        </Field>
        <Field label="Header value" htmlFor={`${uid}-hv`}>
          <input id={`${uid}-hv`} type="password" className="input font-mono" value={headerValue} onChange={(e) => setHeaderValue(e.target.value)} placeholder="••••••••" autoComplete="new-password" />
        </Field>
      </div>

      <Field label="Price per call (KAS)" htmlFor={`${uid}-price`} error={show("price")} hint={minPriceHint}>
        <div className="relative">
          <input id={`${uid}-price`} className="input pr-14 font-mono" value={priceKas} onChange={(e) => setPriceKas(e.target.value)} placeholder="6.67" inputMode="decimal" autoComplete="off" />
          <span className="pointer-events-none absolute inset-y-0 right-3 flex items-center font-mono text-xs text-zinc-500">KAS</span>
        </div>
      </Field>

      {sompi !== null && pricing.status === "ready" && meetsOutputFloor(sompi, pricing.minOutput) ? <FeeSplitView totalSompi={sompi} minOutput={pricing.minOutput} /> : null}

      <fieldset>
        <legend className="label">Allowed methods</legend>
        <div className="flex flex-wrap gap-2">
          {HTTP_METHODS.map((m) => {
            const on = methods.includes(m);
            return (
              <label key={m} className={`cursor-pointer rounded-md border px-3 py-1.5 font-mono text-xs transition ${on ? "border-kas-500 bg-kas-500/15 text-kas-300" : "border-zinc-800 bg-zinc-950 text-zinc-400 hover:border-zinc-700"}`}>
                <input type="checkbox" className="sr-only" checked={on} onChange={() => toggleMethod(m)} />
                {m}
              </label>
            );
          })}
        </div>
        {show("methods") ? <p className="mt-1.5 text-xs text-red-400">{show("methods")}</p> : null}
      </fieldset>

      <Field label="Description (optional)" htmlFor={`${uid}-desc`} hint="Shown in the public directory and manifest">
        <textarea id={`${uid}-desc`} className="input min-h-[72px] resize-y" value={description} onChange={(e) => setDescription(e.target.value)} maxLength={2000} />
      </Field>

      {props.mode === "create" ? (
        <details className="rounded-lg border border-zinc-800 bg-zinc-950/50 px-4 py-3 text-sm">
          <summary className="cursor-pointer text-zinc-400">Advanced: custom slug</summary>
          <div className="mt-3">
            <Field label="Slug" htmlFor={`${uid}-slug`} error={show("slug")} hint="Leave empty to auto-generate from the name.">
              <input id={`${uid}-slug`} className="input font-mono" value={slug} onChange={(e) => setSlug(e.target.value)} placeholder="weather-api" autoComplete="off" spellCheck={false} />
            </Field>
          </div>
        </details>
      ) : null}

      <Toggle id={`${uid}-listed`} checked={listed} onChange={setListed} label="List in the public AI-agent directory and manifest" />

      <ErrorBanner message={props.error} />

      <div className="flex flex-wrap items-center gap-3">
        <button type="submit" className="btn-primary" disabled={props.busy}>
          {props.busy ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : null}
          {props.mode === "create" ? "Create endpoint" : "Save changes"}
        </button>
        {props.mode === "edit" ? (
          <button type="button" className="btn-ghost" onClick={props.onCancel} disabled={props.busy}>
            Cancel
          </button>
        ) : null}
        {props.mode === "create" ? <span className="text-xs text-zinc-500">Payouts go to your {ADDRESS_PREFIX} address, non-custodially.</span> : null}
      </div>
    </form>
  );
}
