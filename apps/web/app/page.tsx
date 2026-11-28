import { ArrowRight, Bot, ExternalLink, FileSpreadsheet, Gauge, Hexagon, Lock, Wallet, Zap } from "lucide-react";
import Link from "next/link";
import { CodeBlock } from "@/components/code-block";
import { FaucetBox } from "@/components/faucet-box";
import { TryItNow } from "@/components/try-it-now";
import { ADDRESS_PREFIX, DEMO_ENDPOINT_SLUG, GATEWAY_PUBLIC_URL, IS_TESTNET, KASPA_NETWORK, KASWARE_URL, NETWORK_LABEL, TESTNET_FAUCET_URL } from "@/lib/config";
import { getMinOutputSompi } from "@/lib/server/gateway";
import { curlCreateEndpoint, curlRegisterMerchant } from "@/lib/snippets";
import { flatFeeMaxPriceSompi, formatKas, minPriceSompi } from "@/lib/sompi";

export const dynamic = "force-dynamic";

const FEATURES = (feeNote: string) =>
  [
  { icon: Zap, title: "60-second, zero-code onboarding", body: "Paste your upstream API URL, set a price in KAS, get a live proxy URL. Your API stays untouched." },
  { icon: Lock, title: "Non-custodial by design", body: `Each paid call is one Kaspa transaction: your share straight to your address, the platform fee (${feeNote}) to the treasury. We never hold your funds.` },
  { icon: Bot, title: "Built for AI agents", body: "x402-style HTTP 402 handshake plus a public directory and machine-readable manifest that agents can crawl." },
  { icon: FileSpreadsheet, title: "Tax-ready ledger", body: "Every settlement records KAS and the USD value at settlement. Export a CSV for your accountant in one click." },
  { icon: Gauge, title: "Live analytics", body: "Daily revenue, per-endpoint breakdowns and a full settlements table with transaction IDs." },
  { icon: Hexagon, title: "Fast finality on Kaspa", body: "Sub-second blocks mean buyers pay and get a response in a single round trip." },
] as const;

export default async function LandingPage() {
  const floor = await getMinOutputSompi();
  const feeNote = floor === null ? "1.5%, with a small flat minimum on low-priced calls" : `1.5%, or a flat ${formatKas(floor)} KAS on calls up to ${formatKas(flatFeeMaxPriceSompi(floor))} KAS`;
  const minPrice = floor === null ? null : `${formatKas(minPriceSompi(floor))} KAS`;

  return (
    <div>
      <section className="mx-auto max-w-6xl px-4 pb-10 pt-14 text-center sm:pt-20">
        {IS_TESTNET ? (
          <span className="badge border-amber-700 bg-amber-950/60 font-mono text-amber-200">testnet beta · Kaspa {NETWORK_LABEL}</span>
        ) : (
          <span className="badge border-kas-700 bg-kas-900/40 font-mono text-kas-300">live on Kaspa {NETWORK_LABEL}</span>
        )}
        <h1 className="mx-auto mt-6 max-w-3xl text-4xl font-bold tracking-tight text-zinc-50 sm:text-6xl">
          Get paid in <span className="text-kas-400">KAS</span> every time your API is called
        </h1>
        <p className="mx-auto mt-5 max-w-2xl text-lg text-zinc-400">
          KasGate wraps any HTTP API in a pay-per-call paywall. No SDK, no deploy, no custody. Humans and AI agents pay on-chain; the platform fee is 1.5% with a small flat minimum on low-priced calls.
        </p>
        <div className="mt-8 flex flex-wrap justify-center gap-3">
          <a href="#try-it-now" className="btn-primary px-6 py-3 text-base">
            Try it now, no wallet
            <ArrowRight className="h-4 w-4" aria-hidden />
          </a>
          <Link href="/onboarding" className="btn-secondary px-6 py-3 text-base">
            Monetize an API
          </Link>
          <Link href="/directory" className="btn-secondary px-6 py-3 text-base">
            Browse the directory
          </Link>
          <Link href="/beta" className="btn-secondary px-6 py-3 text-base">
            Beta guide
          </Link>
        </div>
      </section>

      {IS_TESTNET && (
        <section className="mx-auto max-w-6xl px-4 pb-6" aria-label="Try KasGate">
          <div className="grid gap-4 md:grid-cols-2">
            <TryItNow gatewayUrl={GATEWAY_PUBLIC_URL} slug={DEMO_ENDPOINT_SLUG} addressPrefix={ADDRESS_PREFIX} network={KASPA_NETWORK} />
            <PayForReal />
          </div>
        </section>
      )}

      <section className="mx-auto max-w-6xl px-4 py-8">
        <dl className="mx-auto mt-0 grid max-w-3xl grid-cols-3 gap-4 text-left">
          <Metric value="1.5%" label="platform fee" />
          <Metric value={minPrice ?? "—"} label="minimum price per call" />
          <Metric value="0" label="lines of code" />
        </dl>
      </section>

      <section className="mx-auto max-w-6xl px-4 py-12">
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {FEATURES(feeNote).map((f) => (
            <div key={f.title} className="card p-5">
              <f.icon className="h-5 w-5 text-kas-400" aria-hidden />
              <h2 className="mt-3 font-medium text-zinc-100">{f.title}</h2>
              <p className="mt-1.5 text-sm text-zinc-400">{f.body}</p>
            </div>
          ))}
        </div>
      </section>

      <section className="mx-auto max-w-4xl px-4 py-12">
        <h2 className="text-center text-2xl font-semibold text-zinc-100">Onboard from your terminal</h2>
        <p className="mb-6 mt-2 text-center text-sm text-zinc-400">Prefer curl to clicking? The wizard does exactly this.</p>
        <div className="space-y-3">
          <CodeBlock title="1 · register a merchant" code={curlRegisterMerchant(GATEWAY_PUBLIC_URL)} />
          <CodeBlock title="2 · create a paid endpoint" code={curlCreateEndpoint(GATEWAY_PUBLIC_URL)} />
        </div>
      </section>

      <section className="mx-auto max-w-4xl px-4 py-12">
        <h2 className="mb-6 text-center text-2xl font-semibold text-zinc-100">How a paid call works</h2>
        <ol className="grid gap-3 sm:grid-cols-4">
          {[
            ["1", "Call", "Buyer requests your proxy URL."],
            ["2", "402", "KasGate replies with exact KAS payment terms."],
            ["3", "Pay", "Buyer signs one transaction: you + fee outputs."],
            ["4", "Data", "KasGate verifies, forwards upstream, returns the response."],
          ].map(([n, title, body]) => (
            <li key={n} className="card p-4">
              <span className="font-mono text-xs text-kas-400">0{n}</span>
              <h3 className="mt-1 font-medium text-zinc-100">{title}</h3>
              <p className="mt-1 text-sm text-zinc-400">{body}</p>
            </li>
          ))}
        </ol>
      </section>
    </div>
  );
}

function Metric({ value, label }: { value: string; label: string }) {
  return (
    <div className="card p-4">
      <dd className="font-mono text-3xl font-bold text-kas-300">{value}</dd>
      <dt className="text-sm text-zinc-500">{label}</dt>
    </div>
  );
}

const PAY_STEPS = [
  { title: "Install KasWare", body: "The browser wallet that signs Kaspa payments.", href: KASWARE_URL, link: "kasware.xyz", note: null },
  { title: `Switch to ${NETWORK_LABEL}`, body: "Open KasWare, choose the network menu and pick Testnet 10.", href: null, link: null, note: null },
  { title: "Get test KAS from the faucet", body: "Paste your kaspatest: address; test coins are free and worth nothing.", href: TESTNET_FAUCET_URL, link: "faucet-tn10.kaspanet.io", note: "Faucet empty? Ask for free test KAS in the Kaspa Discord #testnet, or reply to our X post and we'll send you some." },
] as const;

function PayForReal() {
  return (
    <div className="card flex flex-col p-5 text-left sm:p-6">
      <div className="flex items-center gap-2">
        <span className="badge border-zinc-700 bg-zinc-900 font-mono text-zinc-300">wallet</span>
        <span className="font-mono text-xs text-zinc-500">on-chain payment</span>
      </div>
      <h2 className="mt-3 text-xl font-semibold text-zinc-50">Pay for real with KasWare</h2>
      <p className="mt-1.5 text-sm text-zinc-400">Connect a wallet and settle a call on-chain: your share to the merchant, the fee to the treasury, in one transaction.</p>
      <ol className="mt-4 space-y-3">
        {PAY_STEPS.map((step, index) => (
          <li key={step.title} className="flex gap-3">
            <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-kas-700 bg-kas-900/40 font-mono text-xs text-kas-300">{index + 1}</span>
            <div className="min-w-0">
              <p className="text-sm font-medium text-zinc-100">{step.title}</p>
              <p className="text-sm text-zinc-400">{step.body}</p>
              {step.href && (
                <a href={step.href} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 break-all text-sm text-kas-300 hover:text-kas-200">
                  {step.link}
                  <ExternalLink className="h-3.5 w-3.5 shrink-0" aria-hidden />
                </a>
              )}
              {step.note && <p className="mt-1 text-xs text-zinc-500">{step.note}</p>}
            </div>
          </li>
        ))}
      </ol>
      {IS_TESTNET && <FaucetBox />}
      <p className="mt-4 text-sm text-zinc-400">Then connect KasWare and pay: the beta guide walks through it, and the directory lists endpoints to call.</p>
      <div className="mt-4 flex flex-wrap gap-3">
        <Link href="/beta" className="btn-primary px-5 py-2.5">
          <Wallet className="h-4 w-4" aria-hidden />
          Connect and pay
        </Link>
        <Link href="/directory" className="btn-secondary px-5 py-2.5">
          Browse the directory
        </Link>
      </div>
    </div>
  );
}
