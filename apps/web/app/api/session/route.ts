import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import { checkSameOrigin } from "@/lib/server/route";
import { clearSessionCookie } from "@/lib/server/session";

export const dynamic = "force-dynamic";

/** Sign out: clears the session cookie. (Sign-in is wallet-signature only, via /api/auth/verify.) */
export async function DELETE(request: NextRequest): Promise<NextResponse> {
  const blocked = checkSameOrigin(request);
  if (blocked) return blocked;
  const response = new NextResponse(null, { status: 204, headers: { "Cache-Control": "no-store" } });
  clearSessionCookie(response);
  return response;
}
