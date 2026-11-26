import "server-only";
import {
  checkEndpointName,
  checkFreeTrialCalls,
  checkMethods,
  checkOptionalSlug,
  checkOptionalText,
  checkPrepaidPriceSompi,
  checkPriceCurrency,
  checkPriceSompi,
  checkPriceUsdMicros,
  checkTrialDailyCap,
  checkUpstreamHeaders,
  checkUpstreamUrl,
  type Check,
} from "../validation";
import { getMinOutputSompi } from "./gateway";

type Payload = Record<string, unknown>;

function collect<T>(check: Check<T>, errors: string[]): T | undefined {
  if (check.ok) return check.value;
  errors.push(check.error);
  return undefined;
}

export type PayloadResult = { ok: true; payload: Payload } | { ok: false; error: string };

const FLOOR_UNAVAILABLE = "Cannot verify the minimum price right now: the gateway directory is unreachable. Try again shortly.";

export async function buildCreatePayload(body: Payload): Promise<PayloadResult> {
  const prepaidOnly = body.prepaid_only === undefined ? false : body.prepaid_only;
  if (typeof prepaidOnly !== "boolean") return { ok: false, error: "prepaid_only must be a boolean" };
  const floor = prepaidOnly ? null : await getMinOutputSompi();
  if (!prepaidOnly && floor === null) return { ok: false, error: FLOOR_UNAVAILABLE };
  const errors: string[] = [];
  const name = collect(checkEndpointName(body.name), errors);
  const slug = collect(checkOptionalSlug(body.slug), errors);
  const description = collect(checkOptionalText(body.description, "Description", 2000), errors);
  const category = collect(checkOptionalText(body.category, "Category", 60), errors);
  const upstreamUrl = collect(checkUpstreamUrl(body.upstream_url), errors);
  const headers = collect(checkUpstreamHeaders(body.upstream_headers), errors);
  const methods = collect(checkMethods(body.allowed_methods ?? ["GET"]), errors);
  const currency = collect(checkPriceCurrency(body.price_currency === undefined ? "KAS" : body.price_currency), errors);
  const listed = body.is_public_listed === undefined ? true : body.is_public_listed;
  if (typeof listed !== "boolean") errors.push("is_public_listed must be a boolean");
  const freeTrialCalls = collect(checkFreeTrialCalls(body.free_trial_calls === undefined ? 0 : body.free_trial_calls), errors);
  const trialDailyCap = body.trial_daily_cap === undefined ? undefined : collect(checkTrialDailyCap(body.trial_daily_cap), errors);
  let priceSompi: string | undefined;
  let priceUsdMicros: string | undefined;
  if (currency === "USD") {
    priceUsdMicros = collect(checkPriceUsdMicros(body.price_usd_micros), errors);
    if (body.price_sompi !== undefined && body.price_sompi !== null) {
      errors.push("price_sompi must be omitted when price_currency is USD");
    }
  } else if (currency === "KAS") {
    priceSompi = collect(floor === null ? checkPrepaidPriceSompi(body.price_sompi) : checkPriceSompi(body.price_sompi, floor), errors);
    if (body.price_usd_micros !== undefined && body.price_usd_micros !== null) {
      errors.push("price_usd_micros must be omitted when price_currency is KAS");
    }
  }
  if (errors.length > 0) return { ok: false, error: errors.join("; ") };

  const payload: Payload = {
    name,
    upstream_url: upstreamUrl,
    price_currency: currency,
    allowed_methods: methods,
    is_public_listed: listed,
    prepaid_only: prepaidOnly,
    upstream_headers: headers ?? {},
    free_trial_calls: freeTrialCalls,
  };
  if (trialDailyCap !== undefined) payload.trial_daily_cap = trialDailyCap;
  if (currency === "USD") payload.price_usd_micros = priceUsdMicros;
  else payload.price_sompi = priceSompi;
  if (slug) payload.slug = slug;
  if (description) payload.description = description;
  if (category) payload.category = category;
  return { ok: true, payload };
}

export async function buildUpdatePayload(body: Payload): Promise<PayloadResult> {
  const errors: string[] = [];
  const payload: Payload = {};

  if (body.name !== undefined) {
    const v = collect(checkEndpointName(body.name), errors);
    if (v !== undefined) payload.name = v;
  }
  if (body.description !== undefined) {
    const v = collect(checkOptionalText(body.description, "Description", 2000), errors);
    if (v !== undefined) payload.description = v;
  }
  if (body.category !== undefined) {
    const v = collect(checkOptionalText(body.category, "Category", 60), errors);
    if (v !== undefined) payload.category = v;
  }
  if (body.upstream_url !== undefined && body.upstream_url !== null) {
    const v = collect(checkUpstreamUrl(body.upstream_url), errors);
    if (v !== undefined) payload.upstream_url = v;
  }
  if (body.upstream_headers !== undefined && body.upstream_headers !== null) {
    const v = collect(checkUpstreamHeaders(body.upstream_headers), errors);
    if (v !== undefined) payload.upstream_headers = v;
  }
  const hasPrice =
    body.price_currency !== undefined ||
    (body.price_sompi !== undefined && body.price_sompi !== null) ||
    (body.price_usd_micros !== undefined && body.price_usd_micros !== null);
  if (hasPrice) {
    if (body.price_sompi !== undefined && body.price_sompi !== null && body.price_usd_micros !== undefined && body.price_usd_micros !== null) {
      errors.push("set only one of price_sompi or price_usd_micros");
    } else {
      const currencyCheck =
        body.price_currency === undefined
          ? { ok: true as const, value: body.price_usd_micros !== undefined && body.price_usd_micros !== null ? ("USD" as const) : ("KAS" as const) }
          : checkPriceCurrency(body.price_currency);
      const currency = collect(currencyCheck, errors);
      if (currency === "USD") {
        if (body.price_sompi !== undefined && body.price_sompi !== null) {
          errors.push("price_sompi must be omitted when price_currency is USD");
        }
        const v = collect(checkPriceUsdMicros(body.price_usd_micros), errors);
        if (v !== undefined) {
          payload.price_currency = "USD";
          payload.price_usd_micros = v;
        }
      } else if (currency === "KAS") {
        if (body.price_usd_micros !== undefined && body.price_usd_micros !== null) {
          errors.push("price_usd_micros must be omitted when price_currency is KAS");
        }
        // A prepaid-only endpoint has no on-chain floor; the gateway re-checks the merged row either way.
        let v: string | undefined;
        if (body.prepaid_only === true) {
          v = collect(checkPrepaidPriceSompi(body.price_sompi), errors);
        } else {
          const floor = await getMinOutputSompi();
          if (floor === null) return { ok: false, error: FLOOR_UNAVAILABLE };
          v = collect(checkPriceSompi(body.price_sompi, floor), errors);
        }
        if (v !== undefined) {
          payload.price_currency = "KAS";
          payload.price_sompi = v;
        }
      }
    }
  }
  if (body.allowed_methods !== undefined && body.allowed_methods !== null) {
    const v = collect(checkMethods(body.allowed_methods), errors);
    if (v !== undefined) payload.allowed_methods = v;
  }
  if (body.free_trial_calls !== undefined) {
    const v = collect(checkFreeTrialCalls(body.free_trial_calls), errors);
    if (v !== undefined) payload.free_trial_calls = v;
  }
  if (body.trial_daily_cap !== undefined) {
    const v = collect(checkTrialDailyCap(body.trial_daily_cap), errors);
    if (v !== undefined) payload.trial_daily_cap = v;
  }
  for (const flag of ["is_active", "is_public_listed", "prepaid_only"] as const) {
    const v = body[flag];
    if (v === undefined || v === null) continue;
    if (typeof v !== "boolean") errors.push(`${flag} must be a boolean`);
    else payload[flag] = v;
  }

  if (errors.length > 0) return { ok: false, error: errors.join("; ") };
  if (Object.keys(payload).length === 0) return { ok: false, error: "No changes supplied" };
  return { ok: true, payload };
}
