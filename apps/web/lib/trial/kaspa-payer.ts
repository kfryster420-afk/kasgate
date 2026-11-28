import { schnorr } from "@noble/curves/secp256k1.js";
import { blake2b } from "@noble/hashes/blake2.js";
import { bytesToHex, utf8ToBytes } from "@noble/hashes/utils.js";

/**
 * A throwaway Kaspa identity for the no-wallet demo: a fresh BIP-340 key made in the visitor's browser, kept only in
 * this closure (never stored, never sent anywhere). It owns no coins; the gateway only ever sees its public address and
 * a Schnorr signature over the trial-call text, so the signature can never move funds.
 */

const CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l";
const PERSONAL_MESSAGE_KEY = utf8ToBytes("PersonalMessageSigningHash");
const ADDRESS_VERSION_PUBKEY = 0;
const GENERATORS: readonly bigint[] = [0x98f2bc8e61n, 0x79b76d99e2n, 0xf33e5fb3c4n, 0xae2eabe2a8n, 0x1e4f43e470n];

function polymod(values: readonly number[]): bigint {
  let c = 1n;
  for (const d of values) {
    const c0 = Number(c >> 35n);
    c = ((c & 0x07ffffffffn) << 5n) ^ BigInt(d);
    GENERATORS.forEach((g, bit) => {
      if ((c0 >> bit) & 1) c ^= g;
    });
  }
  return c ^ 1n;
}

function toFiveBitGroups(bytes: Uint8Array): number[] {
  const out: number[] = [];
  let buffer = 0;
  let bits = 0;
  for (const byte of bytes) {
    buffer = (buffer << 8) | byte;
    bits += 8;
    while (bits >= 5) {
      out.push((buffer >> (bits - 5)) & 31);
      bits -= 5;
    }
    buffer &= (1 << bits) - 1;
  }
  if (bits > 0) out.push((buffer << (5 - bits)) & 31);
  return out;
}

/** P2PK Schnorr address (`<prefix>q...`) of an x-only public key; `prefix` is "kaspatest:" or "kaspa:". */
export function p2pkAddress(prefix: string, xOnlyPublicKey: Uint8Array): string {
  if (!/^[a-z]+:$/.test(prefix)) throw new Error("address prefix must look like 'kaspatest:'");
  if (xOnlyPublicKey.length !== 32) throw new Error("x-only public key must be 32 bytes");
  const payload = toFiveBitGroups(Uint8Array.of(ADDRESS_VERSION_PUBKEY, ...xOnlyPublicKey));
  const prefixValues = [...prefix.slice(0, -1)].map((ch) => ch.charCodeAt(0) & 31);
  let checksum = polymod([...prefixValues, 0, ...payload, 0, 0, 0, 0, 0, 0, 0, 0]);
  const checksumChars: string[] = [];
  for (let i = 0; i < 8; i += 1) {
    checksumChars.unshift(CHARSET.charAt(Number(checksum & 31n)));
    checksum >>= 5n;
  }
  return `${prefix}${payload.map((v) => CHARSET.charAt(v)).join("")}${checksumChars.join("")}`;
}

/** Kaspa personal-message digest (what `signMessage` and the gateway's `verify_message` hash): keyed BLAKE2b-256. */
export function personalMessageDigest(message: string): Uint8Array {
  return blake2b(utf8ToBytes(message), { dkLen: 32, key: PERSONAL_MESSAGE_KEY });
}

export interface EphemeralPayer {
  address: string;
  /** 128-hex BIP-340 signature of the Kaspa personal-message digest, as `kaspa.sign_message` returns. */
  sign(message: string): string;
}

export function createEphemeralPayer(prefix: string): EphemeralPayer {
  const secretKey = schnorr.utils.randomSecretKey();
  const publicKey = schnorr.getPublicKey(secretKey);
  return {
    address: p2pkAddress(prefix, publicKey),
    sign: (message) => bytesToHex(schnorr.sign(personalMessageDigest(message), secretKey)),
  };
}
