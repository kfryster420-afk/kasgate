import { KASPA_UNAVAILABLE_MESSAGE } from "./errors";
import type { ApiErrorBody } from "./types";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string | undefined;
  /** Per-row errors keyed by selection index (POST /api/endpoints/import only). */
  readonly rowErrors: Record<string, string>;
  /** Seconds to wait before retrying (from the gateway's Retry-After), when given. */
  readonly retryAfter: number | undefined;

  constructor(message: string, status: number, code?: string, rowErrors: Record<string, string> = {}, retryAfter?: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.rowErrors = rowErrors;
    this.retryAfter = retryAfter;
  }

  get isNodeUnavailable(): boolean {
    return this.code === "rpc_unavailable" || this.status === 503;
  }
}

/** Browser-side fetch against this app's /api/* route handlers (same-origin, cookie attached automatically). */
export async function apiFetch<T>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      method: init.method ?? "GET",
      headers: init.body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: init.body !== undefined ? JSON.stringify(init.body) : undefined,
      credentials: "same-origin",
      cache: "no-store",
    });
  } catch {
    throw new ApiError("Network error — check your connection and try again.", 0);
  }
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  if (!response.ok) {
    let message = `Request failed (HTTP ${response.status})`;
    let code: string | undefined;
    let rowErrors: Record<string, string> = {};
    let retryAfter: number | undefined;
    try {
      const parsed = JSON.parse(text) as Partial<ApiErrorBody>;
      if (typeof parsed.error === "string") message = parsed.error;
      if (typeof parsed.code === "string") code = parsed.code;
      if (parsed.row_errors && typeof parsed.row_errors === "object") rowErrors = parsed.row_errors;
      if (typeof parsed.retry_after === "number" && Number.isInteger(parsed.retry_after) && parsed.retry_after >= 0) retryAfter = parsed.retry_after;
    } catch {
      /* keep generic message */
    }
    if (code === "rpc_unavailable" || response.status === 503) {
      throw new ApiError(KASPA_UNAVAILABLE_MESSAGE, 503, "rpc_unavailable");
    }
    throw new ApiError(message, response.status, code, rowErrors, retryAfter);
  }
  return JSON.parse(text) as T;
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong";
}
