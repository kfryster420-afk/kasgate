import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import { BetaBanner } from "@/components/beta-banner";
import { SiteFooter, SiteHeader } from "@/components/site-chrome";
import { BETA_BANNER_BOOT_SCRIPT } from "@/lib/beta";
import { IS_TESTNET } from "@/lib/config";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "KasGate — monetize any API with Kaspa", template: "%s · KasGate" },
  description:
    "Put any HTTP API behind a non-custodial Kaspa paywall in 60 seconds. x402-style pay-per-call, 1.5% fee (small flat minimum on low prices), zero code.",
  applicationName: "KasGate",
};

export const viewport: Viewport = {
  themeColor: "#09090b",
  colorScheme: "dark",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className="dark" suppressHydrationWarning>
      <head>{IS_TESTNET ? <script dangerouslySetInnerHTML={{ __html: BETA_BANNER_BOOT_SCRIPT }} /> : null}</head>
      <body className="flex min-h-screen flex-col">
        {IS_TESTNET ? <BetaBanner /> : null}
        <SiteHeader />
        <main className="flex-1">{children}</main>
        <SiteFooter />
      </body>
    </html>
  );
}
