"""HMAC-SHA256 truncated to 8 bytes.

Mirror of nocturnation-stickc's src/crypto/hmac_sha256.cpp. Used by the
Epic 21 authenticated config channel: `sign_t8` writes the first 8
bytes of HMAC-SHA256 into the trailing MAC region of a CONFIG_WRITE
frame; `verify_t8` compares a received MAC against the recomputed one
in constant time to deny a timing oracle on the stored device secret.

Reference: FIPS 180-4 (SHA-256) and RFC 2104 (HMAC construction).

Why not use CPython's `hmac` module directly? Two reasons:
  - MicroPython's stdlib `hmac` module is NOT part of the Tildagon
    micropython-lib and would need bundling. `hashlib.sha256` is
    available on both so we build HMAC on top of that - 20 lines
    and the construction is covered by RFC 4231 test vectors.
  - Constant-time compare is explicit here so the semantics match
    the C++ implementation on StickC byte-for-byte.
"""

import hashlib


_IPAD = 0x36
_OPAD = 0x5C
_BLOCK = 64            # SHA-256 block size in bytes
_KEY_LEN = 16          # NocturNation Epic 21 secret length
_T8 = 8                # Truncation length (leftmost 8 bytes)


def _derive_pads(key):
    """Return (ipad, opad) each 64 bytes, following RFC 2104 §2.

    The 16-byte NocturNation secret is zero-padded to the SHA-256 block
    size before XORing with the ipad/opad constants.
    """
    if len(key) != _KEY_LEN:
        raise ValueError("key must be 16 bytes")
    ipad = bytearray(_BLOCK)
    opad = bytearray(_BLOCK)
    for i in range(_KEY_LEN):
        ipad[i] = key[i] ^ _IPAD
        opad[i] = key[i] ^ _OPAD
    for i in range(_KEY_LEN, _BLOCK):
        ipad[i] = _IPAD
        opad[i] = _OPAD
    return bytes(ipad), bytes(opad)


def hmac_sha256_full(key, msg):
    """Return the full 32-byte HMAC-SHA256(key, msg).

    `key` must be exactly 16 bytes. `msg` is a bytes-like of any
    length. Return value is 32 bytes.
    """
    ipad, opad = _derive_pads(key)
    inner = hashlib.sha256()
    inner.update(ipad)
    inner.update(msg)
    inner_digest = inner.digest()
    outer = hashlib.sha256()
    outer.update(opad)
    outer.update(inner_digest)
    return outer.digest()


def sign_t8(key, msg):
    """Return the leftmost 8 bytes of HMAC-SHA256(key, msg).

    Caller writes this value into the trailing MAC region of the
    CONFIG_WRITE frame it is building.
    """
    return hmac_sha256_full(key, msg)[:_T8]


def verify_t8(key, msg, mac8):
    """Constant-time compare of the recomputed 8-byte MAC against `mac8`.

    Returns True iff `mac8` matches `sign_t8(key, msg)`. The compare is
    constant-time regardless of where the first differing byte lives,
    so a remote attacker cannot recover expected MAC bytes one at a
    time by measuring reply latency on crafted CONFIG_WRITEs.
    """
    if len(mac8) != _T8:
        return False
    expected = sign_t8(key, msg)
    diff = 0
    for i in range(_T8):
        diff |= expected[i] ^ mac8[i]
    return diff == 0
