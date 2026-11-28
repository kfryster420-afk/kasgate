import Link from "next/link";
import { Hexagon } from "lucide-react";
import { GITHUB_REPO_URL, NETWORK_LABEL } from "@/lib/config";

const NAV = [
  { href: "/quickstart", label: "Quickstart" },
  { href: "/beta", label: "Beta" },
  { href: "/directory", label: "Directory" },
  { href: "/onboarding", label: "Onboarding", hideOnMobile: true },
  { href: "/dashboard", label: "Dashboard" },
] as const;

export function SiteHeader() {
  return (
    <header className="sticky top-0 z-30 border-b border-zinc-800/80 bg-zinc-950/80 backdrop-blur">
      <div className="mx-auto flex h-14 max-w-6xl items-center justify-between px-4">
        <Link href="/" className="flex items-center gap-2 font-semibold text-zinc-100">
          <Hexagon className="h-5 w-5 text-kas-400" aria-hidden />
          <span>
            Kas<span className="text-kas-400">Gate</span>
          </span>
          <span className="badge ml-1 border-zinc-700 max-sm:hidden bg-zinc-900 font-mono text-zinc-400">{NETWORK_LABEL}</span>
        </Link>
        <nav className="flex items-center gap-1 text-sm">
          {NAV.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className={`rounded-md px-3 py-1.5 text-zinc-400 transition hover:bg-zinc-800/70 hover:text-zinc-100${"hideOnMobile" in item ? " max-sm:hidden" : ""}`}
            >
              {item.label}
            </Link>
          ))}
          <Link href="/onboarding" className="btn-primary ml-2 hidden sm:inline-flex">
            Get started
          </Link>
        </nav>
      </div>
    </header>
  );
}

export function SiteFooter() {
  return (
    <footer className="border-t border-zinc-800/80 py-8 text-sm text-zinc-500">
      <div className="mx-auto flex max-w-6xl flex-col items-center justify-between gap-3 px-4 sm:flex-row">
        <p>© KasGate. Non-custodial: payments settle directly on the Kaspa network.</p>
        <div className="flex flex-wrap justify-center gap-4 font-mono text-xs">
          <Link href="/quickstart" className="hover:text-zinc-300">
            /quickstart
          </Link>
          <Link href="/beta" className="hover:text-zinc-300">
            /beta
          </Link>
          <Link href="/directory" className="hover:text-zinc-300">
            /directory
          </Link>
          <Link href="/prepaid/refunds" className="hover:text-zinc-300">
            /prepaid/refunds
          </Link>
          <a href="/directory/manifest.json" className="hover:text-zinc-300">
            /directory/manifest.json
          </a>
          <a href={GITHUB_REPO_URL} className="hover:text-zinc-300" rel="noreferrer">
            GitHub
          </a>
        </div>
      </div>
    </footer>
  );
}
