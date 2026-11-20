import type { Metadata } from "next";
import { OnboardingWizard } from "@/components/onboarding-wizard";
import { gatewayRequest } from "@/lib/server/gateway";
import { readSessionToken } from "@/lib/server/session";
import type { MerchantRead } from "@/lib/types";

export const metadata: Metadata = { title: "Onboarding" };
export const dynamic = "force-dynamic";

export default async function OnboardingPage() {
  const token = await readSessionToken();
  let merchant: MerchantRead | null = null;
  if (token) {
    const result = await gatewayRequest<MerchantRead>("/v1/merchants/me", { bearer: token });
    if (result.ok) merchant = result.data;
  }
  return <OnboardingWizard initialMerchant={merchant} />;
}
