import type { DeliveryFailure, FailedDeliveryCreditReason, PrepaidLedgerEntryKind } from "./types";

/** Why redelivery was closed when a failed paid call was credited (CONTRACT v2.14). */
export const CREDIT_REASON_LABEL: Record<FailedDeliveryCreditReason, string> = {
  attempts_exhausted: "redelivery attempts used up",
  window_expired: "redelivery window closed",
};

export const DELIVERY_FAILURE_LABEL: Record<DeliveryFailure, string> = {
  upstream_5xx: "upstream 5xx",
  connect_error: "connection error",
  timeout: "timeout",
  oversized: "response too large",
};

export const LEDGER_KIND_LABEL: Record<PrepaidLedgerEntryKind, string> = {
  refund: "refund payout",
  expiry: "credit expired",
  failed_delivery_credit: "failed-call credit",
};

export const LEDGER_KIND_TONE: Record<PrepaidLedgerEntryKind, string> = {
  refund: "border-sky-800 bg-sky-950/50 text-sky-300",
  expiry: "border-zinc-700 bg-zinc-900 text-zinc-300",
  failed_delivery_credit: "border-violet-800 bg-violet-950/50 text-violet-300",
};
