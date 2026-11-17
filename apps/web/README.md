# KasGate web (apps/web)

Next.js 15 App Router + TypeScript (strict) + Tailwind. Dark developer UI.

- `npm install && npm run dev` (set `GATEWAY_INTERNAL_URL`, see `.env.example`)
- `npm run build`, `npm run typecheck`, `npm run lint`
- Docker: build args `NEXT_PUBLIC_GATEWAY_URL`, `NEXT_PUBLIC_KASPA_NETWORK`; runtime `GATEWAY_INTERNAL_URL` (default `http://gateway:8000`); port 3000.

Security: the merchant API key lives only in the httpOnly, Secure, SameSite=Lax `kg_session` cookie set by route handlers; all authenticated gateway calls happen server-side under `app/api/*`. Set `KASGATE_COOKIE_SECURE=false` only for plain-http, non-localhost deployments.

Pricing floor: never hardcoded. `GET /api/pricing` reads `min_output_sompi` from the gateway `/v1/directory` manifest; `lib/sompi.ts#minPriceSompi` derives the minimum price, enforced in the form and in `app/api/endpoints*` handlers.
