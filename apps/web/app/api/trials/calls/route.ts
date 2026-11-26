import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { gatewayRequest } from "@/lib/server/gateway";
import { fromGatewayFailure, isAuth, jsonError, jsonOk, requireSession } from "@/lib/server/route";
import type { TrialCallRead } from "@/lib/types";

export const dynamic = "force-dynamic";

function intParam(value: string | null, fallback: number, min: number, max: number): number | null {
  if (value === null) return fallback;
  if (!/^\d{1,7}$/.test(value)) return null;
  const n = Number(value);
  return n >= min && n <= max ? n : null;
}

/** CONTRACT v2.12: the merchant's free trial call log, newest first. */
export async function GET(request: NextRequest): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const params = request.nextUrl.searchParams;
  const limit = intParam(params.get("limit"), 50, 1, 200);
  const offset = intParam(params.get("offset"), 0, 0, 1_000_000);
  if (limit === null || offset === null) return jsonError("limit must be 1–200 and offset a non-negative integer", 422);
  const result = await gatewayRequest<TrialCallRead[]>("/v1/trials/calls", { bearer: auth.token, query: { limit, offset } });
  return result.ok ? jsonOk(result.data) : fromGatewayFailure(result);
}
