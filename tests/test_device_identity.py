"""Native tests for the Epic 21 device identity helpers.

CRC32 derivation matches the StickC firmware (same reflected
polynomial, same zlib-compatible output). Known-answer vector is the
same bench MAC used by nocturnation-stickc's test_device_identity.
"""

import binascii
import tempfile

import pytest

from nocturnation import settings as settings_mod
from nocturnation.settings import (
    Settings,
    compute_uid_from_mac,
    ensure_identity,
)


# ---------------------------------------------------------------------------
# compute_uid_from_mac
# ---------------------------------------------------------------------------

# Jason's StickC bench MAC. CRC32 under reflected 0xEDB88320 =
# 0x3693CFA4 (verified via `python3 -c "import zlib;
# print(hex(zlib.crc32(bytes.fromhex('240AC412C158'))))"`).
_BENCH_MAC   = bytes.fromhex("240AC412C158")
_BENCH_UID   = 0x3693CFA4


def test_compute_uid_matches_bench_canary():
    assert compute_uid_from_mac(_BENCH_MAC) == _BENCH_UID


def test_compute_uid_matches_zlib():
    # Any input: our result must equal binascii.crc32 directly.
    for mac in (bytes(6), bytes([0xFF] * 6), b"abcdef"):
        assert compute_uid_from_mac(mac) == binascii.crc32(mac) & 0xFFFFFFFF


def test_compute_uid_requires_6_bytes():
    with pytest.raises(ValueError):
        compute_uid_from_mac(bytes(5))
    with pytest.raises(ValueError):
        compute_uid_from_mac(bytes(7))


def test_compute_uid_deterministic():
    assert compute_uid_from_mac(_BENCH_MAC) == compute_uid_from_mac(_BENCH_MAC)


# ---------------------------------------------------------------------------
# ensure_identity
# ---------------------------------------------------------------------------

def test_ensure_identity_populates_on_first_boot():
    s = Settings()
    assert s.dev_uid == 0
    assert s.dev_secret == ""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    changed = ensure_identity(s, _BENCH_MAC, path=path)
    assert changed is True
    assert s.dev_uid == _BENCH_UID
    # Secret is 32 hex chars = 16 bytes.
    assert len(s.dev_secret) == 32
    int(s.dev_secret, 16)   # must parse as hex


def test_ensure_identity_no_op_when_already_set():
    s = Settings(dev_uid=_BENCH_UID,
                 dev_secret="a" * 32,
                 cfg_numonce=5)
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    assert ensure_identity(s, _BENCH_MAC, path=path) is False
    assert s.dev_uid == _BENCH_UID
    assert s.dev_secret == "a" * 32


def test_ensure_identity_persists_to_disk():
    s = Settings()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    ensure_identity(s, _BENCH_MAC, path=path)
    # Re-load from disk and verify.
    loaded = Settings.load(path=path)
    assert loaded.dev_uid == _BENCH_UID
    assert loaded.dev_secret == s.dev_secret


def test_dev_secret_bytes_round_trips():
    s = Settings(dev_secret="00" + "ff" * 15)
    b = s.dev_secret_bytes()
    assert b[0] == 0x00
    assert b[1:] == b"\xff" * 15
    assert len(b) == 16


def test_dev_secret_bytes_safe_when_unset():
    s = Settings()
    assert s.dev_secret_bytes() == b"\x00" * 16


def test_dev_secret_coercer_rejects_bad_hex():
    s = Settings(dev_secret="nothex!" + "0" * 25)
    # Length 32 but not hex -> coerced to ""
    assert s.dev_secret == ""


def test_dev_secret_coercer_rejects_wrong_length():
    s = Settings(dev_secret="ff")
    assert s.dev_secret == ""


def test_next_cfg_numonce_increments_and_persists():
    s = Settings()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    s.save(path=path)
    assert s.next_cfg_numonce(path=path) == 1
    assert s.next_cfg_numonce(path=path) == 2
    loaded = Settings.load(path=path)
    assert loaded.cfg_numonce == 2


def test_next_cfg_numonce_never_returns_zero():
    s = Settings()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    s.save(path=path)
    # First call returns 1 (not 0 - reserved sentinel).
    assert s.next_cfg_numonce(path=path) == 1
