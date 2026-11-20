"use client";

import { Loader2, LogIn, ShieldCheck, Wallet } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { apiFetch, errorMessage } from "@/lib/api-client";
import { checkChallenge, parseChallenge } from "@/lib/challenge";
import { ADDRESS_PREFIX, EXPECTED_AUTH_DOMAIN, NETWORK_LABEL } from "@/lib/config";
import { getKasware, isPublicKey, isSignatureToken } from "@/lib/kasware";
import type { AuthChallenge, AuthChallengeRequest, AuthPurpose } from "@/lib/types";
import { checkKaspaAddress } from "@/lib/validation";
import { CopyButton } from "./copy-button";
import { ErrorBanner, Field } from "./ui";

export interface WalletProof {
  kaspa_address: string;
  challenge: string;
  public_key: string;
  /** Exactly what the wallet (or the user) supplied. */
  signature: string;
}

interface Props {
  purpose: AuthPurpose;
  address: string;
  onAddressChange: (address: string) => void;
  onProof: (proof: WalletProof) => Promise<void>;
  /** Runs before any challenge is requested; return a message to stop. */
  precheck?: () => string | null;
  showAddressField?: boolean;
  kaswareLabel: string;
  manualSubmitLabel: string;
  disabled?: boolean;
}

type Busy = null | "challenge" | "wallet" | "submit";

const PURPOSE_HEADLINE: Record<AuthPurpose, string> = {
  login: "Sign in to the KasGate merchant dashboard",
  register: "Register this address as a KasGate merchant",
};

/** Challenge, Domain/Purpose check, then KasWare or manual signing. Never signs anything the checks refuse. */
export function WalletProofPanel({ purpose, address, onAddressChange, onProof, precheck, showAddressField = true, kaswareLabel, manualSubmitLabel, disabled = false }: Props) {
  const [hasWallet, setHasWallet] = useState(false);
  const [challenge, setChallenge] = useState<AuthChallenge | null>(null);
  const [publicKey, setPublicKey] = useState("");
  const [signature, setSignature] = useState("");
  const [busy, setBusy] = useState<Busy>(null);
  const [error, setError] = useState<string | null>(null);

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

  /** Fetches a challenge and refuses it unless Domain, Purpose, Address and Expires are as expected. */
  async function fetchChallenge(addr: string): Promise<AuthChallenge> {
    const checked = checkKaspaAddress(addr);
    if (!checked.ok) throw new Error(checked.error);
    const body: AuthChallengeRequest = { kaspa_address: checked.value, purpose };
    const result = await apiFetch<AuthChallenge>("/api/auth/challenge", { method: "POST", body });
    const refusal = checkChallenge(result.challenge, { purpose, address: checked.value, domain: EXPECTED_AUTH_DOMAIN, expiresAt: result.expires_at });
    if (refusal) throw new Error(refusal);
    setChallenge(result);
    setSignature("");
    return result;
  }

  function startGuard(): boolean {
    const problem = precheck?.() ?? null;
    if (problem) {
      setError(problem);
      return false;
    }
    setError(null);
    return true;
  }

  async function getChallengeClick() {
    if (!startGuard()) return;
    setBusy("challenge");
    try {
      await fetchChallenge(address);
    } catch (e) {
      setChallenge(null);
      setError(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  async function signWithKasware() {
    const wallet = getKasware();
    if (!wallet) {
      setError("KasWare was not detected in this browser. Use the manual option below.");
      return;
    }
    if (!startGuard()) return;
    setBusy("wallet");
    try {
      const accounts = await wallet.requestAccounts();
      const account = accounts[0];
      if (!account) throw new Error("KasWare did not return an account");
      if (showAddressField || address.trim() === "") onAddressChange(account);
      else if (address.trim().toLowerCase() !== account.toLowerCase()) {
        throw new Error("The connected KasWare account is not the address you entered. Switch accounts in KasWare or fix the address.");
      }
      const fresh = await fetchChallenge(account);
      const walletKey = wallet.getPublicKey ? await wallet.getPublicKey() : publicKey;
      if (!isPublicKey(walletKey)) throw new Error("KasWare did not provide a usable public key. Paste it in the manual fields below.");
      setPublicKey(walletKey.trim());
      const signed = await wallet.signMessage(fresh.challenge, { type: "schnorr" });
      if (!isSignatureToken(signed)) throw new Error("KasWare returned an unexpected signature. Paste a signature in the manual fields below.");
      setSignature(signed);
      setBusy("submit");
      await onProof({ kaspa_address: fresh.kaspa_address, challenge: fresh.challenge, public_key: walletKey.trim(), signature: signed });
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  async function submitManual(event: FormEvent) {
    event.preventDefault();
    if (!challenge) return;
    const refusal = checkChallenge(challenge.challenge, { purpose, address: challenge.kaspa_address, domain: EXPECTED_AUTH_DOMAIN, expiresAt: challenge.expires_at });
    if (refusal) return setError(refusal);
    if (!isPublicKey(publicKey)) return setError("Public key must be 64 or 66 hex characters");
    if (!isSignatureToken(signature)) return setError("Paste the signature exactly as your wallet produced it");
    setError(null);
    setBusy("submit");
    try {
      await onProof({ kaspa_address: challenge.kaspa_address, challenge: challenge.challenge, public_key: publicKey.trim(), signature: signature.trim() });
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  const parsed = challenge ? parseChallenge(challenge.challenge) : null;
  const locked = disabled || busy !== null;

  return (
    <div className="space-y-5">
      <button type="button" className="btn-primary w-full" onClick={() => void signWithKasware()} disabled={locked || !hasWallet}>
        {busy === "wallet" || busy === "submit" ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : <Wallet className="h-4 w-4" aria-hidden />}
        {hasWallet ? kaswareLabel : "KasWare not detected"}
      </button>
      {!hasWallet ? <p className="-mt-2 text-xs text-zinc-500">Install KasWare, or sign the challenge with any Kaspa wallet that can sign messages using the manual steps below.</p> : null}

      <div className="relative py-1 text-center text-xs uppercase tracking-wide text-zinc-600">
        <span className="bg-zinc-900 px-2">or sign manually</span>
        <div className="absolute inset-x-0 top-1/2 -z-10 border-t border-zinc-800" />
      </div>

      {showAddressField ? (
        <Field label="1 · Payout address" htmlFor={`wp-address-${purpose}`} hint={`Your ${ADDRESS_PREFIX} address on ${NETWORK_LABEL}.`}>
          <input id={`wp-address-${purpose}`} className="input font-mono" value={address} onChange={(e) => onAddressChange(e.target.value)} placeholder={`${ADDRESS_PREFIX}q…`} autoComplete="off" spellCheck={false} />
        </Field>
      ) : null}
      <button type="button" className="btn-secondary" onClick={() => void getChallengeClick()} disabled={locked || address.trim() === ""}>
        {busy === "challenge" ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : null}
        Get challenge to sign
      </button>

      {challenge && parsed ? (
        <form onSubmit={(e) => void submitManual(e)} className="space-y-4">
          <div className="rounded-lg border border-emerald-900/60 bg-emerald-950/20 p-3 text-xs text-emerald-200">
            <div className="mb-1 flex items-center gap-1.5 font-medium">
              <ShieldCheck className="h-4 w-4" aria-hidden />
              Checked before signing
            </div>
            <dl className="grid grid-cols-[auto,1fr] gap-x-3 gap-y-0.5 font-mono text-[11px]">
              <dt className="text-emerald-400/70">Domain</dt>
              <dd>{parsed.domain}</dd>
              <dt className="text-emerald-400/70">Purpose</dt>
              <dd>{PURPOSE_HEADLINE[purpose]}. This does not authorise any payment.</dd>
            </dl>
          </div>
          <div>
            <div className="mb-1.5 flex items-center justify-between">
              <span className="label !mb-0">2 · Sign this exact message</span>
              <CopyButton value={challenge.challenge} label="Copy challenge" />
            </div>
            <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded-lg border border-zinc-800 bg-zinc-950 p-3 font-mono text-xs text-zinc-300">{challenge.challenge}</pre>
            <p className="mt-1.5 text-xs text-zinc-500">Single use. Expires {new Date(challenge.expires_at).toISOString().slice(11, 19)} UTC. Sign it byte for byte (Schnorr).</p>
          </div>
          <Field label="3 · Public key (hex)" htmlFor={`wp-pubkey-${purpose}`} hint="64 hex (x-only) or 66 hex (02/03 prefix).">
            <input id={`wp-pubkey-${purpose}`} className="input font-mono" value={publicKey} onChange={(e) => setPublicKey(e.target.value)} autoComplete="off" spellCheck={false} />
          </Field>
          <Field label="Signature" htmlFor={`wp-signature-${purpose}`} hint="Paste exactly what your wallet returns (hex or Base64). It is sent unchanged.">
            <textarea id={`wp-signature-${purpose}`} className="input min-h-[64px] font-mono" value={signature} onChange={(e) => setSignature(e.target.value)} autoComplete="off" spellCheck={false} />
          </Field>
          <button type="submit" className="btn-primary" disabled={locked || publicKey.trim() === "" || signature.trim() === ""}>
            {busy === "submit" ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : <LogIn className="h-4 w-4" aria-hidden />}
            {manualSubmitLabel}
          </button>
        </form>
      ) : null}

      <ErrorBanner message={error} />
    </div>
  );
}
