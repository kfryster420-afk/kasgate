"use client";

import { useEffect, useState } from "react";
import { apiFetch, errorMessage } from "./api-client";
import type { PricingInfo } from "./types";

export type PricingState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; minOutput: bigint; minPrice: bigint };

/** Loads the gateway's min_output_sompi (via /api/pricing) and the derived minimum price. */
export function usePricing(): PricingState {
  const [state, setState] = useState<PricingState>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    apiFetch<PricingInfo>("/api/pricing")
      .then((info) => {
        if (!cancelled) setState({ status: "ready", minOutput: BigInt(info.min_output_sompi), minPrice: BigInt(info.min_price_sompi) });
      })
      .catch((e: unknown) => {
        if (!cancelled) setState({ status: "error", message: errorMessage(e) });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return state;
}
