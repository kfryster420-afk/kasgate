import "server-only";
import {
  checkEndpointName,
  checkMethods,
  checkOptionalSlug,
  checkOptionalText,
  checkPriceSompi,
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
  const floor = await getMinOutputSompi();
  if (floor === null) return { ok: false, error: FLOOR_UNAVAILABLE };
  const errors: string[] = [];
  const name = collect(checkEndpointName(body.name), errors);
  const slug = collect(checkOptionalSlug(body.slug), errors);
  const description = collect(checkOptionalText(body.description, "Description", 2000), errors);
  const category = collect(checkOptionalText(body.category, "Category", 60), errors);
  const upstreamUrl = collect(checkUpstreamUrl(body.upstream_url), errors);
  const headers = collect(checkUpstreamHeaders(body.upstream_headers), errors);
  const methods = collect(checkMethods(body.allowed_methods ?? ["GET"]), errors);
  const price = collect(checkPriceSompi(body.price_sompi, floor), errors);
  const listed = body.is_public_listed === undefined ? true : body.is_public_listed;
  if (typeof listed !== "boolean") errors.push("is_public_listed must be a boolean");
  if (errors.length > 0) return { ok: false, error: errors.join("; ") };

  const payload: Payload = {
    name,
    upstream_url: upstreamUrl,
    price_sompi: price,
    allowed_methods: methods,
    is_public_listed: listed,
    upstream_headers: headers ?? {},
  };
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
  if (body.price_sompi !== undefined && body.price_sompi !== null) {
    const floor = await getMinOutputSompi();
    if (floor === null) return { ok: false, error: FLOOR_UNAVAILABLE };
    const v = collect(checkPriceSompi(body.price_sompi, floor), errors);
    if (v !== undefined) payload.price_sompi = v;
  }
  if (body.allowed_methods !== undefined && body.allowed_methods !== null) {
    const v = collect(checkMethods(body.allowed_methods), errors);
    if (v !== undefined) payload.allowed_methods = v;
  }
  for (const flag of ["is_active", "is_public_listed"] as const) {
    const v = body[flag];
    if (v === undefined || v === null) continue;
    if (typeof v !== "boolean") errors.push(`${flag} must be a boolean`);
    else payload[flag] = v;
  }

  if (errors.length > 0) return { ok: false, error: errors.join("; ") };
  if (Object.keys(payload).length === 0) return { ok: false, error: "No changes supplied" };
  return { ok: true, payload };
}
