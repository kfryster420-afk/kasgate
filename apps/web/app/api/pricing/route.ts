import { getMinOutputSompi } from "@/lib/server/gateway";
import { jsonError, jsonOk } from "@/lib/server/route";
import { minPriceSompi } from "@/lib/sompi";
import { PLATFORM_FEE_BPS } from "@/lib/config";
import type { PricingInfo } from "@/lib/types";

export const dynamic = "force-dynamic";

/** Public pricing parameters: the output floor comes from the gateway's directory manifest, never from a constant. */
export async function GET() {
  const floor = await getMinOutputSompi();
  if (floor === null) return jsonError("Pricing parameters are temporarily unavailable.", 503);
  const body: PricingInfo = {
    min_output_sompi: floor.toString(),
    min_price_sompi: minPriceSompi(floor).toString(),
    platform_fee_bps: Number(PLATFORM_FEE_BPS),
  };
  return jsonOk(body);
}
