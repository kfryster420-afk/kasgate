"use client";

import { ChevronDown, Eye, Loader2, PlugZap, RefreshCw, Search, Undo2, XCircle } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { ApiError, apiFetch } from "@/lib/api-client";
import { ADDRESS_PREFIX, EXPECTED_AUTH_DOMAIN, IS_TESTNET, KASPA_NETWORK, KASWARE_URL, TESTNET_EXPLORER_URL } from "@/lib/config";
import { formatDateTime, shortHash } from "@/lib/format";
import { getKasware, isSignatureToken, isWalletRejection, kaswareNetworkRefusal, walletErrorText } from "@/lib/kasware";
import { balanceReadMessage, normaliseBalanceSignature } from "@/lib/balance-read";
import { checkRefundChallenge } from "@/lib/refund-challenge";
import { CREDIT_REASON_LABEL } from "@/lib/credit-labels";
import { checkCreditChallenge } from "@/lib/credit-challenge";
import { CREDIT_STATUS_PAGE, parseBuyerCreditStatus } from "@/lib/credit-status";
import { refundErrorMessage, txIdsInMessage } from "@/lib/refund-errors";
import { claimExpiresAt } from "@/lib/refund-payout";
import { formatKas, parseKasToSompi } from "@/lib/sompi";
import { usePricing } from "@/lib/use-pricing";
import type {
  BuyerCreditStatus,
  BuyerCreditStatusItem,
  PrepaidBalanceRead,
  PrepaidCreditStatusChallenge,
  PrepaidCreditStatusRequest,
  PrepaidRefundChallenge,
  PrepaidRefundChallengeRequest,
  PrepaidRefundRead,
  PrepaidRefundSigned,
} from "@/lib/types";
import { checkKaspaAddress, checkSlug } from "@/lib/validation";
import { KasAmount } from "./kas-amount";
import { LatePayoutNote, REFUND_STATUS_HELP, RefundStatusBadge } from "./refund-status";
import { ErrorBanner, Field, Spinner } from "./ui";

/** A P2PK Schnorr address (`<prefix>:q…`): only its key can sign refund requests. */
function isP2pkAddress(address: string): boolean {
  return address.startsWith(`${ADDRESS_PREFIX}q`);
}

/** `signed`: the rows came from a signed status read, so they carry the merchant's reasons and payout errors. */
/** `balanceSigned`: the balance came from a buyer-signed read, so its ledger totals (held, deposited, …) are present. */
/** `credits`: pages read with signed `credits` challenges (CONTRACT v2.14); totals from the newest page read. */
type Lookup = { slug: string; address: string; balance: PrepaidBalanceRead; balanceSigned: boolean; refunds: PrepaidRefundRead[]; signed: boolean; credits: CreditView | null };
type Busy = null | "connect" | "lookup" | "request" | "status" | "balance" | "credits" | "credits-more" | `cancel:${string}`;

/** Signed POST /v1/prepaid/credits/status pages: totals of the last page read, the rows loaded so far, the next cursor. */
type CreditView = { totals: BuyerCreditStatus; items: BuyerCreditStatusItem[]; nextCursor: string | null };

type SignedBalance = PrepaidBalanceRead & { deposited_sompi: string; debited_sompi: string; refunded_sompi: string; expired_sompi: string; refund_hold_sompi: string };

const SOMPI_RE = /^(0|[1-9][0-9]{0,17})$/;

/** True when every ledger total is present and a strict decimal sompi string (the signed read). */
/** A strict decimal sompi string as a bigint, else null. */
function sompiOrNull(value: string | null | undefined): bigint | null {
  return typeof value === "string" && SOMPI_RE.test(value) ? BigInt(value) : null;
}

/**
 * What a refund may take: the signed credits/status `refundable_sompi` when read, else the balance's v2.14
 * `refundable_sompi` when the read carried it, else spendable. The gateway enforces the real limit either way.
 */
function refundLimit(lookup: Lookup): { refundable: bigint; callOnly: bigint } {
  if (lookup.credits) return { refundable: BigInt(lookup.credits.totals.refundable_sompi), callOnly: BigInt(lookup.credits.totals.credit_balance_sompi) };
  return {
    refundable: sompiOrNull(lookup.balance.refundable_sompi) ?? BigInt(lookup.balance.spendable_sompi),
    callOnly: sompiOrNull(lookup.balance.credit_balance_sompi) ?? 0n,
  };
}

function hasLedgerTotals(balance: PrepaidBalanceRead): balance is SignedBalance {
  return [balance.deposited_sompi, balance.debited_sompi, balance.refunded_sompi, balance.expired_sompi, balance.refund_hold_sompi].every((v) => typeof v === "string" && SOMPI_RE.test(v));
}

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
  /** A cancel refused because the gateway found a possible payout on-chain: the refund and the tx ids it named. */
  const [payoutFound, setPayoutFound] = useState<{ refundId: string; txIds: string[] } | null>(null);

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
    const next: Lookup = { slug: slugValue, address: addressValue, balance, balanceSigned: false, refunds, signed: false, credits: null };
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
    const { refundable, callOnly } = refundLimit(lookup);
    let amountSompi: string | undefined;
    if (amount.trim() !== "") {
      const parsed = parseKasToSompi(amount);
      if (parsed === null) {
        setAmountError("Enter an amount in KAS, e.g. 1.5 (up to 8 decimals), or leave it blank for all spendable credit");
        return;
      }
      if (parsed > refundable) {
        setAmountError(
          callOnly > 0n
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
    setPayoutFound(null);
    try {
      const cancelled = await signAndSend<PrepaidRefundRead>(
        { slug: lookup.slug, payer_address: lookup.address, action: "cancel", refund_id: refund.id },
        `/api/prepaid/refunds/${refund.id}/cancel`,
        { refundId: refund.id },
      );
      await load(lookup.slug, lookup.address);
      setNotice(
        cancelled.hold_until
          ? `Refund request withdrawn. Because the merchant had started paying it, the amount stays held until ${formatDateTime(cancelled.hold_until)} in case their payout is already on its way; then it is spendable again.`
          : "Refund request withdrawn. The held credit is spendable again.",
      );
    } catch (e) {
      const txIds = e instanceof ApiError && e.code === "refund_not_cancellable" ? txIdsInMessage(e.message) : [];
      if (txIds.length > 0) {
        setPayoutFound({ refundId: refund.id, txIds });
        await load(lookup.slug, lookup.address);
      } else if (e instanceof ApiError && e.status === 503) {
        setError("The merchant had started paying this refund, and the Kaspa node can't be checked for their payout right now, so it can't be withdrawn yet. Nothing changed. Try again shortly.");
      } else {
        setError(refundErrorMessage(e));
      }
    } finally {
      setBusy(null);
    }
  }

  /** Signed `status` read: the same rows with the merchant's reject reasons and payout errors (the public view hides them). */
  /** CONTRACT v2.12.1 signed balance read: KasWare signs the timestamped balance-read text; no challenge round trip. */
  async function signBalance() {
    if (!lookup) return;
    setNotice(null);
    setBusy("balance");
    setError(null);
    try {
      const wallet = getKasware();
      if (!wallet) throw new Error("KasWare was not detected in this browser. The full balance is shown only to the depositing key.");
      if (!EXPECTED_AUTH_DOMAIN) throw new Error("This app has no gateway URL configured, so it can't build the balance message.");
      const accounts = await wallet.requestAccounts();
      if (accounts[0]?.trim().toLowerCase() !== lookup.address) throw new Error("The connected KasWare account is not this address. Switch accounts in KasWare and connect again.");
      const wrongNetwork = await kaswareNetworkRefusal(wallet, KASPA_NETWORK);
      if (wrongNetwork) throw new Error(wrongNetwork);
      const timestamp = Math.floor(Date.now() / 1000);
      const message = balanceReadMessage({ domain: EXPECTED_AUTH_DOMAIN, address: lookup.address, slug: lookup.slug, timestamp });
      let raw: string;
      try {
        raw = await wallet.signMessage(message, { type: "schnorr" });
      } catch (e) {
        if (isWalletRejection(e)) throw new Error("You declined to sign in KasWare. Only spendable credit and expiry are shown.");
        throw new Error(`KasWare: ${walletErrorText(e)}`);
      }
      const signature = normaliseBalanceSignature(raw);
      if (!signature) throw new Error("KasWare returned a signature in an unexpected format (expected 128 hex characters).");
      const balance = await apiFetch<PrepaidBalanceRead>("/api/prepaid/balance/signed", {
        method: "POST",
        body: { slug: lookup.slug, payer_address: lookup.address, timestamp: String(timestamp), signature },
      });
      if (!hasLedgerTotals(balance)) throw new Error("The gateway answered without the ledger totals. Try again.");
      setLookup({ ...lookup, balance, balanceSigned: true });
    } catch (e) {
      if (e instanceof ApiError && e.code === "invalid_balance_signature") {
        setError("The gateway didn't accept the balance signature: the KasWare account isn't the depositing key, or this device's clock is more than 5 minutes off. Nothing was shown.");
      } else {
        setError(refundErrorMessage(e));
      }
    } finally {
      setBusy(null);
    }
  }

  async function showReasons() {
    if (!lookup) return;
    setNotice(null);
    setBusy("status");
    setError(null);
    try {
      const refunds = await signAndSend<PrepaidRefundRead[]>({ slug: lookup.slug, payer_address: lookup.address, action: "status" }, "/api/prepaid/refunds/status", {});
      setLookup({ ...lookup, refunds, signed: true });
    } catch (e) {
      setError(refundErrorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  /**
   * CONTRACT v2.14 buyer credits: POST credits/challenge -> line-by-line check -> KasWare signMessage -> POST credits/status.
   * A challenge is single use, so every page (the newest, then each "older" page via next_cursor) is a new signature.
   */
  async function loadCredits(more: boolean) {
    if (!lookup) return;
    const before = more ? (lookup.credits?.nextCursor ?? null) : null;
    if (more && before === null) return;
    setNotice(null);
    setBusy(more ? "credits-more" : "credits");
    setError(null);
    try {
      const wallet = getKasware();
      if (!wallet) throw new Error("KasWare was not detected in this browser. Credits are shown only to the depositing key.");
      const accounts = await wallet.requestAccounts();
      if (accounts[0]?.trim().toLowerCase() !== lookup.address) throw new Error("The connected KasWare account is not this address. Switch accounts in KasWare and connect again.");
      const wrongNetwork = await kaswareNetworkRefusal(wallet, KASPA_NETWORK);
      if (wrongNetwork) throw new Error(wrongNetwork);
      const challenge = await apiFetch<PrepaidCreditStatusChallenge>("/api/prepaid/credits/challenge", {
        method: "POST",
        body: { slug: lookup.slug, payer_address: lookup.address },
      });
      const refusal = checkCreditChallenge(challenge, { address: lookup.address, slug: lookup.slug, domain: EXPECTED_AUTH_DOMAIN });
      if (refusal) throw new Error(refusal);
      let signature: string;
      try {
        signature = await wallet.signMessage(challenge.message, { type: "schnorr" });
      } catch (e) {
        if (isWalletRejection(e)) throw new Error("You declined to sign in KasWare. Nothing was read.");
        throw new Error(`KasWare: ${walletErrorText(e)}`);
      }
      if (!isSignatureToken(signature)) throw new Error("KasWare returned an unexpected signature.");
      const body: PrepaidCreditStatusRequest = { nonce: challenge.nonce, signature: signature.trim().toLowerCase(), limit: CREDIT_STATUS_PAGE };
      if (before !== null) body.before = before;
      const reply = await apiFetch<unknown>("/api/prepaid/credits/status", { method: "POST", body });
      const totals = parseBuyerCreditStatus(reply, CREDIT_STATUS_PAGE);
      if (!totals) throw new Error("The gateway's credit status reply did not have the documented shape.");
      const loaded = more && lookup.credits ? lookup.credits.items : [];
      const seen = new Set(loaded.map((item) => item.credit_id));
      const items = [...loaded, ...totals.credits.filter((item) => !seen.has(item.credit_id))];
      setLookup({ ...lookup, credits: { totals, items, nextCursor: totals.next_cursor } });
    } catch (e) {
      if (e instanceof ApiError && e.code === "invalid_cursor") {
        setLookup({ ...lookup, credits: lookup.credits ? { ...lookup.credits, nextCursor: null } : null });
        setError("The page marker for older credits was not accepted. Sign again from the newest credits.");
      } else {
        setError(refundErrorMessage(e));
      }
    } finally {
      setBusy(null);
    }
  }

  const now = Date.now();
  const canSign = connected !== null && lookup !== null && connected === lookup.address;
  const openRefund = lookup?.refunds.find((r) => r.status === "requested" || r.status === "paying" || r.status === "payout_submitted") ?? null;
  const balance = lookup?.balance ?? null;
  const expired = balance?.expires_at ? Date.parse(balance.expires_at) <= Date.now() : false;
  const limit = lookup ? refundLimit(lookup) : null;
  const signedRefundable = balance && sompiOrNull(balance.refundable_sompi) !== null ? (balance.refundable_sompi ?? null) : null;
  const signedCallOnly = balance && sompiOrNull(balance.credit_balance_sompi) !== null ? (balance.credit_balance_sompi ?? null) : null;

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
            {lookup.balanceSigned && hasLedgerTotals(balance) ? (
              <dl className="mt-4 grid gap-4 sm:grid-cols-4" data-balance-signed>
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
                  <dt className="label">Expires</dt>
                  <dd className={`font-mono text-xs ${expired ? "text-red-300" : "text-zinc-300"}`}>{balance.expires_at ? formatDateTime(balance.expires_at) : "never"}</dd>
                  {expired ? <p className="mt-1 text-[11px] text-red-300/80">This credit has expired and can no longer be spent or refunded.</p> : null}
                </div>
                <div>
                  <dt className="label">Deposited</dt>
                  <dd>
                    <KasAmount sompi={balance.deposited_sompi} align="left" />
                  </dd>
                </div>
                <div>
                  <dt className="label">Spent on calls</dt>
                  <dd>
                    <KasAmount sompi={balance.debited_sompi} align="left" />
                  </dd>
                </div>
                <div>
                  <dt className="label">Refunded</dt>
                  <dd>
                    <KasAmount sompi={balance.refunded_sompi} align="left" />
                  </dd>
                </div>
                <div>
                  <dt className="label">Expired</dt>
                  <dd>
                    <KasAmount sompi={balance.expired_sompi} align="left" />
                  </dd>
                </div>
                {signedRefundable !== null ? (
                  <div>
                    <dt className="label">Refundable</dt>
                    <dd>
                      <KasAmount sompi={signedRefundable} align="left" tone="text-zinc-100" />
                    </dd>
                  </div>
                ) : null}
                {signedCallOnly !== null ? (
                  <div>
                    <dt className="label" title="Credit from failed paid calls: spent first by calls, never refunded on-chain">Call-only credit</dt>
                    <dd>
                      <KasAmount sompi={signedCallOnly} align="left" tone={signedCallOnly === "0" ? "text-zinc-500" : "text-violet-300"} />
                    </dd>
                  </div>
                ) : null}
              </dl>
            ) : (
              <>
                <dl className="mt-4 grid gap-4 sm:grid-cols-4" data-balance-public>
                  <div>
                    <dt className="label">Spendable</dt>
                    <dd>
                      <KasAmount sompi={balance.spendable_sompi} align="left" tone="text-emerald-300" />
                    </dd>
                  </div>
                  <div>
                    <dt className="label">Expires</dt>
                    <dd className={`font-mono text-xs ${expired ? "text-red-300" : "text-zinc-300"}`}>{balance.expires_at ? formatDateTime(balance.expires_at) : "never"}</dd>
                    {expired ? <p className="mt-1 text-[11px] text-red-300/80">This credit has expired and can no longer be spent or refunded.</p> : null}
                  </div>
                </dl>
                <div className="mt-3 flex flex-wrap items-center gap-3">
                  <button type="button" className="btn-secondary px-3 py-1.5 text-xs" onClick={() => void signBalance()} disabled={busy !== null || !canSign} data-sign-balance>
                    {busy === "balance" ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> : <Eye className="h-3.5 w-3.5" aria-hidden />}
                    Sign to see the full balance
                  </button>
                  <p className="text-[11px] text-zinc-500">
                    Held, deposited, spent, refunded and expired amounts are shown only to the depositing key: KasWare signs a read-only message (it authorises nothing else).
                  </p>
                </div>
              </>
            )}
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
                    hint={`Blank = all refundable credit (${formatKas(limit?.refundable ?? 0n)} KAS). Minimum ${pricing.status === "ready" ? formatKas(pricing.minOutput) : "0.20"} KAS.`}
                  >
                    <input id="refund-amount" className="input font-mono" value={amount} onChange={(e) => setAmount(e.target.value)} placeholder={formatKas(limit?.refundable ?? 0n)} inputMode="decimal" autoComplete="off" />
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
                        {refund.status === "cancelled" && refund.hold_until ? (
                          <p className={`mt-1 max-w-[280px] text-[11px] ${Date.parse(refund.hold_until) > now ? "text-violet-300" : "text-zinc-500"}`} data-hold-until>
                            {Date.parse(refund.hold_until) > now
                              ? `Cooldown: this amount stays held until ${formatDateTime(refund.hold_until)} because the merchant had started paying it. If their payout lands before then, it is recorded as paid; otherwise the credit is spendable again.`
                              : `Cooldown ended ${formatDateTime(refund.hold_until)}: the held amount is released once no payout of this refund is still being checked.`}
                          </p>
                        ) : null}
                        {payoutFound?.refundId === refund.id ? (
                          <div className="mt-2 max-w-[320px] rounded-md border border-sky-500/30 bg-sky-500/10 p-2 text-[11px] text-sky-200" data-cancel-found>
                            <p className="font-medium">Not withdrawn: a payout of this refund may already be on its way to you.</p>
                            <ul className="mt-1 space-y-0.5">
                              {payoutFound.txIds.map((txId) => (
                                <li key={txId} className="font-mono break-all" data-found-tx={txId}>
                                  {IS_TESTNET ? (
                                    <a href={`${TESTNET_EXPLORER_URL}/txs/${txId}`} target="_blank" rel="noreferrer" className="text-kas-300 hover:underline">
                                      {txId}
                                    </a>
                                  ) : (
                                    txId
                                  )}
                                </li>
                              ))}
                            </ul>
                            <p className="mt-1">KasGate is confirming it. If it is this refund&apos;s payout, the refund is marked paid; if not, you can withdraw again once it is ruled out.</p>
                          </div>
                        ) : null}
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

          <section className="space-y-2" aria-labelledby="buyer-credits-heading" data-buyer-credits>
            <h2 id="buyer-credits-heading" className="text-sm font-medium text-zinc-100">
              Credit from failed calls
            </h2>
            <div className="card space-y-3 p-4">
              {lookup.credits ? (
                <>
                  <dl className="grid gap-4 sm:grid-cols-4" data-credit-totals>
                    <div>
                      <dt className="label">Credited in total</dt>
                      <dd>
                        <KasAmount sompi={lookup.credits.totals.credited_sompi} align="left" tone="text-violet-300" />
                      </dd>
                    </div>
                    <div>
                      <dt className="label" title="Spent first by calls; never paid back on-chain">Call-only credit (never refundable)</dt>
                      <dd>
                        <KasAmount sompi={lookup.credits.totals.credit_balance_sompi} align="left" tone="text-zinc-100" />
                      </dd>
                    </div>
                    <div>
                      <dt className="label">Refundable now</dt>
                      <dd>
                        <KasAmount sompi={lookup.credits.totals.refundable_sompi} align="left" tone="text-zinc-100" />
                      </dd>
                    </div>
                    <div>
                      <dt className="label">Credits</dt>
                      <dd className="font-mono text-sm text-zinc-100" data-credit-count>
                        {lookup.credits.totals.credit_count}
                      </dd>
                    </div>
                  </dl>
                  {lookup.credits.items.length > 0 ? (
                    <table className="w-full min-w-[640px]">
                      <thead>
                        <tr className="border-b border-zinc-800">
                          <th className="th">When</th>
                          <th className="th">Call</th>
                          <th className="th">Why</th>
                          <th className="th text-right">Credited (call-only)</th>
                          <th className="th text-right">Not credited (cap)</th>
                        </tr>
                      </thead>
                      <tbody>
                        {lookup.credits.items.map((credit) => (
                          <tr key={credit.credit_id} data-buyer-credit={credit.credit_id}>
                            <td className="td whitespace-nowrap font-mono text-xs">{formatDateTime(credit.created_at)}</td>
                            <td className="td font-mono text-xs">
                              {credit.slug}
                              <div className="text-[11px] text-zinc-600" title={credit.tx_id}>
                                call {shortHash(credit.tx_id)}
                              </div>
                            </td>
                            <td className="td text-xs text-zinc-400">{CREDIT_REASON_LABEL[credit.reason]}</td>
                            <td className="td">
                              <KasAmount sompi={credit.credit_sompi} tone="text-violet-300" />
                            </td>
                            <td className="td">
                              <KasAmount sompi={credit.shortfall_sompi} tone={credit.shortfall_sompi === "0" ? "text-zinc-600" : "text-amber-300"} />
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  ) : (
                    <p className="text-xs text-zinc-500">No paid call to this merchant has been credited.</p>
                  )}
                  <div className="flex flex-wrap items-center gap-3">
                    <p className="text-[11px] text-zinc-500" data-credit-shown>
                      Showing {lookup.credits.items.length} of {lookup.credits.totals.credit_count} credits, newest first.
                    </p>
                    {lookup.credits.nextCursor !== null ? (
                      <button type="button" className="btn-secondary px-3 py-1.5 text-xs" onClick={() => void loadCredits(true)} disabled={busy !== null || !canSign} data-more-credits>
                        {busy === "credits-more" ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> : <ChevronDown className="h-3.5 w-3.5" aria-hidden />}
                        Sign to load older credits
                      </button>
                    ) : null}
                    <button type="button" className="btn-ghost px-2 py-1 text-xs" onClick={() => void loadCredits(false)} disabled={busy !== null || !canSign} data-reload-credits>
                      <RefreshCw className="h-3.5 w-3.5" aria-hidden />
                      Newest
                    </button>
                  </div>
                </>
              ) : (
                <div className="flex flex-wrap items-center gap-3">
                  <button type="button" className="btn-secondary px-3 py-1.5 text-xs" onClick={() => void loadCredits(false)} disabled={busy !== null || !canSign} data-load-credits>
                    {busy === "credits" ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> : <Eye className="h-3.5 w-3.5" aria-hidden />}
                    Sign to see credits
                  </button>
                  <p className="text-[11px] text-zinc-500">Shown only to the depositing key. Each page is one read-only KasWare signature (it authorises nothing else).</p>
                </div>
              )}
              <p className="text-[11px] text-zinc-500">
                When a paid call fails for good, the merchant&apos;s share comes back to you as call-only credit: it pays for your next calls first and is never paid back
                on-chain. The platform fee is not returned.
              </p>
            </div>
          </section>
        </>
      ) : busy === "lookup" ? (
        <Spinner label="Loading…" />
      ) : null}
    </div>
  );
}
