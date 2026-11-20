import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { gatewayRequest } from "@/lib/server/gateway";
import { fromGatewayFailure, isAuth, jsonOk, requireSession } from "@/lib/server/route";
import type { MerchantRead } from "@/lib/types";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const result = await gatewayRequest<MerchantRead>("/v1/merchants/me", { bearer: auth.token });
  return result.ok ? jsonOk(result.data) : fromGatewayFailure(result);
}
