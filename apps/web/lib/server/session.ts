import "server-only";
import { cookies } from "next/headers";
import type { NextResponse } from "next/server";
import { SESSION_COOKIE, SESSION_MAX_AGE_SECONDS } from "../config";

/** Secure unless KASGATE_COOKIE_SECURE is explicitly "false" / "0" / "no" (default: true). */
const COOKIE_SECURE = !["false", "0", "no"].includes((process.env.KASGATE_COOKIE_SECURE ?? "true").trim().toLowerCase());

export const API_KEY_RE = /^kg_[A-Za-z0-9_-]{16,200}$/;

export async function readApiKey(): Promise<string | null> {
  const store = await cookies();
  const value = store.get(SESSION_COOKIE)?.value;
  return value && API_KEY_RE.test(value) ? value : null;
}

export function setSessionCookie(response: NextResponse, apiKey: string): void {
  response.cookies.set({
    name: SESSION_COOKIE,
    value: apiKey,
    httpOnly: true,
    secure: COOKIE_SECURE,
    sameSite: "lax",
    path: "/",
    maxAge: SESSION_MAX_AGE_SECONDS,
  });
}

export function clearSessionCookie(response: NextResponse): void {
  response.cookies.set({
    name: SESSION_COOKIE,
    value: "",
    httpOnly: true,
    secure: COOKIE_SECURE,
    sameSite: "lax",
    path: "/",
    maxAge: 0,
  });
}
