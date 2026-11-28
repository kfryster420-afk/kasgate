// Pure-helper tests for the CONTRACT v2.14 buyer credit status (run: npm run test:unit).
import assert from "node:assert/strict";
import { test } from "node:test";
import { isCreditCursor, parseBuyerCreditStatus, parseSompi } from "./credit-status.ts";

const item = { credit_id: "7c9e6679-7425-40de-944b-e07fc1f90ae7", tx_id: "cd".repeat(32), slug: "weather", credit_sompi: "20000000", shortfall_sompi: "1950000000", reason: "attempts_exhausted", created_at: "2026-11-26T16:48:10Z" };
const body = { merchant_id: "b2df6cb9-9ff3-4061-b50a-601e8232a809", credited_sompi: "20000000", credit_balance_sompi: "20000000", refundable_sompi: "40000000", credit_count: 1, credits: [item], next_cursor: null };

test("spec example parses", () => {
  const parsed = parseBuyerCreditStatus(body);
  assert.ok(parsed);
  assert.equal(parsed.credits[0].credit_id, item.credit_id);
});

test("extra or missing keys, bad sompi and bad cursors are refused", () => {
  assert.equal(parseBuyerCreditStatus({ ...body, extra: 1 }), null);
  const missing = { ...body };
  delete missing.next_cursor;
  assert.equal(parseBuyerCreditStatus(missing), null);
  assert.equal(parseBuyerCreditStatus({ ...body, credited_sompi: "01" }), null);
  assert.equal(parseBuyerCreditStatus({ ...body, credited_sompi: 20000000 }), null);
  assert.equal(parseBuyerCreditStatus({ ...body, credit_balance_sompi: "30000000" }), null);
  assert.equal(parseBuyerCreditStatus({ ...body, credit_count: 0 }), null);
  assert.equal(parseBuyerCreditStatus({ ...body, credits: [{ ...item, reason: "other" }] }), null);
  assert.equal(parseBuyerCreditStatus({ ...body, next_cursor: "not a cursor!" }), null);
  assert.equal(parseBuyerCreditStatus({ ...body, credits: [item, item] }, 1), null);
  assert.ok(parseBuyerCreditStatus({ ...body, credit_count: 5, next_cursor: "djF8MjAyNi0xMS0yNg" }));
});

test("sompi strings parse with BigInt, strictly", () => {
  assert.equal(parseSompi("999999999999999999"), 999999999999999999n);
  for (const bad of ["", "-1", "1e3", "1.0", " 1", "0x10", "1000000000000000000"]) assert.equal(parseSompi(bad), null);
  assert.equal(isCreditCursor("a".repeat(1025)), false);
});

test("USD at settlement: exact decimal sompi x rate", async () => {
  const { usdForSompi } = await import("./usd-at-settlement.ts");
  assert.equal(usdForSompi("20000000", "0.1043"), "0.0209");
  assert.equal(usdForSompi("100000000", "0.12"), "0.1200");
  assert.equal(usdForSompi("123456789", "1"), "1.2346");
  assert.equal(usdForSompi("0", "0.5"), "0.0000");
  assert.equal(usdForSompi("20000000", null), null);
  assert.equal(usdForSompi("20000000", "-1"), null);
  assert.equal(usdForSompi("2e7", "0.1"), null);
});
