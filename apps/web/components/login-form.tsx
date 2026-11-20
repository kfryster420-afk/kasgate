"use client";

import { KeyRound, Loader2, LogIn, Wallet } from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useState, type FormEvent } from "react";
import { apiFetch, errorMessage } from "@/lib/api-client";
import type { AuthVerifyRequest, SessionStarted } from "@/lib/types";
import { ErrorBanner, Field } from "./ui";
import { WalletProofPanel, type WalletProof } from "./wallet-proof";

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
  const [address, setAddress] = useState("");

  async function login(proof: WalletProof): Promise<void> {
    const body: AuthVerifyRequest = { ...proof };
    await apiFetch<SessionStarted>("/api/auth/verify", { method: "POST", body });
    done();
  }

  return (
    <div className="card space-y-5 p-6">
      <p className="text-sm text-zinc-400">
        Prove you own your payout address by signing a one-time message. Nothing is spent, your private key never leaves the wallet, and your API key is not changed.
      </p>
      <WalletProofPanel purpose="login" address={address} onAddressChange={setAddress} onProof={login} kaswareLabel="Sign in with KasWare" manualSubmitLabel="Verify & sign in" />
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
      await apiFetch<SessionStarted>("/api/session", { method: "POST", body: { api_key: apiKey.trim() } });
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
