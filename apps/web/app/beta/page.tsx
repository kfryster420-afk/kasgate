import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";
import { ExternalLink, FlaskConical } from "lucide-react";
import { BETA_LINKS } from "@/lib/beta";
import { NETWORK_LABEL } from "@/lib/config";
import { getMinOutputSompi } from "@/lib/server/gateway";
import { flatFeeMaxPriceSompi, formatKas, formatSompi, minPriceSompi } from "@/lib/sompi";

export const metadata: Metadata = {
  title: "Testnet beta",
  description: "How to test KasGate on Kaspa testnet-10: install KasWare, get free TN10 KAS, register as a merchant, pay an endpoint, report bugs.",
};
export const dynamic = "force-dynamic";

export default async function BetaPage() {
  const floor = await getMinOutputSompi();

  return (
    <div className="mx-auto max-w-3xl px-4 py-12">
      <span className="badge border-amber-800 bg-amber-950/50 font-mono text-amber-300">
        <FlaskConical className="mr-1 h-3 w-3" aria-hidden />
        testnet beta · {NETWORK_LABEL}
      </span>
      <h1 className="mt-4 text-3xl font-bold tracking-tight text-zinc-50">KasGate testnet beta</h1>
      <p className="mt-4 text-zinc-400">
        KasGate wraps any HTTP API in a pay-per-call paywall settled on Kaspa. A merchant registers an upstream URL and a price in KAS and gets a proxy URL. A buyer
        (a person or an AI agent) calling that URL receives HTTP 402 with exact payment terms, signs one Kaspa transaction that pays the merchant and the platform fee
        directly, and retries; KasGate verifies the payment on-chain and forwards the call upstream. Funds never pass through KasGate.
      </p>
      <div className="mt-6 rounded-lg border border-amber-800/70 bg-amber-950/40 px-4 py-3 text-sm text-amber-200">
        This beta runs on Kaspa testnet-10 (TN10). Every payment uses free testnet KAS, which has no value. KasGate has not had an independent security audit; do not use
        it with mainnet funds.
      </div>

      <h2 className="mt-12 text-xl font-semibold text-zinc-100">Tester guide</h2>
      <ol className="mt-6 space-y-4">
        <Step n={1} title="Install KasWare">
          <p>
            Install the KasWare wallet browser extension from the <Ext href={BETA_LINKS.kaswareChromeStore}>Chrome Web Store</Ext> (linked from the official{" "}
            <Ext href={BETA_LINKS.kaswareDocs}>KasWare docs</Ext>; project site <Ext href={BETA_LINKS.kaswareSite}>kasware.xyz</Ext>). Create or import a wallet.
          </p>
        </Step>

        <Step n={2} title="Switch KasWare to Testnet 10">
          <p>
            In KasWare, change the network to <strong className="text-zinc-200">Testnet 10</strong>. KasGate expects the network{" "}
            <code className="font-mono text-kas-300">kaspa:testnet-10</code> and addresses starting with <code className="font-mono text-kas-300">kaspatest:</code>. The
            sign-in step refuses other networks (mainnet, Testnet 11, Testnet 12) when KasWare reports them.
          </p>
        </Step>

        <Step n={3} title="Get free TN10 KAS from the faucet">
          <p>
            Copy your <code className="font-mono text-kas-300">kaspatest:</code> address from KasWare and request coins from the TN10 faucet:{" "}
            <Ext href={BETA_LINKS.tn10Faucet}>{BETA_LINKS.tn10Faucet}</Ext>. This is the faucet listed on the official{" "}
            <Ext href={BETA_LINKS.kaspaWikiTestnets}>Kaspa Wiki testnets page</Ext>. If the faucet is dry, the wiki suggests asking in the{" "}
            <code className="font-mono">#testnet</code> channel on the Kaspa Discord.
          </p>
        </Step>

        <Step n={4} title="Register as a merchant and create a priced endpoint">
          <p>
            Open <Link href="/onboarding" className="text-kas-300 underline decoration-kas-700 underline-offset-2 hover:text-kas-200">/onboarding</Link>, register with
            KasWare (it signs a login challenge, it does not send funds), then add your upstream API URL and a price in KAS. You get a proxy URL and an API key; the key is
            shown once, so store it.
          </p>
          <PricingRules floor={floor} />
        </Step>

        <Step n={5} title="Call the endpoint as a buyer">
          <p>
            Call your proxy URL (or any endpoint in the <Link href="/directory" className="text-kas-300 underline decoration-kas-700 underline-offset-2 hover:text-kas-200">directory</Link>)
            with the Python buyer SDK, <code className="font-mono text-kas-300">kasgate-client</code>. It answers the 402, enforces a client-side spend cap, signs the
            payment with a funded TN10 key and retries. Setup and usage are in the <Ext href={BETA_LINKS.buyerSdk}>buyer SDK README</Ext>; the full walkthrough is in the{" "}
            <Ext href={BETA_LINKS.runbook}>runbook</Ext>. Use a separate buyer wallet from your merchant wallet.
          </p>
          <p className="mt-2">
            Settlement happens on-chain before the upstream is called. If your upstream fails, the buyer is not refunded automatically; retrying with the same payment
            redelivers without charging again, within a limited window.
          </p>
        </Step>

        <Step n={6} title="Report bugs">
          <p>
            Open an issue at <Ext href={BETA_LINKS.newIssue}>github.com/kfryster420-afk/kasgate/issues</Ext>. Include the page or endpoint slug, what you expected, what
            happened, and the transaction ID if a payment was involved. Never paste private keys, seed phrases or API keys into an issue.
          </p>
        </Step>
      </ol>
    </div>
  );
}

function Step({ n, title, children }: { n: number; title: string; children: ReactNode }) {
  return (
    <li className="card p-5">
      <span className="font-mono text-xs text-kas-400">{String(n).padStart(2, "0")}</span>
      <h3 className="mt-1 font-medium text-zinc-100">{title}</h3>
      <div className="mt-2 text-sm leading-relaxed text-zinc-400">{children}</div>
    </li>
  );
}

function Ext({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-0.5 text-kas-300 underline decoration-kas-700 underline-offset-2 hover:text-kas-200">
      {children}
      <ExternalLink className="h-3 w-3" aria-hidden />
    </a>
  );
}

function PricingRules({ floor }: { floor: bigint | null }) {
  if (floor === null) {
    return (
      <ul className="mt-3 list-disc space-y-1 pl-5">
        <li>
          F is the gateway&apos;s <code className="font-mono">min_output_sompi</code> (live pricing is unavailable right now; the onboarding form shows it).
        </li>
        <li>Platform fee = max(ceil(price × 150 / 10,000), F): 1.5% with a flat minimum of F.</li>
        <li>Minimum endpoint price = 3F, because the merchant output must be at least 2F.</li>
      </ul>
    );
  }
  return (
    <ul className="mt-3 list-disc space-y-1 pl-5">
      <li>
        F = <code className="font-mono">min_output_sompi</code> = {formatSompi(floor)} sompi ({formatKas(floor)} KAS).
      </li>
      <li>
        Platform fee = max(ceil(price × 150 / 10,000), F): a flat {formatKas(floor)} KAS on prices up to {formatKas(flatFeeMaxPriceSompi(floor))} KAS, 1.5% above that.
      </li>
      <li>
        Minimum endpoint price = 3F = {formatSompi(minPriceSompi(floor))} sompi ({formatKas(minPriceSompi(floor))} KAS), because the merchant output must be at least 2F ={" "}
        {formatKas(2n * floor)} KAS.
      </li>
    </ul>
  );
}
