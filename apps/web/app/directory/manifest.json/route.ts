import { gatewayRaw } from "@/lib/server/gateway";

export const dynamic = "force-dynamic";

const CACHE_CONTROL = "public, max-age=30, s-maxage=30, stale-while-revalidate=60";

/** Machine-readable directory feed: the gateway's DirectoryManifest, byte-for-byte unchanged. */
export async function GET(): Promise<Response> {
  const upstream = await gatewayRaw("/v1/directory", { revalidate: 30 });
  if (!upstream || !upstream.ok) {
    return Response.json(
      { error: "directory_unavailable", message: "The KasGate gateway directory is temporarily unavailable." },
      { status: 503, headers: { "Cache-Control": "no-store", "Retry-After": "10" } },
    );
  }
  const body = await upstream.text();
  return new Response(body, {
    status: 200,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": CACHE_CONTROL,
      "Access-Control-Allow-Origin": "*",
    },
  });
}
