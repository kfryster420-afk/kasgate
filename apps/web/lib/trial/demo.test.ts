import assert from "node:assert/strict";
import { test } from "node:test";
import { schnorr } from "@noble/curves/secp256k1.js";
import { hexToBytes } from "@noble/hashes/utils.js";
import { buildTrialMessage, DemoError, demoPath, fetchChallenge, parsePayment402, runTrialCall, type DemoConfig } from "./demo";
import { createEphemeralPayer, p2pkAddress, personalMessageDigest } from "./kaspa-payer";

const PREFIX = "kaspatest:";
const NETWORK = "kaspa:testnet-10";
const MERCHANT = "kaspatest:qpp4pfqlztd9phu9smstamuydskst3ahdah7gdl70jqnfpn5glf36wp0u4dzn";
const TREASURY = "kaspatest:qrrcz3xf5kfm5yzss38kd4460fe3zlx269zxr6q0alrg5eshw3szq59tcs55l";

function body(overrides: Record<string, unknown> = {}): string {
  return JSON.stringify({
    error: "payment_required",
    accepts: [
      { scheme: "kaspa-exact-v1", network: NETWORK, amount: "60000000", payTo: MERCHANT, platformFee: { feeTo: TREASURY, feeAmount: "20000000" }, expiresAt: "2026-11-28T09:54:55Z", ...overrides },
    ],
  });
}

function offerHeader(calls = 1): Headers {
  return new Headers({ "X-KasGate-Trial-Offer": btoa(JSON.stringify({ scheme: "kasgate-trial-v1", freeTrialCalls: calls, challengeUrl: "x", statusUrl: "y" })) });
}

test("address and signature of the throwaway key match the Kaspa SDK vector", () => {
  const secret = hexToBytes("cadee1024cf0069ee6375a200ba6b8b95a93cf02c8067cae202c88985dba5094");
  assert.equal(p2pkAddress(PREFIX, schnorr.getPublicKey(secret)), "kaspatest:qr0x467r76pnnw9k3r8exye60kv5x07mscguxw9jlakd7qr2f8khuzqkg87vp");
  const signature = hexToBytes("dedda7cf20341a75103f3a5e2b22c3747854e9822163e8b6040a377c49feb3af6a7f6f698255d43024a67a8b7c1734278ef61652ab8e03a2667933667a89c0bb");
  assert.ok(schnorr.verify(signature, personalMessageDigest("KasGate\nDomain: x\nNonce: 1"), schnorr.getPublicKey(secret)));
});

test("ephemeral payer signs a message that verifies under its own key", () => {
  const payer = createEphemeralPayer(PREFIX);
  assert.match(payer.address, /^kaspatest:q[a-z0-9]{60,}$/);
  assert.match(payer.sign("hello"), /^[0-9a-f]{128}$/);
});

test("parsePayment402 keeps amounts as exact bigint and splits output 0 / output 1", () => {
  const parsed = parsePayment402(body(), offerHeader(2), { addressPrefix: PREFIX, network: NETWORK });
  assert.equal(parsed.totalSompi, 60000000n);
  assert.equal(parsed.merchantOutput.sompi, 40000000n);
  assert.equal(parsed.treasuryOutput.sompi, 20000000n);
  assert.equal(parsed.merchantOutput.address, MERCHANT);
  assert.equal(parsed.trialCalls, 2);
  const big = parsePayment402(body({ amount: "999999999999999999", platformFee: { feeTo: TREASURY, feeAmount: "14999999999999999" } }), new Headers(), { addressPrefix: PREFIX, network: NETWORK });
  assert.equal(big.merchantOutput.sompi, 985000000000000000n);
  assert.equal(big.trialCalls, null);
});

test("parsePayment402 rejects malformed terms", () => {
  const cfg = { addressPrefix: PREFIX, network: NETWORK };
  assert.throws(() => parsePayment402("not json", new Headers(), cfg), DemoError);
  assert.throws(() => parsePayment402(body({ amount: "6e7" }), new Headers(), cfg), DemoError);
  assert.throws(() => parsePayment402(body({ amount: "10000000" }), new Headers(), cfg), DemoError);
  assert.throws(() => parsePayment402(body({ network: "kaspa:mainnet" }), new Headers(), cfg), DemoError);
  assert.throws(() => parsePayment402(body({ payTo: "kaspa:qabc" }), new Headers(), cfg), DemoError);
});

test("trial message is rebuilt byte for byte, expiry normalised to +00:00", () => {
  const text = buildTrialMessage({ domain: "gw.example", address: "kaspatest:qa", merchantId: "m", slug: "demo-httpbin", method: "GET", path: "/p/demo-httpbin?kg_demo=aa", bodyHash: "-", nonce: "n".repeat(32), expiresAt: "2026-11-28T09:54:55Z" });
  assert.ok(text.endsWith("Expires: 2026-11-28T09:54:55+00:00"));
  assert.ok(text.startsWith("KasGate\nDomain: gw.example\nPurpose: kasgate:v1:trial-call\n"));
  assert.throws(() => buildTrialMessage({ domain: "d", address: "a", merchantId: "m", slug: "s", method: "GET", path: "/", bodyHash: "-", nonce: "n", expiresAt: "tomorrow" }), DemoError);
});

test("demoPath validates slug and run id", () => {
  assert.equal(demoPath("demo-httpbin", "0123456789ab"), "/p/demo-httpbin?kg_demo=0123456789ab");
  assert.throws(() => demoPath("../x", "0123456789ab"), DemoError);
  assert.throws(() => demoPath("demo-httpbin", "zz"), DemoError);
});

function config(handler: (url: string, init: RequestInit) => Response | Promise<Response>): DemoConfig {
  return { gatewayUrl: "https://gw.example", slug: "demo-httpbin", addressPrefix: PREFIX, network: NETWORK, fetchImpl: (async (url: string, init: RequestInit) => handler(url, init)) as typeof fetch };
}

const PATH = "/p/demo-httpbin?kg_demo=0123456789ab";

async function failure(promise: Promise<unknown>): Promise<DemoError> {
  try {
    await promise;
  } catch (error) {
    assert.ok(error instanceof DemoError);
    return error;
  }
  throw new Error("expected a DemoError");
}

test("fetchChallenge: trials disabled when the 402 carries no offer", async () => {
  const seen: bigint[] = [];
  const error = await failure(fetchChallenge({ ...config(() => new Response(body(), { status: 402 })), onChallenge: (c) => seen.push(c.totalSompi) }, PATH));
  assert.equal(error.kind, "trials_disabled");
  assert.deepEqual(seen, [60000000n]);
});

test("fetchChallenge: gateway down on network failure and 503; 429 keeps Retry-After", async () => {
  assert.equal((await failure(fetchChallenge(config(() => Promise.reject(new TypeError("x"))), PATH))).kind, "gateway_down");
  assert.equal((await failure(fetchChallenge(config(() => new Response("{}", { status: 503 })), PATH))).kind, "gateway_down");
  const limited = await failure(fetchChallenge(config(() => new Response("{}", { status: 429, headers: { "Retry-After": "42" } })), PATH));
  assert.equal(limited.kind, "rate_limited");
  assert.equal(limited.retryAfterSeconds, 42);
  assert.equal((await failure(fetchChallenge(config(() => new Response("{}", { status: 429 })), PATH))).retryAfterSeconds, null);
});

test("fetchChallenge: returns the parsed 402 when trials are offered", async () => {
  const seen: string[] = [];
  const parsed = await fetchChallenge({ ...config(() => new Response(body(), { status: 402, headers: offerHeader() })), onChallenge: (c) => seen.push(String(c.feeSompi)) }, PATH);
  assert.equal(parsed.feeSompi, 20000000n);
  assert.deepEqual(seen, ["20000000"]);
});

function trialGateway(call: (headers: Headers) => Response): DemoConfig {
  return config(async (url, init) => {
    if (url.endsWith("/v1/trials/challenge")) {
      const request = JSON.parse(String(init.body)) as { payer_address: string; path: string };
      const nonce = "ab".repeat(16);
      const challenge = {
        nonce,
        payer_address: request.payer_address,
        path: request.path,
        merchant_id: "5a4b7c2e-0000-4000-8000-000000000001",
        expires_at: "2026-11-28T09:54:55Z",
        message: buildTrialMessage({ domain: "gw.example", address: request.payer_address, merchantId: "5a4b7c2e-0000-4000-8000-000000000001", slug: "demo-httpbin", method: "GET", path: request.path, bodyHash: "-", nonce, expiresAt: "2026-11-28T09:54:55Z" }),
      };
      return new Response(JSON.stringify(challenge), { status: 201 });
    }
    return call(new Headers(init.headers));
  });
}

test("runTrialCall signs the rebuilt text, sends X-KasGate-Trial and reads the receipt", async () => {
  const receipt = btoa(JSON.stringify({ status: "used", nonce: "ab".repeat(16) }));
  const outcome = await runTrialCall(
    trialGateway((headers) => {
      const decoded = JSON.parse(atob(headers.get("X-KasGate-Trial") ?? "")) as { nonce: string; signature: string };
      assert.equal(decoded.nonce, "ab".repeat(16));
      assert.match(decoded.signature, /^[0-9a-f]{128}$/);
      return new Response('{"ok":true}', { status: 200, headers: { "X-KasGate-Trial-Receipt": receipt, "X-KasGate-Trial-Limit": "1", "X-KasGate-Trial-Remaining": "0" } });
    }),
    PATH,
  );
  assert.equal(outcome.status, 200);
  assert.equal(outcome.remaining, 0);
  assert.equal(outcome.receiptStatus, "used");
  assert.equal(outcome.body, '{"ok":true}');
});

test("runTrialCall: a refunded trial (upstream down) is reported as upstream_failed", async () => {
  const receipt = btoa(JSON.stringify({ status: "refunded", nonce: "ab".repeat(16) }));
  const error = await failure(runTrialCall(trialGateway(() => new Response("{}", { status: 502, headers: { "X-KasGate-Trial-Receipt": receipt } })), PATH));
  assert.equal(error.kind, "upstream_failed");
});

test("runTrialCall: exhausted trial and daily cap map to trial_cap_reached; a non-trial answer is refused", async () => {
  const exhausted = config(async () => new Response(JSON.stringify({ error: "trial_exhausted" }), { status: 402 }));
  assert.equal((await failure(runTrialCall(exhausted, PATH))).kind, "trial_cap_reached");
  const noReceipt = await failure(runTrialCall(trialGateway(() => new Response("{}", { status: 200 })), PATH));
  assert.equal(noReceipt.kind, "bad_response");
  const limited = await failure(runTrialCall(trialGateway(() => new Response("{}", { status: 429, headers: { "Retry-After": "7" } })), PATH));
  assert.equal(limited.kind, "rate_limited");
  assert.equal(limited.retryAfterSeconds, 7);
});

test("runTrialCall refuses a challenge whose text differs from the request", async () => {
  const tampered = config(async (url, init) => {
    const request = JSON.parse(String(init.body)) as { payer_address: string; path: string };
    return new Response(JSON.stringify({ nonce: "ab".repeat(16), payer_address: request.payer_address, path: request.path, merchant_id: "m", expires_at: "2026-11-28T09:54:55Z", message: "Purpose: something else" }), { status: 201 });
  });
  assert.equal((await failure(runTrialCall(tampered, PATH))).kind, "bad_response");
});
