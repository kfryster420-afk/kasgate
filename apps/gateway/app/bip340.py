"""BIP-340 Schnorr signature verification over secp256k1 (verification only, no secret material).

kaspa-x402 `kaspa-exact-v2` signs two 32-byte digests with BIP-340 Schnorr: the per-input Kaspa sighash and the
off-chain request-authorization digest. The `kaspa` Python SDK can sign a raw digest (`sign_script_hash`) but only
verifies `sign_message` signatures, so the raw-digest check lives here. This is the reference algorithm from
https://github.com/bitcoin/bips/blob/master/bip-0340.mediawiki ("Verification"), operating on public data only, so
constant-time arithmetic is not required.
"""

import hashlib
from typing import Final

_P: Final = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N: Final = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G: Final = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)

Point = tuple[int, int] | None


def _tagged_hash(tag: str, message: bytes) -> bytes:
    tag_hash = hashlib.sha256(tag.encode("utf-8")).digest()
    return hashlib.sha256(tag_hash + tag_hash + message).digest()


def _point_add(p1: Point, p2: Point) -> Point:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and y1 != y2:
        return None
    numerator, denominator = (3 * x1 * x1, 2 * y1) if p1 == p2 else (y2 - y1, x2 - x1)
    slope = (numerator * pow(denominator, _P - 2, _P)) % _P
    x3 = (slope * slope - x1 - x2) % _P
    return x3, (slope * (x1 - x3) - y1) % _P


def _point_mul(point: Point, scalar: int) -> Point:
    result: Point = None
    addend = point
    while scalar:
        if scalar & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        scalar >>= 1
    return result


def _lift_x(x: int) -> Point:
    if x >= _P:
        return None
    y_sq = (pow(x, 3, _P) + 7) % _P
    y = pow(y_sq, (_P + 1) // 4, _P)
    if pow(y, 2, _P) != y_sq:
        return None
    return x, y if y % 2 == 0 else _P - y


def verify_schnorr(message: bytes, public_key: bytes, signature: bytes) -> bool:
    """True when `signature` (64 bytes) is a valid BIP-340 signature of `message` by x-only `public_key` (32 bytes)."""
    if len(public_key) != 32 or len(signature) != 64:
        return False
    point = _lift_x(int.from_bytes(public_key, "big"))
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    if point is None or r >= _P or s >= _N:
        return False
    challenge = int.from_bytes(_tagged_hash("BIP0340/challenge", signature[:32] + public_key + message), "big") % _N
    candidate = _point_add(_point_mul(_G, s), _point_mul(point, _N - challenge))
    return candidate is not None and candidate[1] % 2 == 0 and candidate[0] == r


def verify_schnorr_hex(digest_hex: str, public_key_hex: str, signature_hex: str) -> bool:
    """Hex convenience wrapper; malformed hex is simply an invalid signature."""
    try:
        return verify_schnorr(bytes.fromhex(digest_hex), bytes.fromhex(public_key_hex), bytes.fromhex(signature_hex))
    except ValueError:
        return False
