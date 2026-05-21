"""LIGHT_COMMAND encoder tests.

The Director builds frames to broadcast (the Tildagon was receive-only
until Director mode). These tests pin the wire byte layout and confirm
encode -> parse round-trips cleanly against the existing parser.
"""

from nocturnation.protocol import (
    encode_light_command,
    encode_heartbeat,
    make_light_command_frame,
    parse_frame,
)
from nocturnation.protocol.constants import (
    MAGIC_0,
    MAGIC_1,
    PROTOCOL_VERSION,
    HEADER_SIZE,
    MessageType,
    DeviceClass,
    Time,
    Chance,
)


class TestEncodeByteLayout:
    def test_header_and_payload_bytes(self):
        buf = encode_light_command(
            source_id=0x12,
            sequence_number=0x34,
            target_class=DeviceClass.LIGHT,
            target_group=0x01,
            r=255, g=128, b=0,
            attack=Time.T_0_MS,
            sustain=Time.T_96_MS,
            release=Time.T_480_MS,
            chance=Chance.CHANCE_100,
        )
        assert buf == bytes((
            MAGIC_0, MAGIC_1, PROTOCOL_VERSION,
            0x12,            # source_id
            0x34,            # sequence
            0x00,            # hop_count default
            MessageType.LIGHT_COMMAND,
            0x09,            # payload_len
            0x01,            # target_class (Light)
            0x01,            # target_group
            255, 128, 0,     # rgb
            Time.T_0_MS, Time.T_96_MS, Time.T_480_MS,
            Chance.CHANCE_100,
        ))

    def test_total_length_is_17(self):
        buf = encode_light_command(1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        assert len(buf) == HEADER_SIZE + 9 == 17

    def test_hop_count_override(self):
        buf = encode_light_command(1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, hop_count=3)
        assert buf[5] == 3

    def test_fields_masked_to_byte(self):
        # Out-of-range args must not corrupt the frame length.
        buf = encode_light_command(
            source_id=0x1FF, sequence_number=0x100,
            target_class=0x101, target_group=0x102,
            r=0x1FF, g=0x200, b=0x2FF,
            attack=0x108, sustain=0x109, release=0x10A, chance=0x10B,
        )
        assert len(buf) == 17
        assert buf[3] == 0xFF   # source_id masked
        assert buf[4] == 0x00   # sequence masked (0x100 & 0xFF)
        assert buf[10] == 0xFF  # r masked


class TestEncodeParseRoundTrip:
    def test_round_trip_preserves_fields(self):
        buf = encode_light_command(
            source_id=0x40,
            sequence_number=7,
            target_class=DeviceClass.MULTI_LED_SCREEN,
            target_group=2,
            r=10, g=20, b=30,
            attack=Time.T_32_MS,
            sustain=Time.T_192_MS,
            release=Time.T_960_MS,
            chance=Chance.CHANCE_50,
        )
        f = parse_frame(buf)
        assert f.protocol_version == PROTOCOL_VERSION
        assert f.source_id == 0x40
        assert f.sequence_number == 7
        assert f.hop_count == 0
        assert f.message_type == MessageType.LIGHT_COMMAND
        assert f.payload_len == 9
        assert f.target_class == DeviceClass.MULTI_LED_SCREEN
        assert f.target_group == 2
        assert (f.r, f.g, f.b) == (10, 20, 30)
        assert f.attack == Time.T_32_MS
        assert f.sustain == Time.T_192_MS
        assert f.release == Time.T_960_MS
        assert f.chance == Chance.CHANCE_50

    def test_encoded_frame_passes_parser_validation(self):
        # A frame we built should never trip the parser's structural
        # checks (magic, version, payload_len agreement).
        buf = encode_light_command(0x05, 0, DeviceClass.ALL, 0, 1, 2, 3, 0, 0, 0, 0)
        f = parse_frame(buf)  # would raise FrameError on any mismatch
        assert f.message_type == MessageType.LIGHT_COMMAND


class TestEncodeHeartbeat:
    def test_byte_layout(self):
        buf = encode_heartbeat(
            source_id=0x20,
            sequence_number=7,
            tick=0x01020304,
            days_since_2026=0x0506,
            centiseconds_today=0x070809,
        )
        assert buf == bytes((
            MAGIC_0, MAGIC_1, PROTOCOL_VERSION,
            0x20,            # source_id
            7,               # sequence
            0x00,            # hop_count
            MessageType.HEARTBEAT,
            0x09,            # payload_len
            # tick u32 LE
            0x04, 0x03, 0x02, 0x01,
            # days_since_2026 u16 LE
            0x06, 0x05,
            # centiseconds_today u24 LE
            0x09, 0x08, 0x07,
        ))
        assert len(buf) == 17

    def test_round_trip(self):
        buf = encode_heartbeat(0x20, 3, tick=123456, days_since_2026=42,
                               centiseconds_today=8675309 & 0xFFFFFF)
        f = parse_frame(buf)
        assert f.message_type == MessageType.HEARTBEAT
        assert f.source_id == 0x20
        assert f.sequence_number == 3
        assert f.tick == 123456
        assert f.days_since_2026 == 42
        assert f.centiseconds_today == (8675309 & 0xFFFFFF)

    def test_defaults_are_zero(self):
        f = parse_frame(encode_heartbeat(0x20, 0))
        assert f.tick == 0
        assert f.days_since_2026 == 0
        assert f.centiseconds_today == 0


class TestMakeLightCommandFrame:
    def test_builds_frame_with_payload_fields(self):
        f = make_light_command_frame(
            target_class=DeviceClass.SCREEN,
            target_group=5,
            r=100, g=110, b=120,
            attack=Time.T_0_MS,
            sustain=Time.T_480_MS,
            release=Time.T_480_MS,
            chance=Chance.CHANCE_88,
        )
        assert f.message_type == MessageType.LIGHT_COMMAND
        assert f.target_class == DeviceClass.SCREEN
        assert f.target_group == 5
        assert (f.r, f.g, f.b) == (100, 110, 120)
        assert f.attack == Time.T_0_MS
        assert f.sustain == Time.T_480_MS
        assert f.release == Time.T_480_MS
        assert f.chance == Chance.CHANCE_88

    def test_matches_encoded_then_parsed_frame(self):
        # The direct-built loopback Frame should carry the same
        # payload fields as one round-tripped through the wire.
        direct = make_light_command_frame(
            DeviceClass.LIGHT, 1, 200, 50, 25,
            Time.T_0_MS, Time.T_96_MS, Time.T_480_MS, Chance.CHANCE_100,
        )
        parsed = parse_frame(encode_light_command(
            1, 0, DeviceClass.LIGHT, 1, 200, 50, 25,
            Time.T_0_MS, Time.T_96_MS, Time.T_480_MS, Chance.CHANCE_100,
        ))
        for attr in ("target_class", "target_group", "r", "g", "b",
                     "attack", "sustain", "release", "chance"):
            assert getattr(direct, attr) == getattr(parsed, attr)
