"use client";

import { CalendarClock, Loader2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { ApiError, apiFetch, errorMessage } from "@/lib/api-client";
import { PREPAID_EXPIRY_DAYS_MAX, type MerchantRead, type MerchantUpdate } from "@/lib/types";
import { parseExpiryDaysInput } from "@/lib/validation";
import { ErrorBanner, Field } from "./ui";

/** Merchant default prepaid credit expiry (PATCH /v1/merchants/me, CONTRACT v2.13). Blank = credit never expires. */
export function CreditExpirySettings({ merchant, onSaved, onUnauthorized }: { merchant: MerchantRead; onSaved: (next: MerchantRead) => void; onUnauthorized: () => void }) {
  const [days, setDays] = useState(merchant.prepaid_expiry_days === null ? "" : String(merchant.prepaid_expiry_days));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setSaved(null);
    const parsed = parseExpiryDaysInput(days);
    if (!parsed.ok) {
      setFieldError(parsed.error);
      return;
    }
    setFieldError(null);
    setBusy(true);
    setError(null);
    try {
      const body: MerchantUpdate = { prepaid_expiry_days: parsed.value };
      const next = await apiFetch<MerchantRead>("/api/merchant", { method: "PATCH", body });
      onSaved(next);
      setDays(next.prepaid_expiry_days === null ? "" : String(next.prepaid_expiry_days));
      setSaved(next.prepaid_expiry_days === null ? "Saved: prepaid credit never expires by default." : `Saved: credit expires ${next.prepaid_expiry_days} days after a buyer's latest deposit.`);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) onUnauthorized();
      else setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="card space-y-3 p-4" noValidate aria-labelledby="expiry-heading">
      <div className="flex items-center gap-2">
        <CalendarClock className="h-4 w-4 text-kas-400" aria-hidden />
        <h3 id="expiry-heading" className="text-sm font-medium text-zinc-100">
          Default credit expiry
        </h3>
      </div>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
        <div className="sm:w-72">
          <Field
            label="Days after the latest deposit"
            htmlFor="merchant-expiry-days"
            error={fieldError}
            hint={`Blank = never expires. 1–${PREPAID_EXPIRY_DAYS_MAX.toLocaleString("en-US")} days.`}
          >
            <input id="merchant-expiry-days" className="input font-mono" value={days} onChange={(e) => setDays(e.target.value)} inputMode="numeric" placeholder="never" autoComplete="off" />
          </Field>
        </div>
        <button type="submit" className="btn-secondary sm:mb-6" disabled={busy}>
          {busy ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : null}
          Save expiry
        </button>
      </div>
      <p className="text-xs text-zinc-500">
        Applies to endpoints without their own expiry. A new policy is never retroactive: it takes effect on each buyer&apos;s next deposit, and every deposit pushes the
        expiry date out again. Credit held by an open refund request never expires.
      </p>
      {saved ? <p className="text-xs text-emerald-300">{saved}</p> : null}
      <ErrorBanner message={error} />
    </form>
  );
}
