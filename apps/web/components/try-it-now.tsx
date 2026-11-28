"use client";

import { CheckCircle2, ExternalLink, Loader2, Play, ShieldCheck } from "lucide-react";
import { useCallback, useRef, useState } from "react";
import { DEMO_SETTLEMENT_TX_ID, TESTNET_EXPLORER_URL } from "@/lib/config";
import { shortAddress } from "@/lib/format";
import { formatKas, formatSompi } from "@/lib/sompi";
import { DemoError, demoPath, fetchChallenge, newRunId, runTrialCall, type Payment402, type TrialOutcome } from "@/lib/trial/demo";
import { ErrorBanner } from "./ui";

type Phase = "idle" | "challenge" | "trial" | "done" | "failed";

export interface TryItNowProps {
  gatewayUrl: string;
  slug: string;
  addressPrefix: string;
  network: string;
}

const SETTLEMENT_URL = `${TESTNET_EXPLORER_URL}/transactions/${DEMO_SETTLEMENT_TX_ID}`;

export function TryItNow({ gatewayUrl, slug, addressPrefix, network }: TryItNowProps) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [challenge, setChallenge] = useState<Payment402 | null>(null);
  const [outcome, setOutcome] = useState<TrialOutcome | null>(null);
  const [error, setError] = useState<DemoError | null>(null);
  const running = useRef(false);

  const run = useCallback(async () => {
    if (running.current) return;
    running.current = true;
    setPhase("challenge");
    setChallenge(null);
    setOutcome(null);
    setError(null);
    try {
      const config = { gatewayUrl, slug, addressPrefix, network, onChallenge: setChallenge };
      const path = demoPath(slug, newRunId());
      await fetchChallenge(config, path);
      setPhase("trial");
      setOutcome(await runTrialCall(config, path));
      setPhase("done");
    } catch (caught) {
      setError(caught instanceof DemoError ? caught : new DemoError("bad_response", "The demo hit an unexpected error."));
      setPhase("failed");
    } finally {
      running.current = false;
    }
  }, [gatewayUrl, slug, addressPrefix, network]);

  const busy = phase === "challenge" || phase === "trial";

  return (
    <div className="card flex flex-col p-5 text-left sm:p-6" id="try-it-now">
      <div className="flex items-center gap-2">
        <span className="badge border-kas-700 bg-kas-900/40 font-mono text-kas-300">no wallet</span>
        <span className="font-mono text-xs text-zinc-500">live on {network}</span>
      </div>
      <h2 className="mt-3 text-xl font-semibold text-zinc-50">Try it now, no wallet</h2>
      <p className="mt-1.5 text-sm text-zinc-400">
        Call a real paywalled endpoint from this page. You will see the HTTP 402 payment terms, then get the response through a free trial call. No coins move and nothing is signed with your wallet.
      </p>

      <button type="button" onClick={() => void run()} disabled={busy} className="btn-primary mt-4 w-full justify-center px-6 py-3 text-base sm:w-auto sm:self-start">
        {busy ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : <Play className="h-4 w-4" aria-hidden />}
        {busy ? "Calling the gateway…" : phase === "idle" ? "Try it now" : "Run it again"}
      </button>

      <div className="mt-4 space-y-3" aria-live="polite">
        {challenge && <ChallengeView challenge={challenge} />}
        {phase === "trial" && (
          <p className="inline-flex items-center gap-2 text-sm text-zinc-400">
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> Making the free trial call…
          </p>
        )}
        {outcome && <OutcomeView outcome={outcome} />}
        {error && <ErrorBanner message={error.message} />}
      </div>

      <a
        href={SETTLEMENT_URL}
        target="_blank"
        rel="noopener noreferrer"
        className="mt-5 inline-flex items-center gap-1.5 text-sm text-kas-300 hover:text-kas-200"
      >
        See a real settlement on the testnet-10 explorer
        <ExternalLink className="h-3.5 w-3.5" aria-hidden />
      </a>
    </div>
  );
}

function ChallengeView({ challenge }: { challenge: Payment402 }) {
  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-950 p-3">
      <p className="font-mono text-xs text-amber-300">① HTTP 402 Payment Required · {challenge.scheme}</p>
      <dl className="mt-2 space-y-2 font-mono text-xs">
        <Row label="price" kas={challenge.totalSompi} />
        <Row label="output 0 · merchant" kas={challenge.merchantOutput.sompi} address={challenge.merchantOutput.address} />
        <Row label="output 1 · treasury" kas={challenge.treasuryOutput.sompi} address={challenge.treasuryOutput.address} />
        <Row label="platform fee" kas={challenge.feeSompi} />
      </dl>
    </div>
  );
}

function Row({ label, kas, address }: { label: string; kas: bigint; address?: string }) {
  return (
    <div className="flex flex-col gap-0.5 sm:flex-row sm:items-baseline sm:justify-between">
      <dt className="text-zinc-500">
        {label}
        {address && <span className="ml-2 text-zinc-600">{shortAddress(address)}</span>}
      </dt>
      <dd className="text-zinc-200">
        {formatKas(kas)} KAS <span className="text-zinc-500">({formatSompi(kas)} sompi)</span>
      </dd>
    </div>
  );
}

function OutcomeView({ outcome }: { outcome: TrialOutcome }) {
  return (
    <div className="rounded-lg border border-kas-800/70 bg-zinc-950 p-3">
      <p className="flex flex-wrap items-center gap-2 font-mono text-xs text-kas-300">
        <CheckCircle2 className="h-4 w-4" aria-hidden />② Free trial call · HTTP {outcome.status}
        {outcome.remaining !== null && outcome.limit !== null && (
          <span className="text-zinc-500">
            · {outcome.remaining} of {outcome.limit} trial calls left for this throwaway key
          </span>
        )}
      </p>
      <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all font-mono text-[12px] leading-relaxed text-zinc-300">
        {outcome.body}
        {outcome.bodyTruncated ? "\n…" : ""}
      </pre>
      <p className="mt-2 flex items-start gap-1.5 text-xs text-zinc-500">
        <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
        Signed in your browser by a throwaway key that owns nothing. No transaction, no fee.
      </p>
    </div>
  );
}
