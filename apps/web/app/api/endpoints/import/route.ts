import { NextResponse, type NextRequest } from "next/server";
import { OPENAPI_MAX_REQUEST_BYTES } from "@/lib/openapi-import";
import { extractErrorCode, extractErrorMessage, gatewayRaw, getMinOutputSompi } from "@/lib/server/gateway";
import { buildImportPayload, gatewayRowErrors, readCappedText } from "@/lib/server/import-input";
import { fromGatewayFailure, isAuth, jsonError, jsonOk, requireSession } from "@/lib/server/route";
import { parseGatewayJson, stringifyWithIntegerKeys } from "@/lib/safe-json";
import type { ApiErrorBody, OpenApiImportCreated, OpenApiImportPreview } from "@/lib/types";

export const dynamic = "force-dynamic";

const NO_STORE = { "Cache-Control": "no-store" } as const;
const FLOOR_UNAVAILABLE = "Cannot verify the minimum price right now: the gateway directory is unreachable. Try again shortly.";

function rowErrorResponse(message: string, status: number, rowErrors: Record<string, string>, code?: string): NextResponse<ApiErrorBody> {
  const body: ApiErrorBody = { error: message, row_errors: rowErrors };
  if (code) body.code = code;
  return NextResponse.json(body, { status, headers: NO_STORE });
}

/**
 * POST /api/endpoints/import -> gateway POST /v1/endpoints/import with the session's `kgs_` bearer.
 * mode "preview" returns OpenApiImportPreview (200); mode "create" returns OpenApiImportCreated (201), all-or-nothing.
 */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;

  const raw = await readCappedText(request);
  if (!raw.ok) return jsonError(`Request body exceeds ${OPENAPI_MAX_REQUEST_BYTES} bytes; the OpenAPI document may be at most 2 MB`, 413);
  let body: unknown;
  try {
    body = JSON.parse(raw.text);
  } catch {
    return jsonError("Request body must be a JSON object", 400);
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) return jsonError("Request body must be a JSON object", 400);

  const floor = await getMinOutputSompi();
  if (floor === null) return jsonError(FLOOR_UNAVAILABLE, 503);
  const built = buildImportPayload(body as Record<string, unknown>, floor);
  if (!built.ok) return built.rowErrors ? rowErrorResponse(built.error, built.status, built.rowErrors) : jsonError(built.error, built.status);

  const response = await gatewayRaw("/v1/endpoints/import", {
    method: "POST",
    bearer: auth.token,
    body: stringifyWithIntegerKeys(built.payload, ["default_price_sompi", "price_sompi", "default_price_usd_micros", "price_usd_micros"]),
  });
  if (!response) return jsonError("The KasGate gateway is unreachable. Try again shortly.", 502);
  const text = await response.text();
  if (!response.ok) {
    const message = extractErrorMessage(text, response.status);
    const code = extractErrorCode(text);
    if (response.status === 422) {
      const rowErrors = gatewayRowErrors(text);
      if (Object.keys(rowErrors).length > 0) return rowErrorResponse(message, 422, rowErrors, code);
    }
    return fromGatewayFailure({ ok: false, status: response.status, message, code });
  }
  try {
    const data = parseGatewayJson<OpenApiImportPreview | OpenApiImportCreated>(text);
    return jsonOk(data, response.status);
  } catch {
    return jsonError("The gateway returned an unreadable response.", 502);
  }
}
