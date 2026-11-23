import { GATEWAY_PUBLIC_URL, ADDRESS_PREFIX } from "./config";
import type { HttpMethod } from "./types";

export function gatewayOriginOf(proxyUrl: string): string {
  try {
    return new URL(proxyUrl).origin;
  } catch {
    return GATEWAY_PUBLIC_URL;
  }
}

export function slugOf(proxyUrl: string): string {
  const parts = proxyUrl.replace(/\/+$/, "").split("/");
  return parts[parts.length - 1] ?? "";
}

export function curlDiscover(proxyUrl: string, method: HttpMethod | string = "GET"): string {
  const flag = method === "GET" ? "" : ` -X ${method}`;
  return [
    `# 1. Call the endpoint with no payment: HTTP 402 plus a PAYMENT-REQUIRED header (Base64 JSON).`,
    `curl -i${flag} ${proxyUrl}`,
    ``,
    `# 2. Decode the terms: an x402 v2 envelope whose accepts[] offers kaspa-x402 "exact" (no platform fee) and`,
    `#    KasGate "kaspa-exact-v1"; the v1 terms are also at the top level: amount = TOTAL sompi,`,
    `#    platformFee = treasury output, nonce + expiresAt bind your payment.`,
    `curl -s -D - -o /dev/null${flag} ${proxyUrl} \\`,
    `  | awk 'tolower($1)=="payment-required:" {print $2}' | tr -d '\\r' | base64 -d | python3 -m json.tool`,
    ``,
    `# 3. Same terms as plain JSON, for agents and SDKs:`,
    `curl -s ${gatewayOriginOf(proxyUrl)}/v1/quote/${slugOf(proxyUrl)}`,
  ].join("\n");
}

export function curlPaid(proxyUrl: string, method: HttpMethod | string = "GET"): string {
  const flag = method === "GET" ? "" : ` -X ${method}`;
  return [
    `# PAYMENT_SIGNATURE = Base64 of {"txId":"…","rawTx":"…","payerAddress":"…","nonce":"<nonce from the 402>"}.`,
    `# The signed transaction payload must be the ASCII bytes "kasgate:v1:<nonce>";`,
    `# outputs[0] -> payTo (exact merchant amount), outputs[1] -> platformFee.feeTo (exact fee).`,
    `# If the nonce expired you get 402 "payment_expired" with fresh terms: re-sign against the new nonce.`,
    `curl -i${flag} ${proxyUrl} \\`,
    `  -H "PAYMENT-SIGNATURE: $PAYMENT_SIGNATURE"`,
  ].join("\n");
}

export function pythonSnippet(proxyUrl: string, method: HttpMethod | string = "GET"): string {
  return `import base64
import json

import requests

URL = "${proxyUrl}"
METHOD = "${method}"


def decode_header(value: str) -> dict:
    return json.loads(base64.b64decode(value))


def signature_header(tx_id: str, raw_tx: str, payer_address: str, nonce: str) -> str:
    payload = {"txId": tx_id, "rawTx": raw_tx, "payerAddress": payer_address, "nonce": nonce}
    return base64.b64encode(json.dumps(payload).encode()).decode()


def get_terms() -> dict:
    """Unpaid call: returns the decoded PAYMENT-REQUIRED terms (HTTP 402), including nonce and expiresAt."""
    response = requests.request(METHOD, URL, timeout=30)
    if response.status_code != 402:
        raise RuntimeError(f"expected HTTP 402, got {response.status_code}")
    return decode_header(response.headers["PAYMENT-REQUIRED"])


def payment_payload(terms: dict) -> bytes:
    """The signed transaction's payload must be exactly this, binding the payment to this challenge."""
    return b"kasgate:v1:" + terms["nonce"].encode("ascii")


def call_with_payment(terms: dict, tx_id: str, raw_tx: str, payer_address: str) -> requests.Response:
    """Paid call. outputs[0] pays terms["payTo"] the merchant amount; outputs[1] pays platformFee.feeTo platformFee.feeAmount."""
    headers = {"PAYMENT-SIGNATURE": signature_header(tx_id, raw_tx, payer_address, terms["nonce"])}
    response = requests.request(METHOD, URL, headers=headers, timeout=60)
    if response.status_code == 402 and response.json().get("error") == "payment_expired":
        raise RuntimeError("nonce expired: call get_terms() again and re-sign")
    if "PAYMENT-RESPONSE" in response.headers:
        receipt = decode_header(response.headers["PAYMENT-RESPONSE"])
        print("settled", receipt["txId"], "USD at settlement:", receipt["usdValueAtSettlement"])
    return response


if __name__ == "__main__":
    terms = get_terms()
    print("total sompi:", terms["amount"], "| payTo:", terms["payTo"], "| fee:", terms["platformFee"], "| expires:", terms["expiresAt"])
    print("transaction payload:", payment_payload(terms))
`;
}

export function curlRegisterMerchant(origin: string = GATEWAY_PUBLIC_URL): string {
  return `# 1. Ask for a registration challenge (purpose "register"). Check its Domain and Purpose lines before signing.
curl -s -X POST ${origin}/v1/auth/challenge \\
  -H "Content-Type: application/json" \\
  -d '{"kaspa_address": "${ADDRESS_PREFIX}q…your-payout-address", "purpose": "register"}'
# -> {"kaspa_address": "...", "challenge": "KasGate\\nDomain: ...\\nPurpose: ...", "expires_at": "..."}

# 2. Sign the exact challenge text with your wallet (Schnorr, e.g. KasWare signMessage(challenge, {type: "schnorr"})).
# 3. Register with the challenge, your public key and the signature (sent as the wallet returned it).
curl -s -X POST ${origin}/v1/merchants \\
  -H "Content-Type: application/json" \\
  -d '{
    "kaspa_address": "${ADDRESS_PREFIX}q…your-payout-address",
    "display_name": "Acme Data API",
    "email": "you@example.com",
    "challenge": "<challenge from step 1>",
    "public_key": "<public key hex>",
    "signature": "<signature>"
  }'
# -> {"merchant": {...}, "api_key": "kg_…"}   (shown once: store it securely)`;
}

export function curlCreateEndpoint(origin: string = GATEWAY_PUBLIC_URL): string {
  return `curl -s -X POST ${origin}/v1/endpoints \\
  -H "Authorization: Bearer $KASGATE_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "name": "Weather API",
    "upstream_url": "https://api.example.com/v1/weather",
    "upstream_headers": {"X-Api-Key": "your-upstream-secret"},
    "price_sompi": 2000000000,
    "allowed_methods": ["GET"],
    "category": "data",
    "is_public_listed": true
  }'
# -> {"slug": "weather-api-1a2b3c", "proxy_url": "${origin}/p/weather-api-1a2b3c", ...}`;
}

/** Copyable curl for an endpoint created by the OpenAPI import; path parameters are shell variables. */
export function curlImported(proxyUrl: string, method: HttpMethod | string, url: string, variables: readonly string[]): string {
  const flag = method === "GET" ? "" : ` -X ${method}`;
  const lines: string[] = [];
  if (variables.length > 0) {
    lines.push(`# Path parameters for this operation:`);
    for (const variable of variables) lines.push(`${variable}="..."`);
    lines.push(``);
  }
  lines.push(
    `# 1. Unpaid call: HTTP 402 plus a PAYMENT-REQUIRED header (Base64 JSON) with the nonce-bound terms.`,
    `curl -i${flag} "${url}"`,
    ``,
    `# 2. Same terms as plain JSON, for agents and SDKs:`,
    `curl -s ${gatewayOriginOf(proxyUrl)}/v1/quote/${slugOf(proxyUrl)}`,
    ``,
    `# 3. Paid retry with the signed transaction:`,
    `curl -i${flag} "${url}" \\`,
    `  -H "PAYMENT-SIGNATURE: $PAYMENT_SIGNATURE"`,
  );
  return lines.join("\n");
}
