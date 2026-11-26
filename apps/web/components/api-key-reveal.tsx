"use client";

import { KeyRound, TriangleAlert } from "lucide-react";
import { useState } from "react";
import { CopyButton } from "./copy-button";

/**
 * One-time display of a long-lived API key. The key lives only in the caller's React state (never storage) and is
 * discarded by `onDone`. There is no way to view it again; rotating issues a new one.
 */
export function ApiKeyReveal({
  apiKey,
  title,
  doneLabel,
  onDone,
  note,
  copyLabel = "Copy key",
}: {
  apiKey: string;
  title: string;
  doneLabel: string;
  onDone: () => void;
  note?: string;
  copyLabel?: string;
}) {
  const [saved, setSaved] = useState(false);
  return (
    <div className="card border-amber-700/50 p-6" role="alertdialog" aria-labelledby="api-key-title">
      <div className="flex items-center gap-2 text-amber-300">
        <KeyRound className="h-5 w-5" aria-hidden />
        <h2 id="api-key-title" className="text-lg font-medium">
          {title}
        </h2>
      </div>
      <p className="mt-2 flex items-start gap-2 text-sm text-zinc-300">
        <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-400" aria-hidden />
        {note ??
          "This is the only time the key is shown. Store it in a password manager or secrets store now. It is for scripts and automation; the dashboard signs you in with your wallet."}
      </p>
      <div className="mt-4 flex flex-col gap-2 sm:flex-row sm:items-center">
        <code className="flex-1 select-all overflow-x-auto rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2.5 font-mono text-sm text-amber-200">{apiKey}</code>
        <CopyButton value={apiKey} label={copyLabel} />
      </div>
      <label className="mt-4 flex cursor-pointer items-center gap-2 text-sm text-zinc-300">
        <input type="checkbox" checked={saved} onChange={(e) => setSaved(e.target.checked)} className="h-4 w-4 accent-kas-400" />
        I have saved this {copyLabel === "Copy key" ? "key" : "secret"} somewhere safe
      </label>
      <button type="button" className="btn-primary mt-4" disabled={!saved} onClick={onDone}>
        {doneLabel}
      </button>
    </div>
  );
}
