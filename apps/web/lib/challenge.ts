/**
 * Wallet challenge format (schemas.build_auth_challenge):
 *   KasGate\nDomain: {host}\nPurpose: {purpose text}\nAddress: {addr}\nNonce: {32 hex}\nExpires: {iso}
 * The web app checks Domain and Purpose BEFORE the user is asked to sign, so a hostile or misconfigured
 * gateway cannot trick a wallet into signing a message for another site or another action.
 */

export const AUTH_PURPOSE = "Sign in to the KasGate merchant dashboard. This does not authorise any payment.";
export const REGISTER_PURPOSE = "Register this address as a KasGate merchant. This does not authorise any payment.";

export type ChallengePurpose = "login" | "register";

export const PURPOSE_TEXT: Record<ChallengePurpose, string> = {
  login: AUTH_PURPOSE,
  register: REGISTER_PURPOSE,
};

export interface ParsedChallenge {
  domain: string;
  purpose: string;
  address: string;
  nonce: string;
  expires: string;
}

const LABELS = ["Domain", "Purpose", "Address", "Nonce", "Expires"] as const;

export function parseChallenge(message: string): ParsedChallenge | null {
  const lines = message.split("\n");
  if (lines.length !== 6 || lines[0] !== "KasGate") return null;
  const values: string[] = [];
  for (let i = 0; i < LABELS.length; i += 1) {
    const prefix = `${LABELS[i]}: `;
    const line = lines[i + 1] ?? "";
    if (!line.startsWith(prefix)) return null;
    values.push(line.slice(prefix.length));
  }
  const [domain, purpose, address, nonce, expires] = values as [string, string, string, string, string];
  if (!/^[0-9a-f]{32}$/.test(nonce)) return null;
  return { domain, purpose, address, nonce, expires };
}

export interface ChallengeExpectation {
  purpose: ChallengePurpose;
  address: string;
  /** Acceptable Domain values (host or hostname of the gateway PUBLIC_BASE_URL). */
  domains: readonly string[];
  /** The `expires_at` field returned alongside the challenge (when known, it must equal the Expires line). */
  expiresAt?: string;
  now?: number;
}

/** Returns null when the challenge is safe to sign, otherwise a user-facing refusal reason. */
export function checkChallenge(message: string, expected: ChallengeExpectation): string | null {
  const parsed = parseChallenge(message);
  if (!parsed) return "Refusing to sign: the challenge is not in the KasGate format.";
  if (expected.domains.length === 0 || !expected.domains.includes(parsed.domain.toLowerCase())) {
    return `Refusing to sign: the challenge is for domain "${parsed.domain}", not this KasGate gateway (${expected.domains.join(" / ") || "unknown"}).`;
  }
  if (parsed.purpose !== PURPOSE_TEXT[expected.purpose]) {
    return `Refusing to sign: the challenge purpose is not "${expected.purpose}" (it says "${parsed.purpose}").`;
  }
  if (parsed.address.toLowerCase() !== expected.address.toLowerCase()) return "Refusing to sign: the challenge is for a different address.";
  const expires = Date.parse(parsed.expires);
  if (Number.isNaN(expires)) return "Refusing to sign: the challenge expiry is not a valid date.";
  if (expected.expiresAt !== undefined && Date.parse(expected.expiresAt) !== expires) return "Refusing to sign: the challenge expiry is inconsistent.";
  if (expires <= (expected.now ?? Date.now())) return "This challenge has already expired. Request a new one.";
  return null;
}
