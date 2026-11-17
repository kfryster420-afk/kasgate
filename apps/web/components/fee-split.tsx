import { computeFeeSplit, formatKas, formatSompi } from "@/lib/sompi";

/** Live 98.5% / 1.5% breakdown for a total price in sompi (integer maths). */
export function FeeSplitView({ totalSompi }: { totalSompi: bigint }) {
  const split = computeFeeSplit(totalSompi);
  return (
    <div className="space-y-3 rounded-lg border border-zinc-800 bg-zinc-950 p-4">
      <div className="flex h-2.5 overflow-hidden rounded-full bg-zinc-800" role="img" aria-label="Fee split: 98.5% to you, 1.5% platform fee">
        <div className="bg-kas-400" style={{ width: "98.5%" }} />
        <div className="bg-amber-400" style={{ width: "1.5%" }} />
      </div>
      <dl className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-3">
        <Row label="Payer sends" tone="text-zinc-100" kas={formatKas(split.total)} sompi={formatSompi(split.total)} />
        <Row label="You receive · 98.5%" tone="text-kas-300" kas={formatKas(split.merchant)} sompi={formatSompi(split.merchant)} />
        <Row label="Platform fee · 1.5%" tone="text-amber-300" kas={formatKas(split.fee)} sompi={formatSompi(split.fee)} />
      </dl>
      <p className="text-xs text-zinc-500">Two exact on-chain outputs in one transaction: outputs[0] to you, outputs[1] to the KasGate treasury. Non-custodial.</p>
    </div>
  );
}

function Row({ label, tone, kas, sompi }: { label: string; tone: string; kas: string; sompi: string }) {
  return (
    <div>
      <dt className="text-xs text-zinc-500">{label}</dt>
      <dd className={`font-mono text-base font-semibold ${tone}`}>{kas} KAS</dd>
      <dd className="font-mono text-xs text-zinc-500">{sompi} sompi</dd>
    </div>
  );
}
