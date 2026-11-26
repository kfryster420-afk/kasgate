import { formatKas } from "@/lib/sompi";
import type { PrepaidRefundPayoutStatus, PrepaidRefundRead, PrepaidRefundStatus } from "@/lib/types";

const TONE: Record<PrepaidRefundStatus, string> = {
  requested: "border-amber-800 bg-amber-950/50 text-amber-300",
  paying: "border-violet-800 bg-violet-950/50 text-violet-300",
  payout_submitted: "border-sky-800 bg-sky-950/50 text-sky-300",
  paid: "border-emerald-800 bg-emerald-950/50 text-emerald-300",
  rejected: "border-red-900 bg-red-950/50 text-red-300",
  cancelled: "border-zinc-700 bg-zinc-900 text-zinc-400",
};

export const REFUND_STATUS_LABEL: Record<PrepaidRefundStatus, string> = {
  requested: "requested",
  paying: "paying",
  payout_submitted: "payout submitted",
  paid: "paid",
  rejected: "rejected",
  cancelled: "cancelled",
};

/** What each CONTRACT v2.13 refund status means, worded for the merchant or the buyer. */
export const REFUND_STATUS_HELP: Record<"merchant" | "buyer", Record<PrepaidRefundStatus, string>> = {
  merchant: {
    requested: "Waiting for you: pay it from your wallet, or reject it with a reason.",
    paying: "You claimed it for payment, so the buyer can't cancel. The claim lapses after 15 minutes without a payout; renewals stop 1 hour after the first claim.",
    payout_submitted: "Payout recorded but not confirmed on-chain yet. KasGate re-checks it automatically.",
    paid: "Payout confirmed on-chain. The buyer's balance was debited.",
    rejected: "You declined it. The held credit went back to the buyer's spendable balance.",
    cancelled: "The buyer withdrew it. The held credit is spendable again, after the cooldown shown below if you had claimed it.",
  },
  buyer: {
    requested: "Waiting for the merchant to pay it. The amount is held and can't be spent while it's open. Sign the status to see whether the merchant is paying it right now.",
    paying: "The merchant is paying it now, so it can't be withdrawn for the moment.",
    payout_submitted: "The merchant has sent the payout. It completes once the network confirms it.",
    paid: "Paid back on-chain to your address.",
    rejected: "The merchant declined it. The held credit is spendable again.",
    cancelled: "You withdrew it. The held credit is spendable again, after the cooldown shown below if the merchant had started paying it.",
  },
};

export function RefundStatusBadge({ status }: { status: PrepaidRefundStatus }) {
  return <span className={`badge whitespace-nowrap ${TONE[status]}`}>{REFUND_STATUS_LABEL[status]}</span>;
}

/** Late payout backstop: shown when a payout confirmed after the refund had been cancelled or rejected. */
export function LatePayoutNote({ refund }: { refund: PrepaidRefundRead }) {
  if (!refund.late_paid_from) return null;
  const shortfall = BigInt(refund.shortfall_sompi);
  return (
    <p className="mt-1 max-w-[280px] text-[11px] text-violet-300" data-late-payout>
      Late payout: confirmed after the refund was {refund.late_paid_from}.
      {shortfall > 0n ? ` Shortfall ${formatKas(shortfall)} KAS (${refund.shortfall_sompi} sompi) was already spent or expired.` : " The full amount was re-debited."}
    </p>
  );
}

const ATTEMPT_TONE: Record<PrepaidRefundPayoutStatus, string> = {
  pending: "border-sky-800 bg-sky-950/50 text-sky-300",
  confirmed: "border-emerald-800 bg-emerald-950/50 text-emerald-300",
  invalid: "border-red-900 bg-red-950/50 text-red-300",
  duplicate: "border-amber-800 bg-amber-950/50 text-amber-300",
  abandoned: "border-zinc-700 bg-zinc-900 text-zinc-400",
};

export function AttemptStatusBadge({ status }: { status: PrepaidRefundPayoutStatus }) {
  return <span className={`badge whitespace-nowrap ${ATTEMPT_TONE[status]}`}>{status}</span>;
}
