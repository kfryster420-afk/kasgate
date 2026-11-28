import type { Metadata } from "next";
import { BuyerRefunds } from "@/components/buyer-refunds";

export const metadata: Metadata = {
  title: "Prepaid refunds",
  description: "Ask a KasGate merchant to pay back unused prepaid credit, withdraw a request, and follow its status.",
};
export const dynamic = "force-dynamic";

type Props = { searchParams: Promise<Record<string, string | string[] | undefined>> };

export default async function PrepaidRefundsPage({ searchParams }: Props) {
  const params = await searchParams;
  const slug = typeof params.slug === "string" ? params.slug.slice(0, 64) : "";
  return (
    <div className="mx-auto max-w-4xl px-4 py-10">
      <h1 className="text-2xl font-semibold text-zinc-100">Prepaid credit refunds</h1>
      <p className="mt-1 max-w-2xl text-sm text-zinc-400">
        Bought prepaid credit for an API and won&apos;t use all of it? Ask the merchant to pay the unused part back to your address. You sign the request with the same
        KasWare account that made the deposit; it moves no coins. The merchant then pays you from their own wallet, with no platform fee.
      </p>
      <BuyerRefunds initialSlug={slug} />
    </div>
  );
}
