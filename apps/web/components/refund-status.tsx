import type { PrepaidRefundStatus } from "@/lib/types";

const TONE: Record<PrepaidRefundStatus, string> = {
  requested: "border-amber-800 bg-amber-950/50 text-amber-300",
  payout_submitted: "border-sky-800 bg-sky-950/50 text-sky-300",
  paid: "border-emerald-800 bg-emerald-950/50 text-emerald-300",
  rejected: "border-red-900 bg-red-950/50 text-red-300",
  cancelled: "border-zinc-700 bg-zinc-900 text-zinc-400",
};

export const REFUND_STATUS_LABEL: Record<PrepaidRefundStatus, string> = {
  requested: "requested",
  payout_submitted: "payout submitted",
  paid: "paid",
  rejected: "rejected",
  cancelled: "cancelled",
};

/** What each CONTRACT v2.13 refund status means, worded for the merchant or the buyer. */
export const REFUND_STATUS_HELP: Record<"merchant" | "buyer", Record<PrepaidRefundStatus, string>> = {
  merchant: {
    requested: "Waiting for you: pay it from your wallet, or reject it with a reason.",
    payout_submitted: "Payout recorded but not visible on-chain yet. KasGate re-checks it automatically.",
    paid: "Payout seen on-chain. The buyer's balance was debited.",
    rejected: "You declined it. The held credit went back to the buyer's spendable balance.",
    cancelled: "The buyer withdrew it. The held credit is spendable again.",
  },
  buyer: {
    requested: "Waiting for the merchant to pay it. The amount is held and can't be spent while it's open.",
    payout_submitted: "The merchant has sent the payout. It completes once the network shows it.",
    paid: "Paid back on-chain to your address.",
    rejected: "The merchant declined it. The held credit is spendable again.",
    cancelled: "You withdrew it. The held credit is spendable again.",
  },
};

export function RefundStatusBadge({ status }: { status: PrepaidRefundStatus }) {
  return <span className={`badge whitespace-nowrap ${TONE[status]}`}>{REFUND_STATUS_LABEL[status]}</span>;
}
