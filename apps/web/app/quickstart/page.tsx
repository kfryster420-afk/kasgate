import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";
import { ExternalLink, Rocket } from "lucide-react";
import { CodeBlock } from "@/components/code-block";
import { FaucetBox } from "@/components/faucet-box";
import { DEMO_ENDPOINT_SLUG, GATEWAY_PUBLIC_URL, GITHUB_REPO_URL, IS_TESTNET, KASWARE_URL, NETWORK_LABEL } from "@/lib/config";

export const metadata: Metadata = {
  title: "Quickstart",
  description: "Live in 5 minutes: sell a paid API with KasGate, or call one from Python and pay in KAS. Testnet-10.",
};

const SDK_GIT = `git+${GITHUB_REPO_URL}.git`;
const INSTALL_SDK = `pip install "${SDK_GIT}#subdirectory=packages/buyer-sdk"`;
const INSTALL_MCP = `pip install "${SDK_GIT}#subdirectory=packages/mcp-server"`;
const DEMO_URL = `${GATEWAY_PUBLIC_URL}/p/${DEMO_ENDPOINT_SLUG}`;

const CURL_402 = `# Call your paid URL with no payment: the gateway answers HTTP 402 with the exact terms.
curl -i ${GATEWAY_PUBLIC_URL}/p/<your-slug>

# HTTP/2 402
# payment-required: <Base64 JSON: amount, payTo, platformFee, nonce, expiresAt>
# x-kasgate-trial-offer: <only when you enabled free trial calls>
# {"error":"payment_required","message":"payment is required to access this resource", ...}

# The same terms as plain JSON, nothing is paid:
curl -s ${GATEWAY_PUBLIC_URL}/v1/quote/<your-slug>`;

const CURL_DEMO = `# Try it on the live demo endpoint (price 0.6 KAS):
curl -i ${DEMO_URL}`;

const KEYGEN = `python - <<'PY'
import secrets
import kaspa

key = secrets.token_hex(32)  # your buyer private key: keep it secret, testnet only
print("address:", kaspa.PrivateKey(key).to_address("testnet").to_string())
open("buyer.key", "w").write(key)
PY
chmod 600 buyer.key`;

const SDK_EXAMPLE = `import os

from kasgate_client import KasGateSyncClient, receipt_of, trial_receipt_of

KEY = os.environ["KASGATE_BUYER_KEY"]  # 64 hex chars: the funded kaspatest: key from buyer.key
URL = "${DEMO_URL}"

# 1. Free trial call: signs a trial text and never spends KAS (while the endpoint offers one).
with KasGateSyncClient(KEY, network="kaspa:testnet-10") as client:
    response = client.trial_request("GET", URL)
    print(response.status_code, trial_receipt_of(response))

# 2. Paid call: answers the 402 with one Kaspa transaction, refusing anything above 1 KAS.
with KasGateSyncClient(KEY, network="kaspa:testnet-10", use_free_trials=False) as client:
    response = client.get(URL, max_price_kas="1")
    print(response.status_code, receipt_of(response))`;

const MCP_CONFIG = `{
  "mcpServers": {
    "kasgate": {
      "command": "kasgate-mcp",
      "env": {
        "KASGATE_BUYER_PRIVATE_KEY_FILE": "/home/me/.config/kasgate/buyer.key",
        "KASGATE_GATEWAY_URL": "${GATEWAY_PUBLIC_URL}",
        "KASGATE_NETWORK": "kaspa:testnet-10",
        "KASGATE_SESSION_BUDGET_KAS": "5"
      }
    }
  }
}`;

function TextLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <Link href={href} className="text-kas-300 underline decoration-kas-700 underline-offset-2 hover:text-kas-200">
      {children}
    </Link>
  );
}

function Ext({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-kas-300 underline decoration-kas-700 underline-offset-2 hover:text-kas-200">
      {children}
      <ExternalLink className="h-3 w-3" aria-hidden />
    </a>
  );
}

function Code({ children }: { children: ReactNode }) {
  return <code className="font-mono text-kas-300">{children}</code>;
}

function Step({ n, title, children }: { n: number; title: string; children: ReactNode }) {
  return (
    <li className="card flex gap-4 p-5">
      <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-kas-700 bg-kas-900/40 font-mono text-sm text-kas-300">{n}</span>
      <div className="min-w-0 flex-1 space-y-3 text-sm text-zinc-400">
        <h3 className="text-base font-medium text-zinc-100">{title}</h3>
        {children}
      </div>
    </li>
  );
}

export default function QuickstartPage() {
  return (
    <div className="mx-auto max-w-3xl px-4 py-12">
      <span className="badge border-amber-800 bg-amber-950/50 font-mono text-amber-300">
        <Rocket className="mr-1 h-3 w-3" aria-hidden />
        quickstart · testnet beta · {NETWORK_LABEL}
      </span>
      <h1 className="mt-4 text-3xl font-bold tracking-tight text-zinc-50">Live in 5 minutes</h1>
      <p className="mt-4 text-zinc-400">
        Two tracks: put a paywall in front of your API and get paid in KAS per call, or call a paid API from Python or an MCP client and pay on-chain. Everything here
        runs on Kaspa testnet-10 with free test coins that have no value. KasGate has not had an independent security audit; do not use mainnet funds.
      </p>
      <p className="mt-3 text-sm text-zinc-400">
        The platform fee is <span className="text-zinc-200">max(1.5%, 0.2 KAS)</span> per paid call, sent to the treasury in the same transaction as the merchant&apos;s share.
        The minimum price is <span className="text-zinc-200">0.6 KAS</span>. KasGate never holds your funds.
      </p>
      <nav aria-label="Tracks" className="mt-6 flex flex-wrap gap-3">
        <a href="#sellers" className="btn-primary px-5 py-2.5">
          Sell your API
        </a>
        <a href="#buyers" className="btn-secondary px-5 py-2.5">
          Pay as an AI agent or developer
        </a>
      </nav>

      <section id="sellers" className="mt-14 scroll-mt-20">
        <h2 className="text-2xl font-semibold text-zinc-50">Sellers: your first paid API in 5 minutes</h2>
        <p className="mt-2 text-sm text-zinc-400">You need a public HTTP API and the KasWare wallet on Testnet 10. No code changes on your side.</p>
        <ol className="mt-6 space-y-4">
          <Step n={1} title="Sign in with KasWare">
            <p>
              Install <Ext href={KASWARE_URL}>KasWare</Ext>, create a wallet and switch it to <strong className="text-zinc-200">Testnet 10</strong>. First time here: open{" "}
              <TextLink href="/onboarding">/onboarding</TextLink>, choose a display name and sign the login challenge. KasWare signs a message; it does not send funds.
              Your API key is shown once, so store it. Already registered: <TextLink href="/login">/login</TextLink> takes you to your{" "}
              <TextLink href="/dashboard">dashboard</TextLink>.
            </p>
            <p>Your payout address is the wallet you sign in with. Every payment pays your share straight to it.</p>
          </Step>
          <Step n={2} title="Create an endpoint">
            <p>In onboarding step 2, or with <strong className="text-zinc-200">New endpoint</strong> in the dashboard, fill in:</p>
            <ul className="list-disc space-y-1 pl-5">
              <li>
                <strong className="text-zinc-200">Upstream API URL</strong>: the public API KasGate forwards paid calls to (no localhost or private IPs). An optional secret
                upstream header keeps your own API key out of buyers&apos; hands.
              </li>
              <li>
                <strong className="text-zinc-200">Price per call</strong>: in KAS (or USD), at least 0.6 KAS for an on-chain payment. The fee is max(1.5%, 0.2 KAS) of the price
                and the rest is yours.
              </li>
              <li>
                <strong className="text-zinc-200">Free trial calls per buyer</strong> (optional): let each buyer key try your API before paying, with a daily cap across all
                buyers.
              </li>
              <li>Name, category, description and a slug (auto-generated if empty). Leave it listed to appear in the public directory and machine-readable catalog.</li>
            </ul>
          </Step>
          <Step n={3} title="Copy your /p/<slug> URL">
            <p>
              You get a proxy URL of the form <Code>{GATEWAY_PUBLIC_URL}/p/&lt;slug&gt;</Code>. Share it, or let AI agents find it through the{" "}
              <TextLink href="/directory">directory</TextLink>. Any path or query after the slug is forwarded to your upstream.
            </p>
          </Step>
          <Step n={4} title="Test it with curl: you should see the 402">
            <CodeBlock title="shell" code={CURL_402} />
            <p>
              A 402 with a <Code>payment-required</Code> header means your paywall is live. A paying buyer signs one transaction, outputs[0] to you and outputs[1] to the
              treasury, retries, and gets your API&apos;s response. Watch settlements and revenue in the dashboard.
            </p>
            <CodeBlock title="shell" code={CURL_DEMO} />
          </Step>
        </ol>
      </section>

      <section id="buyers" className="mt-16 scroll-mt-20">
        <h2 className="text-2xl font-semibold text-zinc-50">Buyers and AI agents: pay for a call from Python</h2>
        <p className="mt-2 text-sm text-zinc-400">
          Python 3.10 or newer. The SDK answers the 402, enforces your spend cap, signs one Kaspa transaction and retries.
        </p>
        <ol className="mt-6 space-y-4">
          <Step n={1} title="Install the Python SDK">
            <CodeBlock title="shell" code={INSTALL_SDK} />
            <p>
              This installs <Code>kasgate-client</Code> from the public repository (it is not on PyPI yet). Source and README:{" "}
              <Ext href={`${GITHUB_REPO_URL}/tree/main/packages/buyer-sdk`}>packages/buyer-sdk</Ext>.
            </p>
          </Step>
          <Step n={2} title="Create a testnet key">
            <CodeBlock title="shell" code={KEYGEN} />
            <p>Use a separate key from your seller wallet. It prints your <Code>kaspatest:</Code> address; keep <Code>buyer.key</Code> private.</p>
          </Step>
          <Step n={3} title="Get test KAS from the on-site faucet">
            <p>
              Paste your <Code>kaspatest:</Code> address below. One claim per address and per IP every 24 hours. If the faucet is empty, the official TN10 faucet and the Kaspa
              Discord <Code>#testnet</Code> channel are the fallback (see the <TextLink href="/beta">beta guide</TextLink>).
            </p>
            {IS_TESTNET ? <FaucetBox /> : null}
          </Step>
          <Step n={4} title="Make a free trial call and a paid call">
            <CodeBlock title="python · KasGateSyncClient" code={SDK_EXAMPLE} />
            <p>
              <Code>trial_request</Code> uses a free trial and never pays; each buyer key gets as many trial calls as the endpoint offers (the demo offers 1). The second client pays: the call costs 0.6 KAS plus
              a small network fee, and <Code>receipt_of</Code> returns the transaction id you can open on the{" "}
              <Ext href="https://tn10.kaspa.stream">TN10 explorer</Ext>. Run <Code>export KASGATE_BUYER_KEY=$(cat buyer.key)</Code> first. Async code uses{" "}
              <Code>KasGateClient</Code> with the same arguments.
            </p>
          </Step>
        </ol>

        <h3 className="mt-10 text-xl font-semibold text-zinc-100">Prefer an MCP client?</h3>
        <p className="mt-2 text-sm text-zinc-400">
          <Code>kasgate-mcp</Code> is a stdio MCP server that lets an AI agent search the catalog, quote a price and pay under a session budget. Install it after the SDK, then add it to
          your MCP client config. Prefer the key file (mode 600) over putting the key in the config.
        </p>
        <div className="mt-4 space-y-4">
          <CodeBlock title="shell" code={INSTALL_MCP} />
          <CodeBlock title="mcp client config (json)" code={MCP_CONFIG} />
        </div>
        <p className="mt-3 text-sm text-zinc-400">
          Tools include <Code>search_catalog</Code>, <Code>inspect_endpoint</Code>, <Code>quote_endpoint</Code>, <Code>get_budget</Code>, <Code>call_free_trial</Code> and{" "}
          <Code>call_with_budget</Code>. Full list and environment variables:{" "}
          <Ext href={`${GITHUB_REPO_URL}/tree/main/packages/mcp-server`}>packages/mcp-server</Ext>.
        </p>
      </section>

      <section className="mt-16 rounded-lg border border-zinc-800 bg-zinc-900/40 p-5 text-sm text-zinc-400">
        <h2 className="text-lg font-semibold text-zinc-100">Good to know</h2>
        <ul className="mt-3 list-disc space-y-1.5 pl-5">
          <li>Testnet-10 only for now: network <Code>kaspa:testnet-10</Code>, addresses starting with <Code>kaspatest:</Code>. Mainnet is not launched.</li>
          <li>Fee: max(1.5%, 0.2 KAS) per paid call. Minimum price 0.6 KAS. Prepaid credit allows smaller prices; see the SDK README.</li>
          <li>
            More: <TextLink href="/beta">beta guide</TextLink>, <TextLink href="/directory">directory</TextLink>,{" "}
            <Ext href={`${GITHUB_REPO_URL}/issues/new?template=bug_report.yml`}>report a bug</Ext>.
          </li>
        </ul>
      </section>
    </div>
  );
}
