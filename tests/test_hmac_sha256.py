"""Native tests for the Epic 21 HMAC-SHA256 module.

Pins the Python implementation against the same known-answer vector
the StickC firmware tests use, so the two sides produce byte-
identical MACs for the same (key, msg) pair. Divergence between the
two would mean every CONFIG_WRITE fails verification on the Tildagon.

Reference vector (also in nocturnation-stickc test_hmac_sha256):

    key = bytes(range(16))
    msg = b"Hi There"
    python3 -c 'import hmac;
      k=bytes(range(16));
      print(hmac.new(k, b"Hi There", "sha256").hexdigest())'
    -> 3afd7fe7437c3ecadb2d089e7a65c8f994a3bff46f9a8eb3d3d453b9fa5acc34

The first 8 bytes (3afd7fe7437c3eca) are the Epic 21 wire-level MAC.
"""

import hmac
import pytest

from nocturnation.crypto.hmac_sha256 import (
    hmac_sha256_full,
    sign_t8,
    verify_t8,
)


_REF_KEY = bytes(range(16))
_REF_MSG = b"Hi There"
_REF_T8  = bytes.fromhex("3afd7fe7437c3eca")


def test_full_hmac_matches_known_vector():
    full = hmac_sha256_full(_REF_KEY, _REF_MSG)
    assert full == bytes.fromhex(
        "3afd7fe7437c3ecadb2d089e7a65c8f994a3bff46f9a8eb3d3d453b9fa5acc34"
    )


def test_sign_t8_matches_known_vector():
    assert sign_t8(_REF_KEY, _REF_MSG) == _REF_T8


def test_verify_t8_accepts_matching_mac():
    assert verify_t8(_REF_KEY, _REF_MSG, _REF_T8) is True


def test_verify_t8_rejects_wrong_mac():
    bad = bytearray(_REF_T8)
    bad[3] ^= 0x01
    assert verify_t8(_REF_KEY, _REF_MSG, bytes(bad)) is False


def test_verify_t8_rejects_wrong_message():
    assert verify_t8(_REF_KEY, b"Hi There!", _REF_T8) is False


def test_verify_t8_rejects_wrong_key():
    wrong_key = bytearray(_REF_KEY)
    wrong_key[0] ^= 0xFF
    assert verify_t8(bytes(wrong_key), _REF_MSG, _REF_T8) is False


def test_verify_t8_rejects_truncated_mac():
    assert verify_t8(_REF_KEY, _REF_MSG, _REF_T8[:7]) is False


def test_key_must_be_16_bytes():
    with pytest.raises(ValueError):
        hmac_sha256_full(bytes(15), _REF_MSG)
    with pytest.raises(ValueError):
        hmac_sha256_full(bytes(17), _REF_MSG)


def test_cross_check_against_cpython_hmac_random_inputs():
    # Not strictly needed on top of the known-answer vector, but
    # catches drift from CPython's hmac module across a spread of
    # input lengths - the kind of regression a careless loop or
    # padding bug would introduce silently.
    import os
    for msg_len in (0, 1, 55, 56, 63, 64, 65, 200, 220):
        key = os.urandom(16)
        msg = os.urandom(msg_len)
        expected = hmac.new(key, msg, "sha256").digest()[:8]
        assert sign_t8(key, msg) == expected
