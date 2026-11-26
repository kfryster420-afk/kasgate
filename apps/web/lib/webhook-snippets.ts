/** Receiver-side signature checks shown in the dashboard (CONTRACT v2.12). Mirrors kasgate_client.verify_webhook_signature. */

export const WEBHOOK_VERIFY_NODE = `import crypto from "node:crypto";

// rawBody: the exact request bytes (Buffer), read BEFORE any JSON parsing.
export function verifyKasGate(secret, signatureHeader, rawBody, toleranceSeconds = 300) {
  let t = NaN;
  const candidates = [];
  for (const part of signatureHeader.split(",")) {
    const [key, value = ""] = part.trim().split("=", 2);
    if (key === "t" && /^\\d+$/.test(value)) t = Number(value);
    else if (key === "v1" && value) candidates.push(value);
  }
  if (!Number.isInteger(t) || candidates.length === 0) return false;
  if (Math.abs(Date.now() / 1000 - t) > toleranceSeconds) return false; // replay protection
  const expected = crypto.createHmac("sha256", secret).update(\`\${t}.\`).update(rawBody).digest("hex");
  return candidates.some(
    (c) => c.length === expected.length && crypto.timingSafeEqual(Buffer.from(c), Buffer.from(expected)),
  );
}
// Then dedupe on the X-KasGate-Event-Id header and answer 2xx quickly.`;

export const WEBHOOK_VERIFY_PYTHON = `from kasgate_client import verify_webhook_signature

# raw_body: the exact request bytes, read BEFORE any JSON parsing.
if not verify_webhook_signature(WEBHOOK_SECRET, request.headers["X-KasGate-Signature"], raw_body):
    return Response(status_code=400)
# Dedupe on request.headers["X-KasGate-Event-Id"], then answer 2xx quickly.`;
