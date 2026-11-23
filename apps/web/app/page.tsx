import { ArrowRight, Bot, FileSpreadsheet, Gauge, Hexagon, Lock, Zap } from "lucide-react";
import Link from "next/link";
import { CodeBlock } from "@/components/code-block";
import { GATEWAY_PUBLIC_URL, IS_TESTNET, NETWORK_LABEL } from "@/lib/config";
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
      <section className="mx-auto max-w-6xl px-4 pb-16 pt-20 text-center">
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
          <Link href="/onboarding" className="btn-primary px-6 py-3 text-base">
            Monetize an API
            <ArrowRight className="h-4 w-4" aria-hidden />
          </Link>
          <Link href="/directory" className="btn-secondary px-6 py-3 text-base">
            Browse the directory
          </Link>
          <Link href="/beta" className="btn-secondary px-6 py-3 text-base">
            Beta guide
          </Link>
        </div>
        <dl className="mx-auto mt-14 grid max-w-3xl grid-cols-3 gap-4 text-left">
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
