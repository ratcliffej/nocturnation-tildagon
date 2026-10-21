"""Native tests for Epic 21 frame codecs (UID_ANNOUNCE / CONFIG_WRITE
/ CONFIG_ACK). Round-trip encode/decode, boundary cases, HMAC
signed-region integration with crypto.hmac_sha256.
"""

import pytest

from nocturnation.crypto.hmac_sha256 import sign_t8, verify_t8
from nocturnation.protocol._generated import MessageType, PROTOCOL_VERSION
from nocturnation.protocol.constants import HEADER_SIZE, MAGIC_0, MAGIC_1
from nocturnation.protocol.config_frames import (
    AckStatus,
    CONFIG_ACK_LEN,
    CONFIG_WRITE_FIXED_PREFIX,
    CONFIG_WRITE_MAX_BAG_LEN,
    HMAC_LEN,
    Host,
    Role,
    UID_ANNOUNCE_MIN_LEN,
    UID_LEN,
    ConfigAckPayload,
    ConfigWritePayload,
    UidAnnouncePayload,
    config_write_signed_region,
    decode_config_ack,
    decode_config_write,
    decode_uid_announce,
    encode_config_ack,
    encode_config_write_unsigned,
    encode_uid_announce,
)


# ---------------------------------------------------------------------------
# UID_ANNOUNCE
# ---------------------------------------------------------------------------

def test_uid_announce_round_trip_with_name():
    p = UidAnnouncePayload(
        uid=0xDEADBEEF,
        secret=bytes(range(16)),
        role=Role.LUME,
        host=Host.TILDAGON,
        friendly_name=b"Alice",
    )
    frame = encode_uid_announce(source_id=0x1234, sequence_number=1,
                                hop_count=0, payload=p)
    # Envelope checks.
    assert frame[0] == MAGIC_0
    assert frame[1] == MAGIC_1
    assert frame[2] == PROTOCOL_VERSION
    assert frame[7] == MessageType.UID_ANNOUNCE
    # Decode the payload.
    out = decode_uid_announce(frame[HEADER_SIZE:])
    assert out.uid == 0xDEADBEEF
    assert out.secret == bytes(range(16))
    assert out.role == Role.LUME
    assert out.host == Host.TILDAGON
    assert out.friendly_name == b"Alice"


def test_uid_announce_round_trip_empty_name():
    p = UidAnnouncePayload(0xAABBCCDD, b"\x00" * 16,
                           Role.DIRECTOR, Host.STICKC, b"")
    frame = encode_uid_announce(0, 0, 0, p)
    assert len(frame) == HEADER_SIZE + UID_ANNOUNCE_MIN_LEN
    out = decode_uid_announce(frame[HEADER_SIZE:])
    assert out.friendly_name == b""


def test_uid_announce_rejects_oversized_name():
    with pytest.raises(ValueError):
        encode_uid_announce(0, 0, 0, UidAnnouncePayload(
            1, bytes(16), Role.LUME, Host.TILDAGON, b"x" * 21))


def test_uid_announce_rejects_bad_secret_length():
    with pytest.raises(ValueError):
        encode_uid_announce(0, 0, 0, UidAnnouncePayload(
            1, bytes(15), Role.LUME, Host.TILDAGON, b""))


def test_uid_announce_decode_rejects_malformed():
    # name_len claims 5 but payload only has 3 trailing bytes.
    buf = (bytes(UID_LEN)        # uid
           + bytes(16)           # secret
           + bytes([0, 0, 5])    # role, host, name_len = 5
           + b"abc")             # only 3 bytes
    with pytest.raises(ValueError):
        decode_uid_announce(buf)


def test_uid_announce_str_name_coerced_to_bytes():
    p = UidAnnouncePayload(1, bytes(16), Role.LUME, Host.TILDAGON, "Bob")
    assert p.friendly_name == b"Bob"


# ---------------------------------------------------------------------------
# CONFIG_WRITE (encode/decode + signed region)
# ---------------------------------------------------------------------------

def test_config_write_round_trip_empty_bag():
    frame = encode_config_write_unsigned(
        source_id=0x40, sequence_number=1, hop_count=0,
        target_uid=0x11223344, numonce=1, bag_tlv=b"")
    out = decode_config_write(frame[HEADER_SIZE:])
    assert out.target_uid == 0x11223344
    assert out.numonce == 1
    assert out.bag_tlv == b""
    # HMAC region is still the zero placeholder.
    assert out.hmac == b"\x00" * HMAC_LEN


def test_config_write_round_trip_with_bag():
    bag = bytes([0x05, ord("g"), ord("r"), ord("o"), ord("u"), ord("p"),
                 0x01, 0x01, 0x03])   # 'group' u8 = 3 (TLV example)
    frame = encode_config_write_unsigned(0x50, 7, 0, 0xCAFEBABE, 42, bag)
    out = decode_config_write(frame[HEADER_SIZE:])
    assert out.target_uid == 0xCAFEBABE
    assert out.numonce == 42
    assert out.bag_tlv == bag


def test_config_write_bag_too_big_rejected():
    with pytest.raises(ValueError):
        encode_config_write_unsigned(
            0, 1, 0, 0, 1, bytes(CONFIG_WRITE_MAX_BAG_LEN + 1))


def test_config_write_decode_rejects_bag_len_overflow():
    # Craft a payload where bag_len claims more bytes than are present.
    bad = (bytes(CONFIG_WRITE_FIXED_PREFIX - 1)
           + bytes([200])          # bag_len = 200
           + bytes(10)             # only 10 bytes of 'bag'
           + bytes(HMAC_LEN))
    with pytest.raises(ValueError):
        decode_config_write(bad)


def test_config_write_sign_verify_round_trip():
    secret = bytes(range(16))
    bag = b"\x05group\x01\x01\x02"
    frame = bytearray(encode_config_write_unsigned(
        source_id=0x40, sequence_number=1, hop_count=0,
        target_uid=0xDEADBEEF, numonce=5, bag_tlv=bag))
    # Sign the covered region, overwrite the trailing MAC placeholder.
    signed_region = config_write_signed_region(bytes(frame))
    mac = sign_t8(secret, signed_region)
    frame[-HMAC_LEN:] = mac
    # Verifier recomputes over the same region.
    assert verify_t8(secret, config_write_signed_region(bytes(frame)),
                     bytes(frame[-HMAC_LEN:])) is True


def test_config_write_sign_rejects_tampered_bag():
    secret = bytes(range(16))
    frame = bytearray(encode_config_write_unsigned(
        0x40, 1, 0, 0xDEADBEEF, 5, b"\x05group\x01\x01\x02"))
    mac = sign_t8(secret, config_write_signed_region(bytes(frame)))
    frame[-HMAC_LEN:] = mac
    # Flip one byte in the bag.
    frame[HEADER_SIZE + CONFIG_WRITE_FIXED_PREFIX + 7] ^= 0x01
    assert verify_t8(secret, config_write_signed_region(bytes(frame)),
                     bytes(frame[-HMAC_LEN:])) is False


# ---------------------------------------------------------------------------
# CONFIG_ACK
# ---------------------------------------------------------------------------

def test_config_ack_round_trip():
    p = ConfigAckPayload(responder_uid=0xAABBCCDD, responder_numonce=42,
                         status=AckStatus.OK, applied_keys=3)
    frame = encode_config_ack(0x40, 1, 0, p)
    assert len(frame) == HEADER_SIZE + CONFIG_ACK_LEN
    assert frame[7] == MessageType.CONFIG_ACK
    out = decode_config_ack(frame[HEADER_SIZE:])
    assert out.responder_uid == 0xAABBCCDD
    assert out.responder_numonce == 42
    assert out.status == AckStatus.OK
    assert out.applied_keys == 3


def test_config_ack_decode_rejects_wrong_length():
    with pytest.raises(ValueError):
        decode_config_ack(bytes(CONFIG_ACK_LEN - 1))
    with pytest.raises(ValueError):
        decode_config_ack(bytes(CONFIG_ACK_LEN + 1))
