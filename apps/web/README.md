# KasGate web (apps/web)

Next.js 15 App Router + TypeScript (strict) + Tailwind. Dark developer UI.

- `npm install && npm run dev` (set `GATEWAY_INTERNAL_URL`, see `.env.example`)
- `npm run build`, `npm run typecheck`, `npm run lint`
- Docker: build args `NEXT_PUBLIC_GATEWAY_URL`, `NEXT_PUBLIC_KASPA_NETWORK`; runtime `GATEWAY_INTERNAL_URL` (default `http://gateway:8000`); port 3000. In `infra/docker-compose.yml` `NEXT_PUBLIC_GATEWAY_URL` is derived from the gateway `PUBLIC_BASE_URL` (do not set it separately there), so the wallet-challenge Domain always matches. When running outside compose, set it to the same value as the gateway `PUBLIC_BASE_URL`. Next.js inlines `NEXT_PUBLIC_*` at build time, so set it when running `next build` (changing it only at `node server.js` runtime has no effect).

Auth (CONTRACT v2.2):
- Registration (`/onboarding`) and login (`/login`) are wallet-signature only. `/api/auth/challenge` asks the gateway for a `register` or `login` challenge and refuses it unless `Domain`, `Purpose`, `Address` and `Expires` match; the same check runs again in the browser before any wallet prompt and in the verify/register handlers. `Domain` must equal, as an exact string, the lowercased netloc (`host[:port]`, parsed from the raw URL like Python `urlsplit().netloc`) of `NEXT_PUBLIC_GATEWAY_URL`, which compose derives from the gateway `PUBLIC_BASE_URL`. Signatures are sent to the gateway exactly as the wallet returned them.
- The cookie `kg_session` (httpOnly, SameSite=Lax, Secure unless `KASGATE_COOKIE_SECURE=false`) only ever holds a `kgs_` session token, with Max-Age from the gateway `expires_at`. A `kg_` API key is never stored in a cookie. Route handlers call the gateway with `Authorization: Bearer <kgs_ token>`.
- Register and rotate-key return `api_key` + `session_token` + `expires_at`. The session token goes into the cookie; the `api_key` is returned in that one response body, shown once in a confirm-to-continue panel and kept only in React state.

Pricing (CONTRACT v2.1 + v2.3): never hardcoded. `GET /api/pricing` reads `min_output_sompi` (F) from the gateway `/v1/directory` manifest. `lib/sompi.ts`: `feeSompi(price, F) = max(ceil(price*150/10000), F)`, merchant = price - fee, `minPriceSompi(F) = 3F` (treasury output >= F, merchant output >= 2F so a change output still fits the storage-mass limit). The same helpers drive the live split preview, the form validation and the `app/api/endpoints*` handlers. Below the flat-fee threshold (`floor(F*10000/150)`) the UI shows the flat fee instead of 1.5%.

## Sign-out (CONTRACT v2.4)

`DELETE /api/session` first calls the gateway's `DELETE /v1/auth/session` with the `kgs_` session token as Bearer (revokes that session only), then always clears the `kg_session` cookie and answers 204, even if the gateway returns 401/404/400/5xx or is unreachable. `rotate-key` revokes every session of the merchant; the response sets a fresh `kgs_` cookie.

## Client IP behind a proxy

The gateway rate-limits public calls (auth challenge/verify, prepaid balance, refund challenge/request/cancel/status)
per client IP. Calls from this dashboard reach it from the Next.js server, so every gateway call carries an
`X-Forwarded-For` header that this server sets itself (`lib/server/gateway.ts`), holding exactly one IP or nothing:

- `TRUSTED_PROXY_HOPS` (default `0`): how many reverse proxies you run in front of this server, each of which sets or
  appends `X-Forwarded-For`. With `N >= 1` the client IP is the `N`-th entry from the right of the inbound header (the
  address your outermost proxy saw); entries further left came from the browser and are ignored. With `0`, or when
  the header has fewer than `N` entries or the entry is not an IP, nothing is forwarded. Valid values are 0 to 5;
  anything else counts as 0.
- Behind the RUNBOOK nginx (`proxy_set_header X-Forwarded-For $remote_addr;`) set `TRUSTED_PROXY_HOPS=1`. Never set it
  when browsers can reach this server directly: Next.js keeps a browser-supplied `X-Forwarded-For` as is, so a non-zero
  value without a proxy in front would let anyone choose their rate-limit identity.
- On the gateway, list this server's address (as the gateway sees it) in `TRUSTED_PROXY_IPS`, for example the compose
  network CIDR. The gateway uses `X-Forwarded-For` only from those peers, so the header is inert until then; without
  it every dashboard user shares the web server's bucket. If uvicorn's `FORWARDED_ALLOW_IPS` covers this server too,
  uvicorn applies the same single value before the gateway's own check, with the same result.
- Check the derivation with `npm run test:unit` (Node 22.18 or newer).
