"use client";

import { useRouter } from "next/navigation";
import { useCallback, useState } from "react";
import { apiFetch } from "@/lib/api-client";
import type { AuthVerifyRequest, SessionStarted } from "@/lib/types";
import { WalletProofPanel, type WalletProof } from "./wallet-proof";

export function LoginForm() {
  return <WalletLogin />;
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
