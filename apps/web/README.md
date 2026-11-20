# KasGate web (apps/web)

Next.js 15 App Router + TypeScript (strict) + Tailwind. Dark developer UI.

- `npm install && npm run dev` (set `GATEWAY_INTERNAL_URL`, see `.env.example`)
- `npm run build`, `npm run typecheck`, `npm run lint`
- Docker: build args `NEXT_PUBLIC_GATEWAY_URL`, `NEXT_PUBLIC_KASPA_NETWORK`; runtime `GATEWAY_INTERNAL_URL` (default `http://gateway:8000`); port 3000.

Auth (CONTRACT v2.2):
- Registration (`/onboarding`) and login (`/login`) are wallet-signature only. `/api/auth/challenge` asks the gateway for a `register` or `login` challenge and refuses it unless `Domain`, `Purpose`, `Address` and `Expires` match; the same check runs again in the browser before any wallet prompt and in the verify/register handlers. `Domain` must equal, as an exact string, the lowercased netloc (`host[:port]`, parsed from the raw URL like Python `urlsplit().netloc`) of `NEXT_PUBLIC_GATEWAY_URL`, which therefore has to match the gateway `PUBLIC_BASE_URL`. Signatures are sent to the gateway exactly as the wallet returned them.
- The cookie `kg_session` (httpOnly, SameSite=Lax, Secure unless `KASGATE_COOKIE_SECURE=false`) only ever holds a `kgs_` session token, with Max-Age from the gateway `expires_at`. A `kg_` API key is never stored in a cookie. Route handlers call the gateway with `Authorization: Bearer <kgs_ token>`.
- Register and rotate-key return `api_key` + `session_token` + `expires_at`. The session token goes into the cookie; the `api_key` is returned in that one response body, shown once in a confirm-to-continue panel and kept only in React state.

Pricing (CONTRACT v2.1): never hardcoded. `GET /api/pricing` reads `min_output_sompi` (F) from the gateway `/v1/directory` manifest. `lib/sompi.ts`: `feeSompi(price, F) = max(ceil(price*150/10000), F)`, merchant = price - fee, `minPriceSompi(F) = 2F`. The same helpers drive the live split preview, the form validation and the `app/api/endpoints*` handlers. Below the flat-fee threshold (`floor(F*10000/150)`) the UI shows the flat fee instead of 1.5%.
