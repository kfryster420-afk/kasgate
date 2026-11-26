"use client";

import { ArrowRight, CheckCircle2, LogIn } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { ADDRESS_PREFIX, GATEWAY_PUBLIC_URL, NETWORK_LABEL } from "@/lib/config";
import { ApiError, apiFetch, errorMessage } from "@/lib/api-client";
import { curlCreateEndpoint, curlDiscover, curlPaid, curlRegisterMerchant, pythonSnippet } from "@/lib/snippets";
import type { EndpointCreate, EndpointRead, MerchantCreate, MerchantKeyIssued, MerchantRead } from "@/lib/types";
import { checkDisplayName, checkKaspaAddress, checkOptionalEmail } from "@/lib/validation";
import { formatEndpointPrice } from "@/lib/format";
import { CodeBlock } from "./code-block";
import { ApiKeyReveal } from "./api-key-reveal";
import { CopyButton } from "./copy-button";
import { EndpointForm } from "./endpoint-form";
import { Field } from "./ui";
import { WalletProofPanel, type WalletProof } from "./wallet-proof";

type Step = 1 | 2 | 3;

const STEPS: readonly { n: Step; title: string }[] = [
  { n: 1, title: "Payout wallet" },
  { n: 2, title: "Your API" },
  { n: 3, title: "Go live" },
];

export function OnboardingWizard({ initialMerchant }: { initialMerchant: MerchantRead | null }) {
  const [step, setStep] = useState<Step>(initialMerchant ? 2 : 1);
  const [merchant, setMerchant] = useState<MerchantRead | null>(initialMerchant);
  const [endpoint, setEndpoint] = useState<EndpointRead | null>(null);
  const [revealedKey, setRevealedKey] = useState<string | null>(null);

  return (
    <div className="mx-auto max-w-3xl px-4 py-10">
      <div className="mb-8">
        <h1 className="text-2xl font-semibold text-zinc-100">Monetize your API in 60 seconds</h1>
        <p className="mt-1 text-sm text-zinc-400">
          No code changes. Three steps on Kaspa {NETWORK_LABEL}: connect a payout address, point us at your API, share the live proxy URL.
        </p>
      </div>

      <ol className="mb-8 grid grid-cols-3 gap-2" aria-label="Progress">
        {STEPS.map((s) => {
          const done = s.n < step;
          const active = s.n === step;
          return (
            <li key={s.n} className={`rounded-lg border px-3 py-2 text-sm ${active ? "border-kas-500/60 bg-kas-500/10 text-kas-200" : done ? "border-zinc-800 bg-zinc-900/60 text-zinc-300" : "border-zinc-800 text-zinc-600"}`} aria-current={active ? "step" : undefined}>
              <span className="mr-2 inline-flex items-center font-mono text-xs">{done ? <CheckCircle2 className="h-4 w-4 text-kas-400" aria-hidden /> : s.n}</span>
              {s.title}
            </li>
          );
        })}
      </ol>

      {step === 1 && !revealedKey ? (
        <MerchantStep
          onDone={(created) => {
            setMerchant(created.merchant);
            setRevealedKey(created.api_key);
          }}
        />
      ) : null}
      {revealedKey ? (
        <ApiKeyReveal
          apiKey={revealedKey}
          title="Your API key (shown once)"
          doneLabel="Continue to step 2"
          onDone={() => {
            setRevealedKey(null);
            setStep(2);
          }}
        />
      ) : null}
      {step === 2 && merchant && !revealedKey ? (
        <EndpointStep
          merchant={merchant}
          onDone={(e) => {
            setEndpoint(e);
            setStep(3);
          }}
        />
      ) : null}
      {step === 3 && endpoint ? <LiveStep endpoint={endpoint} onAnother={() => { setEndpoint(null); setStep(2); }} /> : null}

      <section className="mt-12">
        <h2 className="mb-3 text-sm font-medium uppercase tracking-wide text-zinc-500">Prefer the API? Same thing in curl</h2>
        <div className="space-y-3">
          <CodeBlock title="1 · register" code={curlRegisterMerchant(GATEWAY_PUBLIC_URL)} />
          <CodeBlock title="2 · create endpoint" code={curlCreateEndpoint(GATEWAY_PUBLIC_URL)} />
        </div>
      </section>
    </div>
  );
}

function MerchantStep({ onDone }: { onDone: (created: MerchantKeyIssued) => void }) {
  const [address, setAddress] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [touched, setTouched] = useState(false);

  const addressCheck = checkKaspaAddress(address);
  const nameCheck = checkDisplayName(displayName);
  const emailCheck = checkOptionalEmail(email);

  function precheck(options?: { skipAddress?: boolean }): string | null {
    setTouched(true);
    if (!options?.skipAddress && !addressCheck.ok) return addressCheck.error;
    if (!nameCheck.ok) return nameCheck.error;
    if (!emailCheck.ok) return emailCheck.error;
    return null;
  }

  async function register(proof: WalletProof): Promise<void> {
    // proof.kaspa_address is the checked, lower-cased address the wallet signed for (it may have been auto-filled from
    // KasWare in this same click, so it must not be read from this closure's stale form state).
    if (!nameCheck.ok || !emailCheck.ok) throw new Error("Fix the highlighted fields first");
    const body: MerchantCreate = { ...proof, display_name: nameCheck.value };
    if (emailCheck.value) body.email = emailCheck.value;
    try {
      const created = await apiFetch<MerchantKeyIssued>("/api/merchants", { method: "POST", body });
      onDone(created);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) throw new Error("That address or email is already registered. Sign in with your wallet instead.");
      throw e;
    }
  }

  return (
    <div className="card space-y-5 p-6">
      <h2 className="text-lg font-medium text-zinc-100">Step 1 · Prove your payout wallet</h2>
      <Field
        label="Kaspa payout address"
        htmlFor="kaspa-address"
        error={touched && address.trim() !== "" && !addressCheck.ok ? addressCheck.error : null}
        hint={`Must start with "${ADDRESS_PREFIX}" on ${NETWORK_LABEL}. Payments land here directly on-chain; you will sign a message to prove you own it.`}
      >
        <input id="kaspa-address" className="input font-mono" value={address} onChange={(e) => setAddress(e.target.value)} placeholder={`${ADDRESS_PREFIX}q…`} autoComplete="off" spellCheck={false} />
      </Field>
      <div className="grid gap-5 sm:grid-cols-2">
        <Field label="Display name" htmlFor="display-name" error={touched && !nameCheck.ok ? nameCheck.error : null} hint="Shown next to your endpoints in the directory">
          <input id="display-name" className="input" value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder="Acme Data" maxLength={120} autoComplete="organization" />
        </Field>
        <Field label="Email (optional)" htmlFor="email" error={touched && !emailCheck.ok ? emailCheck.error : null} hint="Only for account notices">
          <input id="email" type="email" className="input" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com" autoComplete="email" />
        </Field>
      </div>
      <p className="rounded-lg border border-zinc-800 bg-zinc-950 p-3 text-xs text-zinc-500">
        Registration signs a one-time message that authorises nothing but ownership of the address. You will then receive an automation API key, shown once. Your browser session is a wallet session token kept in a secure httpOnly cookie; the API key is never stored in the browser.
      </p>
      <WalletProofPanel
        purpose="register"
        address={address}
        onAddressChange={setAddress}
        showAddressField={false}
        precheck={precheck}
        onProof={register}
        kaswareLabel="Register with KasWare"
        manualSubmitLabel="Verify & register"
      />
      <div>
        <Link href="/login" className="btn-ghost">
          <LogIn className="h-4 w-4" aria-hidden />
          I already have an account
        </Link>
      </div>
    </div>
  );
}

function EndpointStep({ merchant, onDone }: { merchant: MerchantRead; onDone: (endpoint: EndpointRead) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function create(payload: EndpointCreate) {
    setBusy(true);
    setError(null);
    try {
      const endpoint = await apiFetch<EndpointRead>("/api/endpoints", { method: "POST", body: payload });
      onDone(endpoint);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setError("Your session expired. Sign in again with your wallet.");
      else setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card p-6">
      <h2 className="text-lg font-medium text-zinc-100">Step 2 · Register your API</h2>
      <p className="mb-5 mt-1 text-sm text-zinc-400">
        Signed in as <span className="text-zinc-200">{merchant.display_name}</span> · payouts to <span className="font-mono text-xs text-zinc-300">{merchant.kaspa_address.slice(0, 18)}…</span>
      </p>
      <EndpointForm mode="create" busy={busy} error={error} onSubmit={create} />
    </div>
  );
}

function LiveStep({ endpoint, onAnother }: { endpoint: EndpointRead; onAnother: () => void }) {
  const method = endpoint.allowed_methods[0] ?? "GET";
  return (
    <div className="space-y-6">
      <div className="card border-kas-500/40 p-6">
        <div className="flex items-center gap-2 text-kas-300">
          <CheckCircle2 className="h-5 w-5" aria-hidden />
          <h2 className="text-lg font-medium">Step 3 · You are live</h2>
        </div>
        <p className="mt-1 text-sm text-zinc-400">
          <span className="text-zinc-200">{endpoint.name}</span> now charges {formatEndpointPrice(endpoint)} per call. Unpaid requests receive HTTP 402.
        </p>
        <div className="mt-5">
          <span className="label">Live proxy URL</span>
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
            <code className="flex-1 overflow-x-auto rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2.5 font-mono text-sm text-kas-300">{endpoint.proxy_url}</code>
            <CopyButton value={endpoint.proxy_url} label="Copy URL" />
          </div>
        </div>
        <div className="mt-4 flex flex-wrap gap-3">
          <Link href="/dashboard" className="btn-primary">
            Open dashboard
            <ArrowRight className="h-4 w-4" aria-hidden />
          </Link>
          <button type="button" className="btn-secondary" onClick={onAnother}>
            Add another endpoint
          </button>
        </div>
      </div>

      <div className="space-y-3">
        <CodeBlock title="curl · discover the price" code={curlDiscover(endpoint.proxy_url, method)} />
        <CodeBlock title="curl · paid call" code={curlPaid(endpoint.proxy_url, method)} />
        <CodeBlock title="python · requests" code={pythonSnippet(endpoint.proxy_url, method)} />
      </div>
    </div>
  );
}
