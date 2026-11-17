import type { Metadata } from "next";
import { DirectoryExplorer } from "@/components/directory-explorer";
import { gatewayRequest } from "@/lib/server/gateway";
import { formatKas } from "@/lib/sompi";
import type { DirectoryManifest } from "@/lib/types";

export const metadata: Metadata = {
  title: "API Directory",
  description: "Search pay-per-call APIs priced in KAS. Machine-readable feed at /directory/manifest.json.",
  alternates: { types: { "application/json": "/directory/manifest.json" } },
};
export const dynamic = "force-dynamic";

export default async function DirectoryPage() {
  const result = await gatewayRequest<DirectoryManifest>("/v1/directory", { revalidate: 30 });

  return (
    <div className="mx-auto max-w-6xl px-4 py-10">
      <div className="mb-8 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold text-zinc-100">Public AI-agent API directory</h1>
          <p className="mt-1 max-w-2xl text-sm text-zinc-400">
            Every endpoint here answers HTTP 402 with exact payment terms. Agents can pay per call in KAS with no accounts or API keys.
          </p>
        </div>
        <a href="/directory/manifest.json" className="btn-secondary font-mono text-xs">
          /directory/manifest.json
        </a>
      </div>

      {result.ok ? (
        <>
          <p className="mb-4 font-mono text-xs text-zinc-500">
            network {result.data.network} · scheme {result.data.scheme} · platform fee {result.data.platform_fee_bps / 100}% · output floor {formatKas(result.data.min_output_sompi)} KAS
          </p>
          <DirectoryExplorer entries={result.data.endpoints} />
        </>
      ) : (
        <div className="card border-red-900/60 p-8 text-center text-sm text-red-300" role="alert">
          The directory is temporarily unavailable ({result.message}). Please retry in a moment.
        </div>
      )}
    </div>
  );
}
