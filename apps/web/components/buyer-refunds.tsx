"use client";

import { Eye, Loader2, PlugZap, RefreshCw, Search, Undo2, XCircle } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { apiFetch } from "@/lib/api-client";
import { ADDRESS_PREFIX, EXPECTED_AUTH_DOMAIN, IS_TESTNET, KASPA_NETWORK, KASWARE_URL, TESTNET_EXPLORER_URL } from "@/lib/config";
import { formatDateTime, shortHash } from "@/lib/format";
import { getKasware, isSignatureToken, isWalletRejection, kaswareNetworkRefusal, walletErrorText } from "@/lib/kasware";
import { checkRefundChallenge } from "@/lib/refund-challenge";
import { CREDIT_REASON_LABEL } from "@/lib/credit-labels";
import { refundErrorMessage } from "@/lib/refund-errors";
import { claimExpiresAt } from "@/lib/refund-payout";
import { formatKas, parseKasToSompi } from "@/lib/sompi";
import { usePricing } from "@/lib/use-pricing";
import type { BuyerCreditSummary, PrepaidBalanceRead, PrepaidRefundChallenge, SignedRefundStatus, PrepaidRefundChallengeRequest, PrepaidRefundRead, PrepaidRefundSigned } from "@/lib/types";
import { checkKaspaAddress, checkSlug } from "@/lib/validation";
import { KasAmount } from "./kas-amount";
import { LatePayoutNote, REFUND_STATUS_HELP, RefundStatusBadge } from "./refund-status";
import { ErrorBanner, Field, Spinner } from "./ui";

/** A P2PK Schnorr address (`<prefix>:q…`): only its key can sign refund requests. */
function isP2pkAddress(address: string): boolean {
  return address.startsWith(`${ADDRESS_PREFIX}q`);
}

/** `signed`: the rows came from a signed status read, so they carry the merchant's reasons and payout errors. */
type Lookup = { slug: string; address: string; balance: PrepaidBalanceRead; refunds: PrepaidRefundRead[]; signed: boolean; credits: BuyerCreditSummary | null };
type Busy = null | "connect" | "lookup" | "request" | "status" | `cancel:${string}`;

export function BuyerRefunds({ initialSlug }: { initialSlug: string }) {
  const pricing = usePricing();
  const [hasWallet, setHasWallet] = useState(false);
  const [slug, setSlug] = useState(initialSlug);
  const [address, setAddress] = useState("");
  const [connected, setConnected] = useState<string | null>(null);
  const [lookup, setLookup] = useState<Lookup | null>(null);
  const [amount, setAmount] = useState("");
  const [amountError, setAmountError] = useState<string | null>(null);
  const [busy, setBusy] = useState<Busy>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    const detect = () => setHasWallet(getKasware() !== null);
    detect();
    window.addEventListener("kasware#initialized", detect);
    const timer = window.setTimeout(detect, 800);
    return () => {
      window.removeEventListener("kasware#initialized", detect);
      window.clearTimeout(timer);
    };
  }, []);

  const load = useCallback(async (slugValue: string, addressValue: string): Promise<Lookup> => {
    const query = `slug=${encodeURIComponent(slugValue)}&payer_address=${encodeURIComponent(addressValue)}`;
    const [balance, refunds] = await Promise.all([
      apiFetch<PrepaidBalanceRead>(`/api/prepaid/balance?${query}`),
      apiFetch<PrepaidRefundRead[]>(`/api/prepaid/refunds/status?${query}&limit=50`),
    ]);
    const next = { slug: slugValue, address: addressValue, balance, refunds, signed: false, credits: null };
    setLookup(next);
    return next;
  }, []);

  function inputs(): { slug: string; address: string } | null {
    const s = checkSlug(slug);
    if (!s.ok) {
      setError(s.error);
      return null;
    }
    const a = checkKaspaAddress(address);
    if (!a.ok) {
      setError(a.error);
      return null;
    }
    if (!isP2pkAddress(a.value)) {
      setError(`Refunds go to the depositing key's address, which starts with "${ADDRESS_PREFIX}q".`);
      return null;
    }
    return { slug: s.value, address: a.value };
  }

  async function connect() {
    const wallet = getKasware();
    if (!wallet) {
      setError("KasWare was not detected in this browser.");
      return;
    }
    setBusy("connect");
    setError(null);
    try {
      const accounts = await wallet.requestAccounts();
      const account = accounts[0];
      if (!account) throw new Error("KasWare did not return an account");
      const wrongNetwork = await kaswareNetworkRefusal(wallet, KASPA_NETWORK);
      if (wrongNetwork) throw new Error(wrongNetwork);
      const checked = checkKaspaAddress(account);
      if (!checked.ok) throw new Error(`KasWare account: ${checked.error}`);
      setConnected(checked.value);
      setAddress(checked.value);
    } catch (e) {
      setError(isWalletRejection(e) ? "You declined the KasWare connection." : walletErrorText(e));
    } finally {
      setBusy(null);
    }
  }

  async function lookUp(event?: FormEvent) {
    event?.preventDefault();
    setNotice(null);
    const values = inputs();
    if (!values) return;
    setBusy("lookup");
    setError(null);
    try {
      await load(values.slug, values.address);
    } catch (e) {
      setLookup(null);
      setError(refundErrorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  /** Challenge -> line-by-line check -> KasWare signMessage (Schnorr, depositing key) -> POST {nonce, signature}. */
  async function signAndSend<T>(request: PrepaidRefundChallengeRequest, submitPath: string, expected: { amountSompi?: string; refundId?: string }): Promise<T> {
    const wallet = getKasware();
    if (!wallet) throw new Error("KasWare was not detected in this browser. Refund requests are signed with the depositing KasWare account (or the buyer SDK).");
    const accounts = await wallet.requestAccounts();
    const account = accounts[0]?.trim().toLowerCase();
    if (account !== request.payer_address) throw new Error("The connected KasWare account is not this address. Switch accounts in KasWare and connect again.");
    const wrongNetwork = await kaswareNetworkRefusal(wallet, KASPA_NETWORK);
    if (wrongNetwork) throw new Error(wrongNetwork);
    const challenge = await apiFetch<PrepaidRefundChallenge>("/api/prepaid/refunds/challenge", { method: "POST", body: request });
    const refusal = checkRefundChallenge(challenge, { action: request.action, address: request.payer_address, domain: EXPECTED_AUTH_DOMAIN, ...expected });
    if (refusal) throw new Error(refusal);
    let signature: string;
    try {
      signature = await wallet.signMessage(challenge.message, { type: "schnorr" });
    } catch (e) {
      if (isWalletRejection(e)) throw new Error("You declined to sign in KasWare. Nothing was changed.");
      throw new Error(`KasWare: ${walletErrorText(e)}`);
    }
    if (!isSignatureToken(signature)) throw new Error("KasWare returned an unexpected signature.");
    const body: PrepaidRefundSigned = { nonce: challenge.nonce, signature };
    return apiFetch<T>(submitPath, { method: "POST", body });
  }

  async function requestRefund(event: FormEvent) {
    event.preventDefault();
    setNotice(null);
    if (!lookup) return;
    const refundable = BigInt(lookup.balance.refundable_sompi);
    let amountSompi: string | undefined;
    if (amount.trim() !== "") {
      const parsed = parseKasToSompi(amount);
      if (parsed === null) {
        setAmountError("Enter an amount in KAS, e.g. 1.5 (up to 8 decimals), or leave it blank for all spendable credit");
        return;
      }
      if (parsed > refundable) {
        setAmountError(
          BigInt(lookup.balance.credit_balance_sompi) > 0n
            ? `You can get back at most ${formatKas(refundable)} KAS. Credit from failed calls only pays for calls and is never refunded.`
            : `You can get back at most ${formatKas(refundable)} KAS (your refundable credit)`,
        );
        return;
      }
      amountSompi = parsed.toString();
    }
    const effective = amountSompi === undefined ? refundable : BigInt(amountSompi);
    if (pricing.status === "ready" && effective < pricing.minOutput) {
      setAmountError(`A refund must be at least ${formatKas(pricing.minOutput)} KAS (the smallest standard Kaspa output). Spend smaller amounts on calls instead.`);
      return;
    }
    setAmountError(null);
    setBusy("request");
    setError(null);
    try {
      const request: PrepaidRefundChallengeRequest = { slug: lookup.slug, payer_address: lookup.address, action: "request" };
      if (amountSompi !== undefined) request.amount_sompi = amountSompi;
      const refund = await signAndSend<PrepaidRefundRead>(request, "/api/prepaid/refunds", { amountSompi });
      setAmount("");
      await load(lookup.slug, lookup.address);
      setNotice(`Refund of ${formatKas(refund.amount_sompi)} KAS requested. The merchant pays it to ${refund.payout_address}.`);
    } catch (e) {
      setError(refundErrorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  async function cancelRefund(refund: PrepaidRefundRead) {
    if (!lookup) return;
    setNotice(null);
    setBusy(`cancel:${refund.id}`);
    setError(null);
    try {
      await signAndSend<PrepaidRefundRead>({ slug: lookup.slug, payer_address: lookup.address, action: "cancel", refund_id: refund.id }, `/api/prepaid/refunds/${refund.id}/cancel`, { refundId: refund.id });
      await load(lookup.slug, lookup.address);
      setNotice("Refund request withdrawn. The held credit is spendable again.");
    } catch (e) {
      setError(refundErrorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  /** Signed `status` read: the same rows with the merchant's reject reasons and payout errors (the public view hides them). */
  async function showReasons() {
    if (!lookup) return;
    setNotice(null);
    setBusy("status");
    setError(null);
    try {
      const reply = await signAndSend<SignedRefundStatus>({ slug: lookup.slug, payer_address: lookup.address, action: "status" }, "/api/prepaid/refunds/status", {});
      setLookup({ ...lookup, refunds: reply.refunds, signed: true, credits: reply.credit_summary });
    } catch (e) {
      setError(refundErrorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  const now = Date.now();
  const canSign = connected !== null && lookup !== null && connected === lookup.address;
  const openRefund = lookup?.refunds.find((r) => r.status === "requested" || r.status === "paying" || r.status === "payout_submitted") ?? null;
  const balance = lookup?.balance ?? null;
  const expired = balance?.expires_at ? Date.parse(balance.expires_at) <= Date.now() : false;

  return (
    <div className="mt-8 space-y-6">
      <form onSubmit={lookUp} className="card space-y-4 p-5" noValidate>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Endpoint slug" htmlFor="refund-slug" hint="Any endpoint of the merchant you deposited with (the part after /p/).">
            <input id="refund-slug" className="input font-mono" value={slug} onChange={(e) => setSlug(e.target.value)} placeholder="weather-api" autoComplete="off" spellCheck={false} />
          </Field>
          <Field label="Your depositing address" htmlFor="refund-address" hint={connected ? "Connected with KasWare." : "Connect KasWare to sign, or paste an address to only view status."}>
            <input
              id="refund-address"
              className="input font-mono"
              value={address}
              onChange={(e) => {
                setAddress(e.target.value);
                setConnected(null);
              }}
              placeholder={`${ADDRESS_PREFIX}q…`}
              autoComplete="off"
              spellCheck={false}
            />
          </Field>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" className="btn-secondary" onClick={() => void connect()} disabled={busy !== null || !hasWallet}>
            {busy === "connect" ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : <PlugZap className="h-4 w-4" aria-hidden />}
            {connected ? "Reconnect KasWare" : "Connect KasWare"}
          </button>
          <button type="submit" className="btn-primary" disabled={busy !== null}>
            {busy === "lookup" ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : <Search className="h-4 w-4" aria-hidden />}
            Show balance &amp; refunds
          </button>
          {!hasWallet ? (
            <span className="text-xs text-zinc-500">
              KasWare not detected.{" "}
              <a href={KASWARE_URL} className="text-kas-300 hover:underline" target="_blank" rel="noreferrer">
                Install it
              </a>{" "}
              to sign refund requests, or use the buyer SDK (<code className="font-mono">prepaid_refund_request</code>).
            </span>
          ) : null}
        </div>
      </form>

      <ErrorBanner message={error} />
      {notice ? <p className="rounded-lg border border-emerald-900/70 bg-emerald-950/30 px-3 py-2 text-sm text-emerald-300">{notice}</p> : null}

      {lookup && balance ? (
        <>
          <section className="card p-5" aria-labelledby="buyer-balance-heading">
            <div className="flex items-center justify-between">
              <h2 id="buyer-balance-heading" className="text-sm font-medium text-zinc-100">
                Credit at the merchant behind <span className="font-mono text-kas-300">{lookup.slug}</span>
              </h2>
              <button type="button" className="btn-ghost px-2 py-1 text-xs" onClick={() => void lookUp()} disabled={busy !== null} aria-label="Reload balance and refunds">
                <RefreshCw className="h-3.5 w-3.5" aria-hidden />
              </button>
            </div>
            <dl className="mt-4 grid gap-4 sm:grid-cols-3 lg:grid-cols-6">
              <div>
                <dt className="label">Balance</dt>
                <dd>
                  <KasAmount sompi={balance.balance_sompi} align="left" tone="text-zinc-100" />
                </dd>
              </div>
              <div>
                <dt className="label">Spendable</dt>
                <dd>
                  <KasAmount sompi={balance.spendable_sompi} align="left" tone="text-emerald-300" />
                </dd>
              </div>
              <div>
                <dt className="label">Held by a refund request</dt>
                <dd>
                  <KasAmount sompi={balance.refund_hold_sompi} align="left" tone="text-amber-300" />
                </dd>
              </div>
              <div>
                <dt className="label">Refundable</dt>
                <dd>
                  <KasAmount sompi={balance.refundable_sompi} align="left" tone="text-zinc-100" />
                </dd>
              </div>
              <div>
                <dt className="label" title="Credit from failed paid calls: spent first by calls, never refunded on-chain">Call-only credit</dt>
                <dd>
                  <KasAmount sompi={balance.credit_balance_sompi} align="left" tone={balance.credit_balance_sompi === "0" ? "text-zinc-500" : "text-violet-300"} />
                </dd>
              </div>
              <div>
                <dt className="label">Expires</dt>
                <dd className={`font-mono text-xs ${expired ? "text-red-300" : "text-zinc-300"}`}>{balance.expires_at ? formatDateTime(balance.expires_at) : "never"}</dd>
                {expired ? <p className="mt-1 text-[11px] text-red-300/80">This credit has expired and can no longer be spent or refunded.</p> : null}
              </div>
            </dl>
            {BigInt(balance.credited_sompi) > 0n ? (
              <p className="mt-3 text-xs text-violet-300/90" data-credited>
                {formatKas(balance.credited_sompi)} KAS has been credited here from paid calls that failed. That credit pays for calls first and is never refunded
                on-chain.
              </p>
            ) : null}
          </section>

          <section className="card space-y-3 p-5" aria-labelledby="buyer-request-heading">
            <h2 id="buyer-request-heading" className="text-sm font-medium text-zinc-100">
              Ask for a refund
            </h2>
            {openRefund ? (
              <p className="text-xs text-zinc-400">You already have an open refund request for this balance. Withdraw it below or wait for the merchant before asking again.</p>
            ) : (
              <form onSubmit={requestRefund} className="flex flex-col gap-3 sm:flex-row sm:items-start" noValidate>
                <div className="flex-1">
                  <Field
                    label="Amount (KAS)"
                    htmlFor="refund-amount"
                    error={amountError}
                    hint={`Blank = all refundable credit (${formatKas(balance.refundable_sompi)} KAS). Minimum ${pricing.status === "ready" ? formatKas(pricing.minOutput) : "0.20"} KAS.`}
                  >
                    <input id="refund-amount" className="input font-mono" value={amount} onChange={(e) => setAmount(e.target.value)} placeholder={formatKas(balance.refundable_sompi)} inputMode="decimal" autoComplete="off" />
                  </Field>
                </div>
                <button type="submit" className="btn-primary sm:mt-6" disabled={busy !== null || !canSign}>
                  {busy === "request" ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : <Undo2 className="h-4 w-4" aria-hidden />}
                  Sign &amp; request refund
                </button>
              </form>
            )}
            {!canSign ? <p className="text-xs text-zinc-500">Connect the KasWare account that made the deposit to sign requests.</p> : null}
          </section>

          <section className="space-y-2" aria-labelledby="buyer-refunds-heading">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 id="buyer-refunds-heading" className="text-sm font-medium text-zinc-100">
                Your refund requests
              </h2>
              {lookup.refunds.length > 0 ? (
                <button type="button" className="btn-secondary px-3 py-1.5 text-xs" onClick={() => void showReasons()} disabled={busy !== null || !canSign} title={canSign ? undefined : "Connect the depositing KasWare account to sign"}>
                  {busy === "status" ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> : <Eye className="h-3.5 w-3.5" aria-hidden />}
                  {lookup.signed ? "Refresh with reasons" : "Sign to see the merchant's reasons"}
                </button>
              ) : null}
            </div>
            {lookup.signed ? <p className="text-xs text-zinc-500" data-signed-status>Showing your signed view: merchant reasons and payout problems are included.</p> : null}
            <div className="card overflow-x-auto">
              <table className="w-full min-w-[720px]">
                <thead>
                  <tr className="border-b border-zinc-800">
                    <th className="th">Requested (UTC)</th>
                    <th className="th text-right">Amount</th>
                    <th className="th">Status</th>
                    <th className="th">Payout tx</th>
                    <th className="th text-right" />
                  </tr>
                </thead>
                <tbody className="divide-y divide-zinc-800/70">
                  {lookup.refunds.map((refund) => (
                    <tr key={refund.id} className="align-top" data-refund-id={refund.id}>
                      <td className="td whitespace-nowrap font-mono text-xs">{formatDateTime(refund.created_at)}</td>
                      <td className="td">
                        <KasAmount sompi={refund.amount_sompi} tone="text-kas-300" />
                      </td>
                      <td className="td">
                        <RefundStatusBadge status={refund.status} />
                        <p className="mt-1 max-w-[280px] text-[11px] text-zinc-500">{REFUND_STATUS_HELP.buyer[refund.status]}</p>
                        {refund.reason ? <p className="mt-1 max-w-[280px] text-[11px] text-zinc-300" data-reject-reason>Merchant&apos;s reason: {refund.reason}</p> : null}
                        {refund.status === "rejected" && !lookup.signed ? <p className="mt-1 max-w-[280px] text-[11px] text-zinc-500">Sign to see the merchant&apos;s reason.</p> : null}
                        {refund.last_error && lookup.signed && refund.status !== "paid" ? <p className="mt-1 max-w-[280px] break-all text-[11px] text-amber-300/80">Payout problem: {refund.last_error}</p> : null}
                        <LatePayoutNote refund={refund} />
                      </td>
                      <td className="td">
                        {refund.tx_id ? (
                          IS_TESTNET ? (
                            <a href={`${TESTNET_EXPLORER_URL}/txs/${refund.tx_id}`} target="_blank" rel="noreferrer" className="font-mono text-xs text-kas-300 hover:underline" title={refund.tx_id}>
                              {shortHash(refund.tx_id)}
                            </a>
                          ) : (
                            <span className="font-mono text-xs" title={refund.tx_id}>
                              {shortHash(refund.tx_id)}
                            </span>
                          )
                        ) : (
                          <span className="text-xs text-zinc-600">—</span>
                        )}
                      </td>
                      <td className="td text-right">
                        {refund.status === "requested" || refund.status === "paying" ? (
                          (() => {
                            const claimEnds = claimExpiresAt(refund);
                            const claimed = claimEnds !== null && claimEnds > now;
                            return (
                              <>
                                <button
                                  type="button"
                                  className="btn-ghost px-3 py-1.5 text-xs text-red-300"
                                  onClick={() => void cancelRefund(refund)}
                                  disabled={busy !== null || !canSign || claimed}
                                  title={claimed ? "The merchant is paying this refund right now" : undefined}
                                >
                                  {busy === `cancel:${refund.id}` ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> : <XCircle className="h-3.5 w-3.5" aria-hidden />}
                                  Withdraw request
                                </button>
                                {claimed && claimEnds !== null ? (
                                  <p className="mt-1 text-[11px] text-zinc-500">Can be withdrawn after {new Date(claimEnds).toISOString().slice(11, 16)} UTC if no payout arrives.</p>
                                ) : null}
                              </>
                            );
                          })()
                        ) : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {lookup.refunds.length === 0 ? <div className="td py-6 text-center text-zinc-500">No refund requests yet.</div> : null}
            </div>
            <p className="text-xs text-zinc-500">
              The merchant pays a refund on-chain to exactly this address for exactly the requested amount. Your balance drops only once that payment is seen on
              the network.
            </p>
          </section>

          {lookup.credits ? (
            <section className="space-y-2" aria-labelledby="buyer-credits-heading" data-credit-summary>
              <h2 id="buyer-credits-heading" className="text-sm font-medium text-zinc-100">
                Credit from failed calls
              </h2>
              <div className="card space-y-3 p-4">
                <dl className="grid gap-4 sm:grid-cols-3">
                  <div>
                    <dt className="label">Credited in total</dt>
                    <dd>
                      <KasAmount sompi={lookup.credits.creditedSompi} align="left" tone="text-violet-300" />
                    </dd>
                  </div>
                  <div>
                    <dt className="label">Unspent (call-only)</dt>
                    <dd>
                      <KasAmount sompi={lookup.credits.creditBalanceSompi} align="left" tone="text-zinc-100" />
                    </dd>
                  </div>
                  <div>
                    <dt className="label">Refundable now</dt>
                    <dd>
                      <KasAmount sompi={lookup.credits.refundableSompi} align="left" tone="text-zinc-100" />
                    </dd>
                  </div>
                </dl>
                {lookup.credits.credits.length > 0 ? (
                  <table className="w-full min-w-[640px] text-left">
                    <thead>
                      <tr className="border-b border-zinc-800">
                        <th className="th">Credited (UTC)</th>
                        <th className="th">Endpoint</th>
                        <th className="th">Why</th>
                        <th className="th text-right">Credited</th>
                        <th className="th text-right">Not credited (cap)</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-zinc-800/70">
                      {lookup.credits.credits.map((credit) => (
                        <tr key={credit.txId} data-buyer-credit={credit.txId}>
                          <td className="td whitespace-nowrap font-mono text-xs">{formatDateTime(credit.createdAt)}</td>
                          <td className="td font-mono text-xs">
                            {credit.slug}
                            <div className="text-[11px] text-zinc-600" title={credit.txId}>
                              call {shortHash(credit.txId)}
                            </div>
                          </td>
                          <td className="td text-xs text-zinc-400">{CREDIT_REASON_LABEL[credit.reason]}</td>
                          <td className="td">
                            <KasAmount sompi={credit.creditSompi} tone="text-violet-300" />
                          </td>
                          <td className="td">
                            <KasAmount sompi={credit.shortfallSompi} tone={credit.shortfallSompi === "0" ? "text-zinc-600" : "text-amber-300"} />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <p className="text-xs text-zinc-500">No paid call to this merchant has been credited.</p>
                )}
                {lookup.credits.creditCount > lookup.credits.credits.length ? (
                  <p className="text-[11px] text-zinc-500">
                    Showing the newest {lookup.credits.credits.length} of {lookup.credits.creditCount} credits.
                  </p>
                ) : null}
                <p className="text-[11px] text-zinc-500">
                  When a paid call fails for good, the merchant&apos;s share comes back to you as credit here. The platform fee is not returned.
                </p>
              </div>
            </section>
          ) : null}
        </>
      ) : busy === "lookup" ? (
        <Spinner label="Loading…" />
      ) : null}
    </div>
  );
}
