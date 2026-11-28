"use client";

import { Droplets, ExternalLink, Loader2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { GATEWAY_PUBLIC_URL, TESTNET_EXPLORER_URL } from "@/lib/config";

const TESTNET_ADDRESS_RE = /^kaspatest:[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{61,63}$/;
const TX_ID_RE = /^[0-9a-f]{64}$/;

type FaucetState =
  | { kind: "idle" }
  | { kind: "sending" }
  | { kind: "sent"; txId: string }
  | { kind: "error"; message: string };

function detailOf(payload: unknown): string | null {
  if (payload && typeof payload === "object" && "detail" in payload) {
    const detail = (payload as { detail: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail.trim().slice(0, 300);
  }
  return null;
}

/** Testnet-10 only: asks the KasGate faucet (POST /v1/faucet/claim {address} -> {tx_id}) to send free test KAS. */
export function FaucetBox() {
  const [address, setAddress] = useState("");
  const [state, setState] = useState<FaucetState>({ kind: "idle" });

  async function claim(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = address.trim().toLowerCase();
    if (!TESTNET_ADDRESS_RE.test(trimmed)) {
      setState({ kind: "error", message: "Enter a testnet-10 address that starts with kaspatest:" });
      return;
    }
    setState({ kind: "sending" });
    let response: Response;
    try {
      response = await fetch(`${GATEWAY_PUBLIC_URL}/v1/faucet/claim`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ address: trimmed }),
        cache: "no-store",
      });
    } catch {
      setState({ kind: "error", message: "The faucet could not be reached. Try again in a moment." });
      return;
    }
    let payload: unknown = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    if (response.ok) {
      const txId = payload && typeof payload === "object" && "tx_id" in payload ? (payload as { tx_id: unknown }).tx_id : null;
      if (typeof txId === "string" && TX_ID_RE.test(txId)) {
        setState({ kind: "sent", txId });
      } else {
        setState({ kind: "error", message: "The faucet answered without a transaction id. Check your wallet before trying again." });
      }
      return;
    }
    const detail = detailOf(payload);
    if (response.status === 429) {
      setState({ kind: "error", message: detail ?? "You have already claimed recently. Try again later." });
    } else if (response.status === 400) {
      setState({ kind: "error", message: detail ?? "That address was not accepted." });
    } else {
      setState({ kind: "error", message: detail ?? `The faucet is unavailable right now (HTTP ${response.status}).` });
    }
  }

  return (
    <form onSubmit={claim} className="mt-4 rounded-lg border border-kas-800/60 bg-kas-900/20 p-4">
      <p className="flex items-center gap-2 text-sm font-medium text-zinc-100">
        <Droplets className="h-4 w-4 text-kas-300" aria-hidden />
        Get free test KAS from KasGate
      </p>
      <div className="mt-3 flex flex-col gap-2 sm:flex-row">
        <input
          value={address}
          onChange={(event) => setAddress(event.target.value)}
          placeholder="kaspatest:..."
          autoComplete="off"
          spellCheck={false}
          aria-label="Your testnet-10 address"
          className="min-w-0 flex-1 rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 font-mono text-sm text-zinc-100 placeholder:text-zinc-600 focus:border-kas-500 focus:outline-none"
        />
        <button type="submit" disabled={state.kind === "sending"} className="btn-primary inline-flex items-center justify-center gap-2 px-4 py-2 disabled:opacity-60">
          {state.kind === "sending" && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
          Send me test KAS
        </button>
      </div>
      <div aria-live="polite">
        {state.kind === "sent" && (
          <p className="mt-2 text-sm text-emerald-300">
            Sent.{" "}
            <a href={`${TESTNET_EXPLORER_URL}/transactions/${state.txId}`} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 break-all font-mono text-kas-300 hover:text-kas-200">
              {state.txId.slice(0, 16)}...
              <ExternalLink className="h-3.5 w-3.5 shrink-0" aria-hidden />
            </a>
          </p>
        )}
        {state.kind === "error" && <p className="mt-2 text-sm text-amber-300">{state.message}</p>}
      </div>
    </form>
  );
}
