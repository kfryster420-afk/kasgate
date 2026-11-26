// Pure-helper tests for the CONTRACT v2.12.1 signed balance read (run: npm run test:unit).
import assert from "node:assert/strict";
import { test } from "node:test";
import { balanceReadMessage, checkBalanceTimestamp, normaliseBalanceSignature } from "./balance-read.ts";

test("message matches schemas.build_prepaid_balance_read_message", () => {
  assert.equal(
    balanceReadMessage({ domain: "localhost:8300", address: "kaspatest:qabc", slug: "weather", timestamp: 1764000000 }),
    "KasGate\nDomain: localhost:8300\nPurpose: kasgate:v1:prepaid-balance-read\nAddress: kaspatest:qabc\nSlug: weather\nTimestamp: 1764000000",
  );
});

test("timestamp: 1-12 ASCII digits only", () => {
  assert.equal(checkBalanceTimestamp("1764000000"), "1764000000");
  for (const bad of ["", "-1", "1.5", "1234567890123", "١٢٣", " 17"]) assert.equal(checkBalanceTimestamp(bad), null);
});

test("signature: 128 hex, lowercased; anything else refused", () => {
  assert.equal(normaliseBalanceSignature("AB".repeat(64)), "ab".repeat(64));
  for (const bad of ["ab".repeat(63), "zz".repeat(64), "ab".repeat(65), "q".repeat(128)]) assert.equal(normaliseBalanceSignature(bad), null);
});
