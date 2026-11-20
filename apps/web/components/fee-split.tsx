import { computeFeeSplit, flatFeeMaxPriceSompi, formatBps, formatKas, formatSompi, isFlatFee, shareBps } from "@/lib/sompi";

/**
 * Live price breakdown (integer maths, CONTRACT v2.1): fee = max(ceil(price x 1.5%), F). Below the flat-fee threshold the
 * treasury takes the flat floor F (shown in KAS, formatted from F); above it the fee is 1.5%.
 */
export function FeeSplitView({ totalSompi, minOutput }: { totalSompi: bigint; minOutput: bigint }) {
  const split = computeFeeSplit(totalSompi, minOutput);
  const flat = isFlatFee(totalSompi, minOutput);
  const feeBps = shareBps(split.fee, split.total);
  const merchantBps = shareBps(split.merchant, split.total);
  const feePct = formatBps(feeBps);
  const merchantPct = formatBps(merchantBps);
  const barFee = Math.max(Number(feeBps) / 100, 0.5);

  return (
    <div className="space-y-3 rounded-lg border border-zinc-800 bg-zinc-950 p-4">
      <div className="flex items-center justify-between gap-2 text-xs">
        <span className={`badge ${flat ? "border-amber-800 bg-amber-950/50 text-amber-300" : "border-zinc-700 bg-zinc-900 text-zinc-300"}`}>
          {flat ? `flat ${formatKas(minOutput)} KAS fee` : "1.5% fee"}
        </span>
        <span className="font-mono text-zinc-500">
          {merchantPct}% / {feePct}%
        </span>
      </div>
      <div className="flex h-2.5 overflow-hidden rounded-full bg-zinc-800" role="img" aria-label={`You receive ${merchantPct}%, platform fee ${feePct}%`}>
        <div className="bg-kas-400" style={{ width: `${100 - barFee}%` }} />
        <div className="bg-amber-400" style={{ width: `${barFee}%` }} />
      </div>
      <dl className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-3">
        <Row label="Payer sends" tone="text-zinc-100" kas={formatKas(split.total)} sompi={formatSompi(split.total)} />
        <Row label={`You receive · ${merchantPct}%`} tone="text-kas-300" kas={formatKas(split.merchant)} sompi={formatSompi(split.merchant)} />
        <Row label={`Platform fee · ${feePct}%`} tone="text-amber-300" kas={formatKas(split.fee)} sompi={formatSompi(split.fee)} />
      </dl>
      <p className="text-xs text-zinc-500">
        {flat
          ? `Prices up to ${formatKas(flatFeeMaxPriceSompi(minOutput))} KAS pay the flat ${formatKas(minOutput)} KAS fee (the on-chain output floor); above that the fee is 1.5%.`
          : "1.5% of the price, rounded up, and never below the on-chain output floor."}{" "}
        Two exact outputs in one transaction: outputs[0] to you, outputs[1] to the KasGate treasury. Non-custodial.
      </p>
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
