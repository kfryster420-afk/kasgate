import { AlertTriangle, Loader2, ServerOff } from "lucide-react";
import { KASPA_UNAVAILABLE_MESSAGE } from "@/lib/errors";
import type { ReactNode } from "react";

export function Field({ label, hint, error, children, htmlFor }: { label: string; hint?: string; error?: string | null; children: ReactNode; htmlFor: string }) {
  return (
    <div>
      <label htmlFor={htmlFor} className="label">
        {label}
      </label>
      {children}
      {error ? (
        <p className="mt-1.5 text-xs text-red-400" role="alert">
          {error}
        </p>
      ) : hint ? (
        <p className="mt-1.5 text-xs text-zinc-500">{hint}</p>
      ) : null}
    </div>
  );
}

export function ErrorBanner({ message }: { message: string | null }) {
  if (!message) return null;
  if (message === KASPA_UNAVAILABLE_MESSAGE) {
    return (
      <div className="flex items-start gap-2 rounded-lg border border-amber-800/70 bg-amber-950/40 px-3 py-2 text-sm text-amber-200" role="alert">
        <ServerOff className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
        <span>
          <strong className="font-medium">Kaspa node unavailable.</strong> Try again shortly. Nothing was charged and your data is safe.
        </span>
      </div>
    );
  }
  return (
    <div className="flex items-start gap-2 rounded-lg border border-red-900/70 bg-red-950/40 px-3 py-2 text-sm text-red-300" role="alert">
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
      <span>{message}</span>
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-2 text-sm text-zinc-400">
      <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
      {label ?? "Loading…"}
    </span>
  );
}

export function Toggle({ checked, onChange, label, id }: { checked: boolean; onChange: (next: boolean) => void; label: string; id: string }) {
  return (
    <label htmlFor={id} className="flex cursor-pointer items-center gap-3 text-sm text-zinc-300">
      <button
        id={id}
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={`relative h-6 w-11 shrink-0 rounded-full border transition ${checked ? "border-kas-500 bg-kas-500/30" : "border-zinc-700 bg-zinc-800"}`}
      >
        <span className={`absolute top-0.5 h-4 w-4 rounded-full transition-all ${checked ? "left-6 bg-kas-400" : "left-1 bg-zinc-500"}`} />
      </button>
      {label}
    </label>
  );
}

export function StatusBadge({ status }: { status: "pending" | "settled" | "rejected" | "active" | "inactive" }) {
  const tone: Record<typeof status, string> = {
    settled: "border-emerald-800 bg-emerald-950/50 text-emerald-300",
    active: "border-emerald-800 bg-emerald-950/50 text-emerald-300",
    pending: "border-amber-800 bg-amber-950/50 text-amber-300",
    rejected: "border-red-900 bg-red-950/50 text-red-300",
    inactive: "border-zinc-700 bg-zinc-900 text-zinc-400",
  };
  return <span className={`badge ${tone[status]}`}>{status}</span>;
}
