# KasGate web (apps/web)

Next.js 15 App Router + TypeScript (strict) + Tailwind. Dark developer UI.

- `npm install && npm run dev` (set `GATEWAY_INTERNAL_URL`, see `.env.example`)
- `npm run build`, `npm run typecheck`, `npm run lint`
- Docker: build args `NEXT_PUBLIC_GATEWAY_URL`, `NEXT_PUBLIC_KASPA_NETWORK`; runtime `GATEWAY_INTERNAL_URL` (default `http://gateway:8000`); port 3000.

Auth (CONTRACT v2):
- Registration (`/onboarding`) and login (`/login`) are wallet-signature flows. The browser asks `/api/auth/challenge` (purpose `register` or `login`); the challenge is refused unless its `Domain` (host of the gateway `PUBLIC_BASE_URL`, taken from `NEXT_PUBLIC_GATEWAY_URL`), `Purpose`, `Address` and `Expires` match, both in the route handler and again in the browser before any wallet prompt. KasWare (`signMessage(challenge, {type:"schnorr"})`) or manual signature entry; signatures are sent to the gateway exactly as the wallet returned them.
- Login: gateway `SessionIssued` -> the `kgs_` session token goes only into the httpOnly cookie, `maxAge` derived from `expires_at`. The long-lived `api_key` is not touched.
- Registration returns the automation `api_key` once (shown once in a confirm-to-continue panel, kept only in React state). Rotating the key from the dashboard also shows the new key once.
- Cookie: `kg_session`, httpOnly, SameSite=Lax, Secure unless `KASGATE_COOKIE_SECURE=false` (default true).

Pricing (CONTRACT v2.1): never hardcoded. `GET /api/pricing` reads `min_output_sompi` (F) from the gateway `/v1/directory` manifest. `lib/sompi.ts`: `feeSompi(price, F) = max(ceil(price*150/10000), F)`, merchant = price - fee, `minPriceSompi(F) = 2F`. The same helpers drive the live split preview, the form validation and the `app/api/endpoints*` handlers. Below the flat-fee threshold (`floor(F*10000/150)`) the UI shows the flat fee instead of 1.5%.
