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
    `# 1. Call the endpoint with no payment: you get HTTP 402 and a PAYMENT-REQUIRED header (Base64 JSON).`,
    `curl -i${flag} ${proxyUrl}`,
    ``,
    `# 2. Decode the payment terms (amount is the TOTAL in sompi; platformFee is the 1.5% output).`,
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
    `# PAYMENT_SIGNATURE = Base64 of {"txId":"…","rawTx":"…","payerAddress":"…"} for your signed Kaspa transaction.`,
    `# outputs[0] -> payTo (exact merchant amount), outputs[1] -> platformFee.feeTo (exact fee).`,
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


def signature_header(tx_id: str, raw_tx: str, payer_address: str) -> str:
    payload = {"txId": tx_id, "rawTx": raw_tx, "payerAddress": payer_address}
    return base64.b64encode(json.dumps(payload).encode()).decode()


def get_terms() -> dict:
    """Unpaid call: returns the decoded PAYMENT-REQUIRED terms (HTTP 402)."""
    response = requests.request(METHOD, URL, timeout=30)
    if response.status_code != 402:
        raise RuntimeError(f"expected HTTP 402, got {response.status_code}")
    return decode_header(response.headers["PAYMENT-REQUIRED"])


def call_with_payment(tx_id: str, raw_tx: str, payer_address: str) -> requests.Response:
    """Paid call: tx_id / raw_tx / payer_address come from the signed Kaspa transaction.

    outputs[0] pays terms["payTo"] the merchant amount; outputs[1] pays platformFee.feeTo platformFee.feeAmount.
    """
    headers = {"PAYMENT-SIGNATURE": signature_header(tx_id, raw_tx, payer_address)}
    response = requests.request(METHOD, URL, headers=headers, timeout=60)
    if "PAYMENT-RESPONSE" in response.headers:
        receipt = decode_header(response.headers["PAYMENT-RESPONSE"])
        print("settled", receipt["txId"], "USD at settlement:", receipt["usdValueAtSettlement"])
    return response


if __name__ == "__main__":
    terms = get_terms()
    print("total sompi:", terms["amount"], "| payTo:", terms["payTo"], "| fee:", terms["platformFee"])
`;
}

export function curlRegisterMerchant(origin: string = GATEWAY_PUBLIC_URL): string {
  return `curl -s -X POST ${origin}/v1/merchants \\
  -H "Content-Type: application/json" \\
  -d '{
    "kaspa_address": "${ADDRESS_PREFIX}q…your-payout-address",
    "display_name": "Acme Data API",
    "email": "you@example.com"
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
