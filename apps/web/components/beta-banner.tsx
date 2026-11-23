"use client";

import Link from "next/link";
import { FlaskConical, X } from "lucide-react";
import { BETA_BANNER_ATTRIBUTE, BETA_BANNER_STORAGE_KEY } from "@/lib/beta";

/** Site-wide testnet notice. Visibility is driven by the <html> attribute so it is correct before hydration. */
export function BetaBanner() {
  function dismiss() {
    document.documentElement.setAttribute(BETA_BANNER_ATTRIBUTE, "");
    try {
      window.localStorage.setItem(BETA_BANNER_STORAGE_KEY, "1");
    } catch {
      // Storage unavailable (private mode or blocked): the banner stays hidden for this page view only.
    }
  }

  return (
    <div role="region" aria-label="Testnet beta notice" className="beta-banner border-b border-amber-900/60 bg-amber-950/40 text-amber-200">
      <div className="mx-auto flex max-w-6xl items-center gap-3 px-4 py-2 text-sm">
        <FlaskConical className="h-4 w-4 shrink-0 text-amber-300" aria-hidden />
        <p className="flex-1">
          Testnet beta — payments use free testnet KAS (TN10). Not real money.{" "}
          <Link href="/beta" className="font-medium text-amber-100 underline decoration-amber-500/60 underline-offset-2 hover:decoration-amber-300">
            Tester guide
          </Link>
        </p>
        <button type="button" onClick={dismiss} className="rounded-md p-1 text-amber-300/80 transition hover:bg-amber-900/50 hover:text-amber-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-400" aria-label="Dismiss testnet beta notice">
          <X className="h-4 w-4" aria-hidden />
        </button>
      </div>
    </div>
  );
}
