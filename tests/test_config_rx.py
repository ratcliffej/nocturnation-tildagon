"""Native tests for the CONFIG_WRITE receive dispatcher."""

from nocturnation.config_rx import process_config_write
from nocturnation.crypto.hmac_sha256 import sign_t8
from nocturnation.protocol import property_bag_tlv as pb
from nocturnation.protocol.config_frames import (
    HMAC_LEN,
    config_write_signed_region,
    encode_config_write_unsigned,
)
from nocturnation.protocol.frame import parse_frame
from nocturnation.protocol.numonce_lru import NumonceLru
from nocturnation.settings import Settings


_SECRET = bytes.fromhex("00112233445566778899aabbccddeeff")
_UID    = 0xDEADBEEF
_SOURCE = 0x1234


def _make_settings(uid=_UID, secret=_SECRET):
    s = Settings()
    s.dev_uid = uid
    s.dev_secret = secret.hex() if hasattr(bytes, "hex") else "".join(
        "%02x" % b for b in secret)
    return s


def _sign_frame(buf, secret):
    """Return buf with the trailing HMAC placeholder overwritten by a
    real sign_t8 computed over the signed region.
    """
    buf = bytearray(buf)
    mac = sign_t8(secret, config_write_signed_region(bytes(buf)))
    buf[-HMAC_LEN:] = mac
    return bytes(buf)


def _build_bag(group=5):
    """Build a well-formed property-bag TLV with 'group' set to `group`."""
    return pb.encode([(pb.KEY_GROUP, pb.TYPE_U8, pb.u8(group))])


def _make_frame(uid, numonce, bag, secret, source_id=_SOURCE):
    """Build a complete signed CONFIG_WRITE frame + parse it."""
    buf = encode_config_write_unsigned(
        source_id=source_id, sequence_number=1, hop_count=0,
        target_uid=uid, numonce=numonce, bag_tlv=bag)
    signed = _sign_frame(buf, secret)
    return parse_frame(signed), signed


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_valid_config_write_applies_bag():
    settings = _make_settings()
    lru = NumonceLru()
    frame, buf = _make_frame(_UID, 1, _build_bag(group=7), _SECRET)
    result = process_config_write(frame, buf, settings, lru)
    assert result.ok is True
    assert result.applied_keys == 1
    assert result.numonce == 1
    assert settings.group == 7


def test_valid_config_write_multi_key_applies_all():
    settings = _make_settings()
    lru = NumonceLru()
    bag = pb.encode([
        (pb.KEY_GROUP, pb.TYPE_U8, pb.u8(3)),
        (pb.KEY_FRIENDLY_NAME, pb.TYPE_UTF8, b"X"),
    ])
    frame, buf = _make_frame(_UID, 1, bag, _SECRET)
    result = process_config_write(frame, buf, settings, lru)
    assert result.ok is True
    assert result.applied_keys == 2
    assert settings.group == 3
    assert settings.friendly_name == "X"


# ---------------------------------------------------------------------------
# Silent drops
# ---------------------------------------------------------------------------

def test_wrong_target_uid_silently_dropped():
    settings = _make_settings(uid=_UID)
    lru = NumonceLru()
    frame, buf = _make_frame(0x11111111, 1, _build_bag(), _SECRET)
    result = process_config_write(frame, buf, settings, lru)
    assert result.ok is False
    assert result.reason == "not_for_us"
    # Group untouched.
    assert settings.group == 0 or settings.group in (1, 2, 3)  # defaults


def test_zero_numonce_silently_dropped():
    settings = _make_settings()
    lru = NumonceLru()
    frame, buf = _make_frame(_UID, 0, _build_bag(group=4), _SECRET)
    result = process_config_write(frame, buf, settings, lru)
    assert result.ok is False
    assert result.reason == "replay"


def test_replay_silently_dropped():
    settings = _make_settings()
    lru = NumonceLru()
    frame, buf = _make_frame(_UID, 5, _build_bag(group=4), _SECRET)
    assert process_config_write(frame, buf, settings, lru).ok is True
    # Replay the same numonce.
    frame2, buf2 = _make_frame(_UID, 5, _build_bag(group=99), _SECRET)
    result = process_config_write(frame2, buf2, settings, lru)
    assert result.ok is False
    assert result.reason == "replay"
    # First apply's effect preserved.
    assert settings.group == 4


def test_bad_hmac_silently_dropped():
    settings = _make_settings()
    lru = NumonceLru()
    frame, buf = _make_frame(_UID, 1, _build_bag(group=4), _SECRET)
    # Tamper with the HMAC itself.
    tampered = bytearray(buf)
    tampered[-1] ^= 0x01
    tampered_frame = parse_frame(bytes(tampered))
    result = process_config_write(tampered_frame, bytes(tampered),
                                   settings, lru)
    assert result.ok is False
    assert result.reason == "bad_hmac"
    # Numonce slot WAS updated before HMAC verify (hardening per spec).
    assert lru.highest(_SOURCE) == 1


def test_wrong_key_silently_dropped():
    settings = _make_settings()
    wrong_settings = _make_settings(secret=bytes(16))   # all-zero secret
    lru = NumonceLru()
    # Director signs with the real secret; receiver has a different one.
    frame, buf = _make_frame(_UID, 1, _build_bag(), _SECRET)
    result = process_config_write(frame, buf, wrong_settings, lru)
    assert result.ok is False
    assert result.reason == "bad_hmac"


def test_tampered_bag_silently_dropped():
    settings = _make_settings()
    lru = NumonceLru()
    frame, buf = _make_frame(_UID, 1, _build_bag(group=4), _SECRET)
    # Flip a byte in the bag (inside the signed region).
    tampered = bytearray(buf)
    tampered[-HMAC_LEN - 1] ^= 0x01
    tampered_frame = parse_frame(bytes(tampered))
    result = process_config_write(tampered_frame, bytes(tampered),
                                   settings, lru)
    assert result.ok is False
    assert result.reason == "bad_hmac"
    # Group untouched by the forged frame.
    assert settings.group != 5


# ---------------------------------------------------------------------------
# Hardening: numonce updates BEFORE HMAC verify.
# ---------------------------------------------------------------------------

def test_numonce_consumed_even_on_hmac_fail():
    """A flood of fake numonces with bad HMACs should consume slots -
    the Director's own next frame must then come in with a higher
    numonce. Prevents an attacker from freezing the LRU on a low
    value so the Director's monotonic emits all land in the dead
    zone.
    """
    settings = _make_settings()
    lru = NumonceLru()
    # Attacker sends bogus numonce=1000 with a bad HMAC.
    frame, buf = _make_frame(_UID, 1000, _build_bag(), bytes(16))
    result = process_config_write(frame, buf, settings, lru)
    assert result.ok is False
    assert result.reason == "bad_hmac"
    # Slot is now at 1000. The real Director's next frame at numonce=1
    # is a 'replay' from the LRU's point of view (reject).
    frame2, buf2 = _make_frame(_UID, 1, _build_bag(group=9), _SECRET)
    result2 = process_config_write(frame2, buf2, settings, lru)
    assert result2.ok is False
    assert result2.reason == "replay"
    # Director's monotonic counter should overtake 1000 eventually; a
    # frame at 1001 then applies.
    frame3, buf3 = _make_frame(_UID, 1001, _build_bag(group=9), _SECRET)
    result3 = process_config_write(frame3, buf3, settings, lru)
    assert result3.ok is True
    assert settings.group == 9
