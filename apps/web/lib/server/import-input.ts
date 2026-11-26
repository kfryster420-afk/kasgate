import "server-only";
import {
  OPENAPI_MAX_OPERATIONS,
  OPENAPI_MAX_PATH_LENGTH,
  OPENAPI_MAX_REQUEST_BYTES,
  checkSpecText,
  checkSpecUrl,
  isHttpMethod,
} from "../openapi-import";
import type { OpenApiImportBody, OpenApiImportSelection } from "../types";
import type { PriceCurrency } from "../types";
import {
  checkFreeTrialCalls,
  checkOptionalText,
  checkPriceCurrency,
  checkPriceSompi,
  checkPriceUsdMicros,
  checkUpstreamHeaders,
  checkUpstreamUrl,
} from "../validation";

export type ImportInputResult =
  | { ok: true; payload: OpenApiImportBody }
  | { ok: false; status: 413 | 422; error: string; rowErrors?: Record<string, string> };

export type CappedBody = { ok: true; text: string } | { ok: false };

/** Reads the request body as UTF-8 text, giving up as soon as it exceeds `maxBytes` (declared or streamed). */
export async function readCappedText(request: Request, maxBytes: number = OPENAPI_MAX_REQUEST_BYTES): Promise<CappedBody> {
  const declared = request.headers.get("content-length");
  if (declared !== null && /^\d+$/.test(declared) && Number(declared) > maxBytes) return { ok: false };
  if (!request.body) return { ok: true, text: "" };
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > maxBytes) {
      await reader.cancel();
      return { ok: false };
    }
    chunks.push(value);
  }
  const joined = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    joined.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return { ok: true, text: new TextDecoder().decode(joined) };
}

function checkSelections(
  input: unknown,
  currency: PriceCurrency,
  floor: bigint,
): { selections?: OpenApiImportSelection[]; error?: string; rowErrors: Record<string, string> } {
  const rowErrors: Record<string, string> = {};
  if (!Array.isArray(input) || input.length === 0) return { error: "Select at least one operation to import", rowErrors };
  if (input.length > OPENAPI_MAX_OPERATIONS) return { error: `At most ${OPENAPI_MAX_OPERATIONS} operations can be imported at once`, rowErrors };
  const selections: OpenApiImportSelection[] = [];
  const seen = new Set<string>();
  const wrongField = currency === "USD" ? "price_sompi" : "price_usd_micros";
  const wrongMessage =
    currency === "USD" ? "price_sompi must be omitted in a USD-priced import" : "price_usd_micros must be omitted in a KAS-priced import";
  input.forEach((raw: unknown, index) => {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
      rowErrors[String(index)] = "Selection must be an object";
      return;
    }
    const item = raw as Record<string, unknown>;
    const method = typeof item.method === "string" ? item.method.trim().toUpperCase() : "";
    const path = typeof item.path === "string" ? item.path : "";
    if (!isHttpMethod(method)) {
      rowErrors[String(index)] = `Unsupported method: ${String(item.method)}`;
      return;
    }
    if (path === "" || path.length > OPENAPI_MAX_PATH_LENGTH) {
      rowErrors[String(index)] = "Path is missing or too long";
      return;
    }
    const key = `${method} ${path}`;
    if (seen.has(key)) {
      rowErrors[String(index)] = `${key} is selected twice`;
      return;
    }
    seen.add(key);
    if (item[wrongField] !== undefined && item[wrongField] !== null) {
      rowErrors[String(index)] = wrongMessage;
      return;
    }
    const selection: OpenApiImportSelection = { method, path };
    if (currency === "USD") {
      if (item.price_usd_micros !== undefined && item.price_usd_micros !== null) {
        const price = checkPriceUsdMicros(item.price_usd_micros);
        if (!price.ok) {
          rowErrors[String(index)] = price.error;
          return;
        }
        selection.price_usd_micros = price.value;
      }
    } else if (item.price_sompi !== undefined && item.price_sompi !== null) {
      const price = checkPriceSompi(item.price_sompi, floor);
      if (!price.ok) {
        rowErrors[String(index)] = price.error;
        return;
      }
      selection.price_sompi = price.value;
    }
    selections.push(selection);
  });
  return Object.keys(rowErrors).length > 0 ? { error: "Some selected operations are invalid", rowErrors } : { selections, rowErrors };
}

/** Validates the browser's import request with the same rules as the endpoint form; the gateway stays the authority. */
export function buildImportPayload(body: Record<string, unknown>, floor: bigint): ImportInputResult {
  const errors: string[] = [];
  const mode = body.mode;
  if (mode !== "preview" && mode !== "create") return { ok: false, status: 422, error: 'mode must be "preview" or "create"' };

  const hasText = body.spec_text !== undefined && body.spec_text !== null && body.spec_text !== "";
  const hasUrl = body.spec_url !== undefined && body.spec_url !== null && body.spec_url !== "";
  if (hasText === hasUrl) return { ok: false, status: 422, error: "Provide either a document URL or an uploaded file" };

  const payload: Partial<OpenApiImportBody> & { mode: typeof mode } = { mode };
  if (hasText) {
    const text = checkSpecText(body.spec_text);
    if (!text.ok) return { ok: false, status: text.error.startsWith("The document is larger") ? 413 : 422, error: text.error };
    payload.spec_text = text.value;
  } else {
    const url = checkSpecUrl(body.spec_url);
    if (url.ok) payload.spec_url = url.value;
    else errors.push(url.error);
  }

  const base = checkUpstreamUrl(body.upstream_base_url);
  if (base.ok) payload.upstream_base_url = base.value;
  else errors.push(base.error.replace(/^Upstream URL/, "Base upstream URL"));

  const currencyResult = checkPriceCurrency(body.default_price_currency === undefined ? "KAS" : body.default_price_currency);
  const currency: PriceCurrency = currencyResult.ok ? currencyResult.value : "KAS";
  if (!currencyResult.ok) errors.push(currencyResult.error);
  else payload.default_price_currency = currency;

  if (currency === "USD") {
    const price = checkPriceUsdMicros(body.default_price_usd_micros);
    if (price.ok) payload.default_price_usd_micros = price.value;
    else errors.push(`Default price: ${price.error}`);
    if (body.default_price_sompi !== undefined && body.default_price_sompi !== null) {
      errors.push("default_price_sompi must be omitted when default_price_currency is USD");
    }
  } else {
    const price = checkPriceSompi(body.default_price_sompi, floor);
    if (price.ok) payload.default_price_sompi = price.value;
    else errors.push(`Default price: ${price.error}`);
    if (body.default_price_usd_micros !== undefined && body.default_price_usd_micros !== null) {
      errors.push("default_price_usd_micros must be omitted when default_price_currency is KAS");
    }
  }

  const headers = checkUpstreamHeaders(body.upstream_headers);
  if (headers.ok) payload.upstream_headers = headers.value;
  else errors.push(headers.error);

  const category = checkOptionalText(body.category, "Category", 60);
  if (category.ok && category.value) payload.category = category.value;
  else if (!category.ok) errors.push(category.error);

  const listed = body.is_public_listed === undefined ? true : body.is_public_listed;
  if (typeof listed !== "boolean") errors.push("is_public_listed must be a boolean");
  else payload.is_public_listed = listed;

  if (body.free_trial_calls !== undefined) {
    const trials = checkFreeTrialCalls(body.free_trial_calls);
    if (trials.ok) payload.free_trial_calls = trials.value;
    else errors.push(trials.error);
  }

  if (mode === "create") {
    const checked = checkSelections(body.selections, currency, floor);
    if (checked.error) return { ok: false, status: 422, error: [...errors, checked.error].join("; "), rowErrors: checked.rowErrors };
    payload.selections = checked.selections;
  } else if (body.selections !== undefined) {
    errors.push("selections are only accepted with mode \"create\"");
  }

  if (errors.length > 0) return { ok: false, status: 422, error: errors.join("; ") };
  return { ok: true, payload: payload as OpenApiImportBody };
}

/** Per-selection messages from a FastAPI 422 body (`loc: ["body", "selections", <index>, ...]`). */
export function gatewayRowErrors(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  try {
    const parsed: unknown = JSON.parse(text);
    const detail = parsed && typeof parsed === "object" ? (parsed as { detail?: unknown }).detail : undefined;
    if (!Array.isArray(detail)) return out;
    for (const item of detail) {
      if (!item || typeof item !== "object") continue;
      const { loc, msg } = item as { loc?: unknown; msg?: unknown };
      if (!Array.isArray(loc) || loc[0] !== "body" || loc[1] !== "selections" || typeof loc[2] !== "number") continue;
      const message = typeof msg === "string" ? msg.replace(/^Value error, /, "") : "invalid value";
      const key = String(loc[2]);
      out[key] = out[key] ? `${out[key]}; ${message}` : message;
    }
  } catch {
    /* not a validation body */
  }
  return out;
}
