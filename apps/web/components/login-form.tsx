"use client";

import { KeyRound, Loader2, LogIn, Wallet } from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { ADDRESS_PREFIX, NETWORK_LABEL } from "@/lib/config";
import { apiFetch, errorMessage } from "@/lib/api-client";
import { getKasware, normalizePublicKey, normalizeSignature } from "@/lib/kasware";
import type { AuthChallenge, AuthChallengeRequest, AuthVerifyRequest, MerchantRegistered } from "@/lib/types";
import { checkKaspaAddress } from "@/lib/validation";
import { CopyButton } from "./copy-button";
import { ErrorBanner, Field } from "./ui";

type Mode = "wallet" | "key";

export function LoginForm() {
  const [mode, setMode] = useState<Mode>("wallet");
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-2" role="tablist" aria-label="Sign-in method">
        <TabButton active={mode === "wallet"} onClick={() => setMode("wallet")} icon={<Wallet className="h-4 w-4" aria-hidden />} label="Wallet signature" />
        <TabButton active={mode === "key"} onClick={() => setMode("key")} icon={<KeyRound className="h-4 w-4" aria-hidden />} label="API key" />
      </div>
      {mode === "wallet" ? <WalletLogin /> : <ApiKeyLogin />}
    </div>
  );
}

function TabButton({ active, onClick, icon, label }: { active: boolean; onClick: () => void; icon: React.ReactNode; label: string }) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={`btn border text-sm ${active ? "border-kas-500/60 bg-kas-500/10 text-kas-200" : "border-zinc-800 text-zinc-400 hover:border-zinc-700"}`}
    >
      {icon}
      {label}
    </button>
  );
}

function useSignedIn() {
  const router = useRouter();
  return useCallback(() => {
    router.replace("/dashboard");
    router.refresh();
  }, [router]);
}

function WalletLogin() {
  const done = useSignedIn();
  const [hasWallet, setHasWallet] = useState(false);
  const [address, setAddress] = useState("");
  const [challenge, setChallenge] = useState<AuthChallenge | null>(null);
  const [publicKey, setPublicKey] = useState("");
  const [signature, setSignature] = useState("");
  const [busy, setBusy] = useState<null | "challenge" | "wallet" | "verify">(null);
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

  async function requestChallenge(addr: string): Promise<AuthChallenge> {
    const checked = checkKaspaAddress(addr);
    if (!checked.ok) throw new Error(checked.error);
    const body: AuthChallengeRequest = { kaspa_address: checked.value };
    const result = await apiFetch<AuthChallenge>("/api/auth/challenge", { method: "POST", body });
    setChallenge(result);
    setSignature("");
    return result;
  }

  async function verify(request: AuthVerifyRequest): Promise<void> {
    await apiFetch<MerchantRegistered>("/api/auth/verify", { method: "POST", body: request });
    done();
  }

  async function getChallengeClick() {
    setError(null);
    setBusy("challenge");
    try {
      await requestChallenge(address);
    } catch (e) {
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
    setError(null);
    setBusy("wallet");
    try {
      const accounts = await wallet.requestAccounts();
      const account = accounts[0];
      if (!account) throw new Error("KasWare did not return an account");
      setAddress(account);
      const fresh = await requestChallenge(account);
      const walletKey = wallet.getPublicKey ? await wallet.getPublicKey() : publicKey;
      const normalizedKey = normalizePublicKey(walletKey);
      if (!normalizedKey) throw new Error("KasWare did not provide a usable public key. Paste it in the manual fields below.");
      setPublicKey(normalizedKey);
      const raw = await wallet.signMessage(fresh.challenge, { type: "schnorr" });
      const normalizedSignature = normalizeSignature(raw);
      if (!normalizedSignature) throw new Error("Unrecognised signature format from the wallet. Paste a hex signature in the manual fields below.");
      setSignature(normalizedSignature);
      setBusy("verify");
      await verify({ kaspa_address: fresh.kaspa_address, challenge: fresh.challenge, public_key: normalizedKey, signature: normalizedSignature });
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  async function submitManual(event: FormEvent) {
    event.preventDefault();
    if (!challenge) return;
    const key = normalizePublicKey(publicKey);
    const sig = normalizeSignature(signature);
    if (!key) return setError("Public key must be 64 or 66 hex characters");
    if (!sig) return setError("Signature must be 128 hex characters");
    if (Date.parse(challenge.expires_at) < Date.now()) return setError("This challenge expired. Request a new one.");
    setError(null);
    setBusy("verify");
    try {
      await verify({ kaspa_address: challenge.kaspa_address, challenge: challenge.challenge, public_key: key, signature: sig });
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="card space-y-5 p-6">
      <p className="text-sm text-zinc-400">
        Prove you own your payout address by signing a one-time message. Nothing is spent and your private key never leaves the wallet. Signing in issues a fresh API key and replaces the old one.
      </p>

      <button type="button" className="btn-primary w-full" onClick={() => void signWithKasware()} disabled={busy !== null || !hasWallet}>
        {busy === "wallet" || busy === "verify" ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : <Wallet className="h-4 w-4" aria-hidden />}
        {hasWallet ? "Sign in with KasWare" : "KasWare not detected"}
      </button>
      {!hasWallet ? <p className="-mt-2 text-xs text-zinc-500">Install KasWare, or sign the challenge with any Kaspa wallet that can sign messages using the manual steps below.</p> : null}

      <div className="relative py-1 text-center text-xs uppercase tracking-wide text-zinc-600">
        <span className="bg-zinc-900 px-2">or sign manually</span>
        <div className="absolute inset-x-0 top-1/2 -z-10 border-t border-zinc-800" />
      </div>

      <Field label="1 · Payout address" htmlFor="login-address" hint={`Your ${ADDRESS_PREFIX} address on ${NETWORK_LABEL}.`}>
        <div className="flex gap-2">
          <input id="login-address" className="input font-mono" value={address} onChange={(e) => setAddress(e.target.value)} placeholder={`${ADDRESS_PREFIX}q…`} autoComplete="off" spellCheck={false} />
          <button type="button" className="btn-secondary shrink-0" onClick={() => void getChallengeClick()} disabled={busy !== null || address.trim() === ""}>
            {busy === "challenge" ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : null}
            Get challenge
          </button>
        </div>
      </Field>

      {challenge ? (
        <form onSubmit={(e) => void submitManual(e)} className="space-y-4">
          <div>
            <div className="mb-1.5 flex items-center justify-between">
              <span className="label !mb-0">2 · Sign this exact message</span>
              <CopyButton value={challenge.challenge} label="Copy challenge" />
            </div>
            <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded-lg border border-zinc-800 bg-zinc-950 p-3 font-mono text-xs text-zinc-300">{challenge.challenge}</pre>
            <p className="mt-1.5 text-xs text-zinc-500">Single use. Expires {new Date(challenge.expires_at).toISOString().slice(11, 19)} UTC. Sign it byte-for-byte as shown (Schnorr).</p>
          </div>
          <Field label="3 · Public key (hex)" htmlFor="login-pubkey" hint="64 hex (x-only) or 66 hex (02/03 prefix).">
            <input id="login-pubkey" className="input font-mono" value={publicKey} onChange={(e) => setPublicKey(e.target.value)} autoComplete="off" spellCheck={false} />
          </Field>
          <Field label="Signature (128 hex)" htmlFor="login-signature">
            <textarea id="login-signature" className="input min-h-[64px] font-mono" value={signature} onChange={(e) => setSignature(e.target.value)} autoComplete="off" spellCheck={false} />
          </Field>
          <button type="submit" className="btn-primary" disabled={busy !== null || publicKey.trim() === "" || signature.trim() === ""}>
            {busy === "verify" ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : <LogIn className="h-4 w-4" aria-hidden />}
            Verify & sign in
          </button>
        </form>
      ) : null}

      <ErrorBanner message={error} />
    </div>
  );
}

function ApiKeyLogin() {
  const done = useSignedIn();
  const [apiKey, setApiKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await apiFetch<MerchantRegistered>("/api/session", { method: "POST", body: { api_key: apiKey.trim() } });
      setApiKey("");
      done();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={(e) => void submit(e)} className="card space-y-5 p-6">
      <Field label="API key" htmlFor="api-key" hint="Verified with the gateway, then kept only in a secure httpOnly cookie.">
        <input id="api-key" type="password" className="input font-mono" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="kg_…" autoComplete="off" spellCheck={false} required />
      </Field>
      <ErrorBanner message={error} />
      <button type="submit" className="btn-primary" disabled={busy || apiKey.trim() === ""}>
        {busy ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : <LogIn className="h-4 w-4" aria-hidden />}
        Sign in
      </button>
    </form>
  );
}
