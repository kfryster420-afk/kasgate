import "server-only";
import { checkOptionalText, checkWebhookEventTypes, checkWebhookUrl } from "../validation";

type Payload = Record<string, unknown>;
export type WebhookPayloadResult = { ok: true; payload: Payload } | { ok: false; error: string };

/** Validates a webhook create body (CONTRACT v2.12); the gateway re-validates and re-resolves on every delivery. */
export function buildWebhookCreate(body: Payload): WebhookPayloadResult {
  const errors: string[] = [];
  const payload: Payload = {};
  const url = checkWebhookUrl(body.url);
  if (url.ok) payload.url = url.value;
  else errors.push(url.error);
  const types = checkWebhookEventTypes(body.event_types);
  if (types.ok) payload.event_types = types.value;
  else errors.push(types.error);
  if (body.description !== undefined) {
    const description = checkOptionalText(body.description, "Description", 200);
    if (description.ok) payload.description = description.value;
    else errors.push(description.error);
  }
  for (const key of Object.keys(body)) {
    if (!["url", "event_types", "description"].includes(key)) errors.push(`Unknown field: ${key}`);
  }
  return errors.length > 0 ? { ok: false, error: errors.join("; ") } : { ok: true, payload };
}

export function buildWebhookUpdate(body: Payload): WebhookPayloadResult {
  const errors: string[] = [];
  const payload: Payload = {};
  if (body.url !== undefined) {
    const url = checkWebhookUrl(body.url);
    if (url.ok) payload.url = url.value;
    else errors.push(url.error);
  }
  if (body.event_types !== undefined) {
    const types = checkWebhookEventTypes(body.event_types);
    if (types.ok) payload.event_types = types.value;
    else errors.push(types.error);
  }
  if (body.description !== undefined) {
    const description = checkOptionalText(body.description, "Description", 200);
    if (description.ok) payload.description = description.value;
    else errors.push(description.error);
  }
  if (body.is_active !== undefined) {
    if (typeof body.is_active === "boolean") payload.is_active = body.is_active;
    else errors.push("is_active must be a boolean");
  }
  for (const key of Object.keys(body)) {
    if (!["url", "event_types", "description", "is_active"].includes(key)) errors.push(`Unknown field: ${key}`);
  }
  if (errors.length > 0) return { ok: false, error: errors.join("; ") };
  if (Object.keys(payload).length === 0) return { ok: false, error: "No changes supplied" };
  return { ok: true, payload };
}
