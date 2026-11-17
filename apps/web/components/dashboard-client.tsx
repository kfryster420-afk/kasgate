"use client";

import { KeyRound, LogOut, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { ApiError, apiFetch, errorMessage } from "@/lib/api-client";
import { defaultRange, type DateRange } from "@/lib/range";
import { shortAddress } from "@/lib/format";
import type { EndpointRead, MerchantRead, MerchantRegistered } from "@/lib/types";
import { AnalyticsPanel } from "./analytics-panel";
import { EndpointsPanel } from "./endpoints-panel";
import { SettlementsPanel } from "./settlements-panel";
import { TaxExport } from "./tax-export";
import { ErrorBanner, Spinner } from "./ui";

export function DashboardClient() {
  const [merchant, setMerchant] = useState<MerchantRead | null>(null);
  const [endpoints, setEndpoints] = useState<EndpointRead[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [range, setRange] = useState<DateRange>(() => defaultRange(30));
  const [reloadKey, setReloadKey] = useState(0);
  const [rotating, setRotating] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const toLogin = useCallback(() => {
    window.location.assign("/login");
  }, []);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [me, list] = await Promise.all([apiFetch<MerchantRead>("/api/merchant"), apiFetch<EndpointRead[]>("/api/endpoints")]);
      setMerchant(me);
      setEndpoints(list);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        toLogin();
        return;
      }
      setError(errorMessage(e));
    } finally {
      setLoading(false);
    }
  }, [toLogin]);

  useEffect(() => {
    void load();
  }, [load, reloadKey]);

  async function rotateKey() {
    const confirmed = window.confirm(
      "Rotate your API key?\n\nThe current key stops working immediately. Any scripts or services using it must be updated. This browser session switches to the new key automatically; the key is never displayed.",
    );
    if (!confirmed) return;
    setRotating(true);
    setError(null);
    setNotice(null);
    try {
      const result = await apiFetch<MerchantRegistered>("/api/merchant/rotate-key", { method: "POST" });
      setMerchant(result.merchant);
      setNotice("API key rotated. The old key is now invalid and this session uses the new one.");
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        toLogin();
        return;
      }
      setError(errorMessage(e));
    } finally {
      setRotating(false);
    }
  }

  async function signOut() {
    try {
      await apiFetch<undefined>("/api/session", { method: "DELETE" });
    } finally {
      window.location.assign("/");
    }
  }

  return (
    <div className="mx-auto max-w-6xl space-y-8 px-4 py-8">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold text-zinc-100">{merchant ? merchant.display_name : "Dashboard"}</h1>
          {merchant ? <p className="font-mono text-xs text-zinc-500">payouts → {shortAddress(merchant.kaspa_address)}</p> : null}
        </div>
        <div className="flex gap-2">
          <button type="button" className="btn-secondary" onClick={() => setReloadKey((k) => k + 1)}>
            <RefreshCw className="h-4 w-4" aria-hidden />
            Refresh
          </button>
          <button type="button" className="btn-secondary" onClick={() => void rotateKey()} disabled={rotating || !merchant}>
            <KeyRound className="h-4 w-4" aria-hidden />
            {rotating ? "Rotating…" : "Rotate key"}
          </button>
          <button type="button" className="btn-ghost" onClick={() => void signOut()}>
            <LogOut className="h-4 w-4" aria-hidden />
            Sign out
          </button>
        </div>
      </div>

      <ErrorBanner message={error} />
      {notice ? (
        <div className="rounded-lg border border-emerald-800 bg-emerald-950/40 px-3 py-2 text-sm text-emerald-300" role="status">
          {notice}
        </div>
      ) : null}
      {loading ? <Spinner label="Loading your dashboard…" /> : null}

      {!loading && merchant ? (
        <>
          <TaxExport range={range} onRangeChange={setRange} />
          <AnalyticsPanel range={range} reloadKey={reloadKey} />
          <EndpointsPanel endpoints={endpoints} onChanged={() => setReloadKey((k) => k + 1)} onUnauthorized={toLogin} />
          <SettlementsPanel endpoints={endpoints} reloadKey={reloadKey} />
        </>
      ) : null}
    </div>
  );
}
