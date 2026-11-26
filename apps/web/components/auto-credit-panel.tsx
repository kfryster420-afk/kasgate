"use client";

import { ChevronLeft, ChevronRight, LifeBuoy, RefreshCw, Search } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { ApiError, apiFetch, errorMessage } from "@/lib/api-client";
import { IS_TESTNET, TESTNET_EXPLORER_URL } from "@/lib/config";
import { CREDIT_REASON_LABEL, DELIVERY_FAILURE_LABEL } from "@/lib/credit-labels";
import { formatDateTime, formatUsd, shortAddress, shortHash } from "@/lib/format";
import type { FailedDeliveryPolicyRead, FailedDeliveryPolicyUpdate, PrepaidCreditWithUsd } from "@/lib/types";
import { usdForSompi } from "@/lib/usd-at-settlement";
import { checkSlug } from "@/lib/validation";
import { KasAmount } from "./kas-amount";
import { ErrorBanner, Spinner, Toggle } from "./ui";

const PAGE_SIZE = 25;

function txLink(txId: string) {
  return IS_TESTNET ? (
    <a href={`${TESTNET_EXPLORER_URL}/txs/${txId}`} target="_blank" rel="noreferrer" className="font-mono text-xs text-kas-300 hover:underline" title={txId}>
      {shortHash(txId)}
    </a>
  ) : (
    <span className="font-mono text-xs" title={txId}>
      {shortHash(txId)}
    </span>
  );
}

function FailedDeliveryPolicy({ onUnauthorized }: { onUnauthorized: () => void }) {
  const [policy, setPolicy] = useState<FailedDeliveryPolicyRead | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  useEffect(() => {
    apiFetch<FailedDeliveryPolicyRead>("/api/prepaid/failed-delivery-policy")
      .then(setPolicy)
      .catch((e: unknown) => {
        if (e instanceof ApiError && e.status === 401) onUnauthorized();
        else setError(errorMessage(e));
      });
  }, [onUnauthorized]);

  async function save(update: FailedDeliveryPolicyUpdate) {
    if (busy) return;
    setBusy(true);
    setError(null);
    setSaved(null);
    try {
      const next = await apiFetch<FailedDeliveryPolicyRead>("/api/prepaid/failed-delivery-policy", { method: "PUT", body: update });
      setPolicy(next);
      setSaved(
        next.enabled
          ? `Saved: failed paid calls settling from now on are credited${next.credit_timeouts ? ", timeouts included" : " (timeouts excluded)"}.`
          : "Saved: auto-credit is off for calls settling from now on. Calls already paid keep the policy they settled under.",
      );
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) onUnauthorized();
      else setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card space-y-3 p-4" aria-labelledby="auto-credit-policy-heading" role="group">
      <h3 id="auto-credit-policy-heading" className="text-sm font-medium text-zinc-100">
        Auto-credit policy
      </h3>
      {policy === null && !error ? <Spinner label="Loading policy…" /> : null}
      {policy ? (
        <div className="space-y-3" data-policy-enabled={policy.enabled ? "true" : "false"}>
          <Toggle id="auto-credit-enabled" checked={policy.enabled} onChange={(next) => void save({ enabled: next })} label="Credit failed paid calls to the buyer's prepaid balance" />
          <Toggle
            id="auto-credit-timeouts"
            checked={policy.credit_timeouts}
            onChange={(next) => void save({ credit_timeouts: next })}
            label="Include upstream timeouts (5xx and connection errors are always credited while on)"
          />
          <p className="text-xs text-zinc-500">
            {policy.enabled ? `On since ${formatDateTime(policy.enabled_at)}.` : `Off since ${policy.disabled_at ? formatDateTime(policy.disabled_at) : "—"}.`} The policy in
            force when a call settles is stamped on it, so a change never adds or removes credit for calls already paid. Responses too large to relay are
            never credited.
          </p>
        </div>
      ) : null}
      {saved ? <p className="text-xs text-emerald-300">{saved}</p> : null}
      <ErrorBanner message={error} />
    </div>
  );
}

/**
 * Failed-delivery auto-credit (CONTRACT v2.14): when a paid call's upstream fails for good, the merchant share becomes
 * call-only prepaid credit for that buyer. The policy switch, and the credits ledger (GET /v1/prepaid/credits).
 */
export function AutoCreditPanel({ reloadKey, onUnauthorized }: { reloadKey: number; onUnauthorized: () => void }) {
  const [credits, setCredits] = useState<PrepaidCreditWithUsd[]>([]);
  const [page, setPage] = useState(0);
  const [slugInput, setSlugInput] = useState("");
  const [slug, setSlug] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [localReload, setLocalReload] = useState(0);

  const load = useCallback(async () => {
    const query = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(page * PAGE_SIZE) });
    if (slug) query.set("slug", slug);
    return apiFetch<PrepaidCreditWithUsd[]>(`/api/prepaid/credits?${query.toString()}`);
  }, [page, slug]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    load()
      .then((rows) => {
        if (!cancelled) setCredits(rows);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        if (e instanceof ApiError && e.status === 401) onUnauthorized();
        else setError(errorMessage(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [load, reloadKey, localReload, onUnauthorized]);

  function filter(event: FormEvent) {
    event.preventDefault();
    const value = slugInput.trim().toLowerCase();
    if (value !== "") {
      const checked = checkSlug(value);
      if (!checked.ok) {
        setError(checked.error);
        return;
      }
    }
    setPage(0);
    setSlug(value);
  }

  return (
    <section className="space-y-3" aria-labelledby="auto-credit-heading">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h2 id="auto-credit-heading" className="flex items-center gap-2 text-lg font-medium text-zinc-100">
            <LifeBuoy className="h-4 w-4 text-violet-300" aria-hidden />
            Failed-call credits
          </h2>
          <p className="max-w-3xl text-xs text-zinc-500">
            When a paid call&apos;s upstream fails for good (redelivery used up or its window closed), your share of that payment becomes prepaid credit
            for the buyer at your endpoints. It only pays for calls, is never refunded on-chain, and nothing moves on-chain. The platform fee is not
            reversed.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <form onSubmit={filter} className="flex items-center gap-1">
            <label htmlFor="credit-slug-filter" className="sr-only">
              Filter by endpoint slug
            </label>
            <input id="credit-slug-filter" className="input w-44 py-1.5 font-mono text-xs" value={slugInput} onChange={(e) => setSlugInput(e.target.value)} placeholder="endpoint slug" autoComplete="off" spellCheck={false} />
            <button type="submit" className="btn-secondary px-2 py-1.5" aria-label="Filter credits">
              <Search className="h-4 w-4" aria-hidden />
            </button>
          </form>
          <button type="button" className="btn-secondary px-2 py-1.5" onClick={() => setLocalReload((k) => k + 1)} disabled={loading} aria-label="Reload credits">
            <RefreshCw className="h-4 w-4" aria-hidden />
          </button>
          <button type="button" className="btn-secondary px-2 py-1.5" onClick={() => setPage((p) => Math.max(0, p - 1))} disabled={page === 0 || loading} aria-label="Previous credits page">
            <ChevronLeft className="h-4 w-4" aria-hidden />
          </button>
          <span className="font-mono text-xs text-zinc-400">page {page + 1}</span>
          <button type="button" className="btn-secondary px-2 py-1.5" onClick={() => setPage((p) => p + 1)} disabled={credits.length < PAGE_SIZE || loading} aria-label="Next credits page">
            <ChevronRight className="h-4 w-4" aria-hidden />
          </button>
        </div>
      </div>
      <ErrorBanner message={error} />
      <div className="card overflow-x-auto">
        <table className="w-full min-w-[1300px]">
          <thead>
            <tr className="border-b border-zinc-800">
              <th className="th">Credited (UTC)</th>
              <th className="th">Buyer</th>
              <th className="th">Endpoint · call tx</th>
              <th className="th">Why</th>
              <th className="th text-right">Paid on-chain</th>
              <th className="th text-right">Platform fee (kept)</th>
              <th className="th text-right">Credited (call-only)</th>
              <th className="th text-right" title="Credited KAS at the KAS/USD rate the call settled at">USD at settlement</th>
              <th className="th text-right">Not credited (cap)</th>
              <th className="th text-right">Balance after</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-800/70">
            {credits.map((credit) => (
              <tr key={credit.id} className="align-top hover:bg-zinc-900/60" data-credit-id={credit.id}>
                <td className="td whitespace-nowrap font-mono text-xs">
                  {formatDateTime(credit.created_at)}
                  {credit.settled_at ? <div className="text-[11px] text-zinc-600">paid {formatDateTime(credit.settled_at)}</div> : null}
                </td>
                <td className="td font-mono text-xs" title={credit.payer_address}>
                  {shortAddress(credit.payer_address)}
                </td>
                <td className="td">
                  <div className="font-mono text-xs text-zinc-200">{credit.slug}</div>
                  {txLink(credit.tx_id)}
                  <div className="font-mono text-[11px] text-zinc-600">{credit.payment_scheme}</div>
                </td>
                <td className="td text-xs">
                  <span className="badge border-violet-800 bg-violet-950/50 text-violet-300">{DELIVERY_FAILURE_LABEL[credit.delivery_failure]}</span>
                  {credit.upstream_status_code !== null ? <span className="ml-1 font-mono text-zinc-400">HTTP {credit.upstream_status_code}</span> : null}
                  <div className="mt-1 text-[11px] text-zinc-500">
                    {CREDIT_REASON_LABEL[credit.reason]} · {credit.delivery_attempts} deliver{credit.delivery_attempts === 1 ? "y" : "ies"}
                  </div>
                </td>
                <td className="td">
                  <KasAmount sompi={credit.amount_sompi} />
                </td>
                <td className="td">
                  <KasAmount sompi={credit.fee_sompi} tone="text-zinc-400" />
                </td>
                <td className="td">
                  <KasAmount sompi={credit.credit_sompi} tone="text-violet-300" />
                </td>
                <td className="td text-right" data-credit-usd>
                  <CreditUsdCell credit={credit} />
                </td>
                <td className="td">
                  <KasAmount sompi={credit.shortfall_sompi} tone={credit.shortfall_sompi === "0" ? "text-zinc-600" : "text-amber-300"} />
                </td>
                <td className="td">
                  <KasAmount sompi={credit.balance_after_sompi} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!loading && credits.length === 0 ? <div className="td py-8 text-center text-zinc-500">{slug ? "No failed-call credits for this endpoint." : "No failed-call credits yet."}</div> : null}
        {loading ? (
          <div className="border-t border-zinc-800 px-4 py-3">
            <Spinner label="Loading credits…" />
          </div>
        ) : null}
      </div>
      <p className="text-[11px] text-zinc-600">Amounts in KAS and sompi. KasGate records no USD value for these credits.</p>
      <FailedDeliveryPolicy onUnauthorized={onUnauthorized} />
    </section>
  );
}

/** Credited KAS at the KAS/USD rate the credited call settled at (from its settlement), or why there is none. */
function CreditUsdCell({ credit }: { credit: PrepaidCreditWithUsd }) {
  const usd = credit.usd ? usdForSompi(credit.credit_sompi, credit.usd.kas_usd_rate) : null;
  if (usd === null) {
    return (
      <span className="text-xs text-zinc-500" title="The call's settlement has no KAS/USD rate, or it is older than the newest 1,000 settlements this view looks through.">
        no rate
      </span>
    );
  }
  return (
    <div className="font-mono text-xs text-zinc-200" title={`Rate $${credit.usd?.kas_usd_rate ?? ""} per KAS at settlement`}>
      {formatUsd(usd)}
      {credit.usd?.call_usd ? <div className="text-[11px] text-zinc-500">call {formatUsd(credit.usd.call_usd)}</div> : null}
    </div>
  );
}
