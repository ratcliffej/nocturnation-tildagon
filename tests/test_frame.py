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
    MusicEventType,
    parse_frame,
)
from nocturnation.protocol.constants import Time, Chance


# Manual annex C.1: LIGHT_COMMAND from source_id 1, sequence 42, broadcast,
# red (255, 0, 0), envelope T_96 / T_0 / T_480, CHANCE_100.
LIGHT_COMMAND_VECTOR = bytes([
    0x01,  # protocol_version
    0x01,  # source_id
    0x2A,  # sequence (42)
    0x00,  # hop_count
    0x03,  # message_type LIGHT_COMMAND
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
HEARTBEAT_VECTOR = bytes([
    0x01,  # protocol_version
    0x01,  # source_id
    0x2B,  # sequence (43)
    0x00,  # hop_count
    0x00,  # message_type HEARTBEAT
    0x00,  # payload_len
])

# Manual annex C.3: MUSIC_EVENT carrying DROP from source_id 1, sequence 44.
MUSIC_EVENT_DROP_VECTOR = bytes([
    0x01,  # protocol_version
    0x01,  # source_id
    0x2C,  # sequence (44)
    0x00,  # hop_count
    0x06,  # message_type MUSIC_EVENT
    0x01,  # payload_len
    0x01,  # event_type DROP
])


class TestHeaderParsing:
    def test_light_command_header_fields(self):
        f = parse_frame(LIGHT_COMMAND_VECTOR)
        assert f.protocol_version == 0x01
        assert f.source_id == 1
        assert f.sequence_number == 42
        assert f.hop_count == 0
        assert f.message_type == MessageType.LIGHT_COMMAND
        assert f.payload_len == 9

    def test_heartbeat_header_fields(self):
        f = parse_frame(HEARTBEAT_VECTOR)
        assert f.message_type == MessageType.HEARTBEAT
        assert f.payload_len == 0
        assert f.payload == b""

    def test_music_event_header_fields(self):
        f = parse_frame(MUSIC_EVENT_DROP_VECTOR)
        assert f.message_type == MessageType.MUSIC_EVENT
        assert f.payload_len == 1
        assert f.payload == bytes([MusicEventType.DROP])


class TestLightCommandPayload:
    def test_light_command_unpacks_all_fields(self):
        f = parse_frame(LIGHT_COMMAND_VECTOR)
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

    def test_wrong_version_rejected(self):
        bad = bytearray(LIGHT_COMMAND_VECTOR)
        bad[0] = 0x02
        with pytest.raises(FrameError):
            parse_frame(bytes(bad))

    def test_payload_len_mismatch_rejected(self):
        bad = bytearray(LIGHT_COMMAND_VECTOR)
        bad[5] = 0x08  # claim 8-byte payload, actual is 9
        with pytest.raises(FrameError):
            parse_frame(bytes(bad))

    def test_wrong_payload_len_for_known_type_rejected(self):
        # HEARTBEAT MUST be zero-payload per manual section 3.3.1.
        bad = bytearray(HEARTBEAT_VECTOR) + b"\x00"
        bad[5] = 0x01
        with pytest.raises(FrameError):
            parse_frame(bytes(bad))
