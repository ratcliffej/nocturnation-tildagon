"""LIGHT_PULSE encoder tests.

The Director builds frames to broadcast (the Tildagon was receive-only
until Director mode). These tests pin the wire byte layout and confirm
encode -> parse round-trips cleanly against the existing parser.
"""

from nocturnation.protocol import (
    encode_light_pulse,
    encode_light_wash,
    encode_light_wash_end,
    encode_light_wash_pulse,
    encode_heartbeat,
    make_light_pulse_frame,
    parse_frame,
    LedMode,
    pack_repeat_mask,
    unpack_repeat_mask,
    LED_REPEAT_MASK_MAX,
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
        buf = encode_light_pulse(
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
            0x12, 0x00,      # source_id LE u16 (v3)
            0x34,            # sequence
            0x00,            # hop_count default
            MessageType.LIGHT_PULSE,
            0x0D,            # payload_len (v4: 13, was 10 in v3)
            0x01,            # target_class (Light)
            0x01, 0x00,      # target_group LE u16 (v3)
            255, 128, 0,     # rgb
            Time.T_0_MS, Time.T_96_MS, Time.T_480_MS,
            Chance.CHANCE_100,
            0x00, 0x00, 0x00,  # v4: led_mode + led_modifier1/2 default 0
        ))

    def test_total_length_is_22(self):
        # v4: 9-byte header + 13-byte payload = 22 (v3 was 19, v2 was 17).
        buf = encode_light_pulse(1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        assert len(buf) == HEADER_SIZE + 13 == 22

    def test_hop_count_override(self):
        # v3+: hop_count moved from offset 5 to offset 6 (unchanged in v4).
        buf = encode_light_pulse(1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, hop_count=3)
        assert buf[6] == 3

    def test_fields_masked(self):
        # Out-of-range args must not corrupt the frame length.
        # v3+: source_id / target_group masked to u16; offsets shifted.
        buf = encode_light_pulse(
            source_id=0x1FFFF, sequence_number=0x100,
            target_class=0x101, target_group=0x1FFFF,
            r=0x1FF, g=0x200, b=0x2FF,
            attack=0x108, sustain=0x109, release=0x10A, chance=0x10B,
        )
        assert len(buf) == 22   # v4
        assert buf[3]  == 0xFF   # source_id LSB masked from 0x1FFFF
        assert buf[4]  == 0xFF   # source_id MSB masked from 0x1FFFF
        assert buf[5]  == 0x00   # sequence masked (0x100 & 0xFF)
        # v3+ layout: buf[9]=target_class, buf[10..11]=target_group LE,
        # buf[12]=r ...
        assert buf[9]  == 0x01   # target_class masked from 0x101
        assert buf[10] == 0xFF   # target_group LSB masked from 0x1FFFF
        assert buf[11] == 0xFF   # target_group MSB masked from 0x1FFFF
        assert buf[12] == 0xFF   # r masked from 0x1FF


class TestEncodeParseRoundTrip:
    def test_round_trip_preserves_fields(self):
        buf = encode_light_pulse(
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
        assert f.message_type == MessageType.LIGHT_PULSE
        assert f.payload_len == 13   # v4 (was 10 in v3)
        assert f.target_class == DeviceClass.MULTI_LED_SCREEN
        assert f.target_group == 2
        assert (f.r, f.g, f.b) == (10, 20, 30)
        assert f.attack == Time.T_32_MS
        assert f.sustain == Time.T_192_MS
        assert f.release == Time.T_960_MS
        assert f.chance == Chance.CHANCE_50
        # v4 default LED addressing (LedMode.ALL, both modifiers 0).
        assert f.led_mode == LedMode.ALL
        assert f.led_modifier1 == 0
        assert f.led_modifier2 == 0

    def test_encoded_frame_passes_parser_validation(self):
        # A frame we built should never trip the parser's structural
        # checks (magic, version, payload_len agreement).
        buf = encode_light_pulse(0x05, 0, DeviceClass.ALL, 0, 1, 2, 3, 0, 0, 0, 0)
        f = parse_frame(buf)  # would raise FrameError on any mismatch
        assert f.message_type == MessageType.LIGHT_PULSE


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
            0x20, 0x00,      # source_id LE u16 (v3)
            7,               # sequence
            0x00,            # hop_count
            MessageType.HEARTBEAT,
            0x09,            # payload_len (heartbeat unchanged - no target_group)
            # tick u32 LE
            0x04, 0x03, 0x02, 0x01,
            # days_since_2026 u16 LE
            0x06, 0x05,
            # centiseconds_today u24 LE
            0x09, 0x08, 0x07,
        ))
        # v3: 9-byte header + 9-byte payload = 18 (v2 was 8+9 = 17)
        assert len(buf) == 18

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
        f = make_light_pulse_frame(
            target_class=DeviceClass.SCREEN,
            target_group=5,
            r=100, g=110, b=120,
            attack=Time.T_0_MS,
            sustain=Time.T_480_MS,
            release=Time.T_480_MS,
            chance=Chance.CHANCE_88,
        )
        assert f.message_type == MessageType.LIGHT_PULSE
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
        direct = make_light_pulse_frame(
            DeviceClass.LIGHT, 1, 200, 50, 25,
            Time.T_0_MS, Time.T_96_MS, Time.T_480_MS, Chance.CHANCE_100,
        )
        parsed = parse_frame(encode_light_pulse(
            1, 0, DeviceClass.LIGHT, 1, 200, 50, 25,
            Time.T_0_MS, Time.T_96_MS, Time.T_480_MS, Chance.CHANCE_100,
        ))
        for attr in ("target_class", "target_group", "r", "g", "b",
                     "attack", "sustain", "release", "chance"):
            assert getattr(direct, attr) == getattr(parsed, attr)


class TestV3WideRange:
    """v3 widened source_id and target_group to u16 LE. The current UI
    only generates values <= 0xFF, but the wire must carry the extended
    range end-to-end so a future NVS/UI widening slots in without a
    second wire break. These pin that contract with values that
    exercise both bytes.
    """

    def test_source_id_extended_range_round_trip(self):
        # 0xBEEF fits u16, exceeds u8; a v2 receiver would have masked
        # the top byte silently, a v3 receiver must preserve it.
        buf = encode_light_pulse(
            source_id=0xBEEF, sequence_number=1,
            target_class=0, target_group=0,
            r=0, g=0, b=0, attack=0, sustain=0, release=0, chance=0,
        )
        # Source_id on the wire: bytes 3 (LSB) + 4 (MSB), little-endian.
        assert buf[3] == 0xEF
        assert buf[4] == 0xBE
        f = parse_frame(buf)
        assert f.source_id == 0xBEEF

    def test_target_group_extended_range_round_trip(self):
        # 0x1234 similarly exercises both target_group bytes.
        buf = encode_light_pulse(
            source_id=0x05, sequence_number=1,
            target_class=DeviceClass.LIGHT, target_group=0x1234,
            r=0, g=0, b=0, attack=0, sustain=0, release=0, chance=0,
        )
        # target_group on the wire: bytes 10 (LSB) + 11 (MSB) (v3+ offsets).
        assert buf[10] == 0x34
        assert buf[11] == 0x12
        f = parse_frame(buf)
        assert f.target_group == 0x1234


class TestV4LedAddressing:
    """v4 (Epic 18) added LedMode + 2-byte LedModifier to LIGHT_PULSE,
    LIGHT_WASH, LIGHT_WASH_END and LIGHT_WASH_PULSE. Wire is a hard
    cutover from v3 (protocol_version == 0x04). Encoders default the
    LED fields to zero so a v4 Director that hasn't yet been taught
    to fill them produces frames indistinguishable from v3-era
    behaviour on the render side.
    """

    def test_light_pulse_single_led_wire_bytes(self):
        # SingleLed with chain=2, LED-index=17.
        buf = encode_light_pulse(
            source_id=0x01, sequence_number=1,
            target_class=DeviceClass.LIGHT, target_group=3,
            r=100, g=50, b=25,
            attack=Time.T_96_MS, sustain=Time.T_480_MS,
            release=Time.T_960_MS, chance=Chance.CHANCE_100,
            led_mode=LedMode.SINGLE_LED,
            led_modifier1=2,
            led_modifier2=17,
        )
        assert buf[HEADER_SIZE + 10] == LedMode.SINGLE_LED
        assert buf[HEADER_SIZE + 11] == 2
        assert buf[HEADER_SIZE + 12] == 17
        f = parse_frame(buf)
        assert f.led_mode      == LedMode.SINGLE_LED
        assert f.led_modifier1 == 2
        assert f.led_modifier2 == 17

    def test_light_wash_repeat_pattern_round_trip(self):
        # Alternating tile: 0b101010101010 = 0xAAA.
        m1, m2 = unpack_repeat_mask(0x0AAA)
        assert (m1, m2) == (0xAA, 0x0A)
        buf = encode_light_wash(
            source_id=0x01, sequence_number=1,
            target_class=DeviceClass.LIGHT, target_group=1,
            r1=128, g1=64, b1=32, r2=0, g2=0, b2=0,
            attack=10, release=10, intensity=255,
            cycle_ms=0, ttl_seconds=0, pulse_response=0,
            led_mode=LedMode.REPEAT_PATTERN,
            led_modifier1=m1, led_modifier2=m2,
        )
        # LIGHT_WASH v4 puts LED bytes at payload offsets 17..19.
        assert buf[HEADER_SIZE + 17] == LedMode.REPEAT_PATTERN
        assert buf[HEADER_SIZE + 18] == 0xAA
        assert buf[HEADER_SIZE + 19] == 0x0A
        f = parse_frame(buf)
        assert f.led_mode == LedMode.REPEAT_PATTERN
        assert pack_repeat_mask(f.led_modifier1, f.led_modifier2) == 0x0AAA

    def test_light_wash_end_single_led_round_trip(self):
        buf = encode_light_wash_end(
            source_id=0x01, sequence_number=1,
            target_class=DeviceClass.LIGHT, target_group=7,
            release_time=20,
            led_mode=LedMode.SINGLE_LED,
            led_modifier1=0, led_modifier2=3,
        )
        # LIGHT_WASH_END v4 puts LED bytes at payload offsets 4..6.
        assert buf[HEADER_SIZE + 4] == LedMode.SINGLE_LED
        assert buf[HEADER_SIZE + 5] == 0
        assert buf[HEADER_SIZE + 6] == 3
        f = parse_frame(buf)
        assert f.led_mode      == LedMode.SINGLE_LED
        assert f.led_modifier1 == 0
        assert f.led_modifier2 == 3

    def test_light_wash_pulse_single_led_round_trip(self):
        buf = encode_light_wash_pulse(
            source_id=0x01, sequence_number=1,
            target_class=DeviceClass.LIGHT, target_group=1,
            r=200, g=100, b=50,
            attack=1, sustain=2, release=3, chance=4,
            led_mode=LedMode.SINGLE_LED,
            led_modifier1=1, led_modifier2=5,
        )
        # LIGHT_WASH_PULSE v4 mirrors LIGHT_PULSE: LED bytes at 10..12.
        assert buf[HEADER_SIZE + 10] == LedMode.SINGLE_LED
        assert buf[HEADER_SIZE + 11] == 1
        assert buf[HEADER_SIZE + 12] == 5
        f = parse_frame(buf)
        assert f.led_mode      == LedMode.SINGLE_LED
        assert f.led_modifier1 == 1
        assert f.led_modifier2 == 5

    def test_repeat_mask_pack_unpack_boundaries(self):
        for mask in (0x000, 0x001, 0x555, 0xAAA, LED_REPEAT_MASK_MAX):
            m1, m2 = unpack_repeat_mask(mask)
            assert m1 == mask & 0xFF
            assert m2 == (mask >> 8) & 0x0F
            assert pack_repeat_mask(m1, m2) == mask
        # Reserved upper 4 bits of modifier2 must be ignored on pack.
        assert pack_repeat_mask(0xFF, 0xFF) == LED_REPEAT_MASK_MAX == 0x0FFF
