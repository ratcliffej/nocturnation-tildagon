"""Protocol parity tests.

Byte vectors taken from the protocol manual annex C in the nocturnation-m5
repo. Anything that disagrees here is a defect in this parser, never the
manual or the reference vectors.

Reference vectors (URL-anchored so a doc update is detectable):
https://github.com/ratcliffej/nocturnation-m5/blob/main/docs/manuals/protocol-manual.md#annex-c-reference-test-vectors
"""

import pytest

from nocturnation.protocol import (
    Frame,
    FrameError,
    MessageType,
    DeviceClass,
    parse_frame,
)
from nocturnation.protocol.constants import Time, Chance


# Manual annex C.1: LIGHT_PULSE from source_id 1, sequence 42, broadcast,
# red (255, 0, 0), envelope T_96 / T_0 / T_480, CHANCE_100.
# Spec v2: 2-byte "NN" magic prefix + protocol_version 0x02.
LIGHT_PULSE_VECTOR = bytes([
    0x4E,  # magic byte 0 ('N')
    0x4E,  # magic byte 1 ('N')
    0x02,  # protocol_version
    0x01,  # source_id
    0x2A,  # sequence (42)
    0x00,  # hop_count
    0x03,  # message_type LIGHT_PULSE
    0x09,  # payload_len
    0x00,  # target_class (All)
    0x00,  # target_group (broadcast)
    0xFF,  # r
    0x00,  # g
    0x00,  # b
    0x02,  # attack T_96_MS
    0x00,  # sustain T_0_MS
    0x04,  # release T_480_MS
    0x00,  # chance CHANCE_100
])

# Manual annex C.2: HEARTBEAT from source_id 1, sequence 43.
# Spec v2 §3.3.1 payload: tick (u32 LE) + days_since_2026 (u16 LE) +
# centiseconds_today (u24 LE). Picked tick = 0x12345678 / days = 0x0123 /
# centiseconds = 0xABCDEF to exercise each field's byte width.
HEARTBEAT_VECTOR = bytes([
    0x4E,  # magic byte 0 ('N')
    0x4E,  # magic byte 1 ('N')
    0x02,  # protocol_version
    0x01,  # source_id
    0x2B,  # sequence (43)
    0x00,  # hop_count
    0x00,  # message_type HEARTBEAT
    0x09,  # payload_len
    0x78, 0x56, 0x34, 0x12,   # tick LE
    0x23, 0x01,               # days_since_2026 LE
    0xEF, 0xCD, 0xAB,         # centiseconds_today LE u24
])


class TestHeaderParsing:
    def test_light_pulse_header_fields(self):
        f = parse_frame(LIGHT_PULSE_VECTOR)
        assert f.protocol_version == 0x02
        assert f.source_id == 1
        assert f.sequence_number == 42
        assert f.hop_count == 0
        assert f.message_type == MessageType.LIGHT_PULSE
        assert f.payload_len == 9

    def test_heartbeat_header_fields(self):
        f = parse_frame(HEARTBEAT_VECTOR)
        assert f.message_type == MessageType.HEARTBEAT
        assert f.payload_len == 9


class TestHeartbeatPayload:
    def test_heartbeat_unpacks_tick_and_date_fields(self):
        f = parse_frame(HEARTBEAT_VECTOR)
        assert f.tick == 0x12345678
        assert f.days_since_2026 == 0x0123
        assert f.centiseconds_today == 0xABCDEF


class TestLightCommandPayload:
    def test_light_pulse_unpacks_all_fields(self):
        f = parse_frame(LIGHT_PULSE_VECTOR)
        assert f.target_class == DeviceClass.ALL
        assert f.target_group == 0
        assert f.r == 0xFF
        assert f.g == 0x00
        assert f.b == 0x00
        assert f.attack == Time.T_96_MS
        assert f.sustain == Time.T_0_MS
        assert f.release == Time.T_480_MS
        assert f.chance == Chance.CHANCE_100


class TestFrameRejection:
    """Receivers MUST drop structurally invalid frames silently (manual section
    3.1). Our contract surfaces these as FrameError so the receive layer can
    log / count them while still discarding cleanly.
    """

    def test_short_frame_rejected(self):
        with pytest.raises(FrameError):
            parse_frame(bytes([0x01, 0x01, 0x01]))

    def test_invalid_magic_rejected(self):
        # A frame whose first two bytes are not "NN" is foreign ESP-NOW
        # traffic or random RF noise. Reject before any further
        # validation - this is the cheapest disambiguator at
        # event-density channels (EMF etc.) where many ESP-NOW users
        # share the same band.
        bad = bytearray(LIGHT_PULSE_VECTOR)
        bad[0] = 0x18  # foreign vendor prefix; not "NN"
        with pytest.raises(FrameError):
            parse_frame(bytes(bad))

    def test_wrong_version_rejected(self):
        bad = bytearray(LIGHT_PULSE_VECTOR)
        bad[2] = 0x09  # protocol_version offset; not 0x02
        with pytest.raises(FrameError):
            parse_frame(bytes(bad))

    def test_payload_len_mismatch_rejected(self):
        bad = bytearray(LIGHT_PULSE_VECTOR)
        bad[7] = 0x08  # payload_len offset; claim 8-byte payload, actual is 9
        with pytest.raises(FrameError):
            parse_frame(bytes(bad))

    def test_wrong_payload_len_for_known_type_rejected(self):
        # HEARTBEAT MUST be 9-byte payload per spec v2 §3.3.1.
        bad = bytearray(HEARTBEAT_VECTOR)
        bad[7] = 0x05  # payload_len offset; claim 5-byte payload, actual is 9
        with pytest.raises(FrameError):
            parse_frame(bytes(bad))
