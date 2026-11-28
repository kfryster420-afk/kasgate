import { formatKas, formatSompi } from "@/lib/sompi";

/** An exact sompi amount shown as KAS with the raw sompi underneath. */
export function KasAmount({ sompi, tone = "text-zinc-200", align = "right" }: { sompi: string; tone?: string; align?: "left" | "right" }) {
  return (
    <div className={align === "right" ? "text-right" : "text-left"}>
      <div className={`font-mono text-xs ${tone}`}>{formatKas(BigInt(sompi))} KAS</div>
      <div className="font-mono text-[11px] text-zinc-600">{formatSompi(BigInt(sompi))} sompi</div>
    </div>
  );
}
