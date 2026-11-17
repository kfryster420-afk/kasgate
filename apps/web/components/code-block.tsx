import { CopyButton } from "./copy-button";

export function CodeBlock({ code, title }: { code: string; title?: string }) {
  return (
    <div className="overflow-hidden rounded-lg border border-zinc-800 bg-zinc-950">
      <div className="flex items-center justify-between border-b border-zinc-800 bg-zinc-900/70 px-3 py-1.5">
        <span className="font-mono text-xs text-zinc-500">{title ?? "shell"}</span>
        <CopyButton value={code} className="!py-1" />
      </div>
      <pre className="overflow-x-auto p-4 font-mono text-[13px] leading-relaxed text-zinc-300">
        <code>{code}</code>
      </pre>
    </div>
  );
}
