"""LIGHT_FX_RUN wire-format tests (Epic 10 B1)."""

import pytest

from nocturnation.protocol import (
    FrameError,
    MessageType,
    encode_light_fx_run,
    make_light_fx_run_frame,
    parse_frame,
    FX_FLAG_START,
    FX_FLAG_REPLACE_RUNNING,
)


class TestEncodeDecodeRoundTrip:
    def test_minimal_frame_round_trips(self):
        wire = encode_light_fx_run(
            source_id=0x42,
            sequence_number=7,
            fx_id=11,
            bpm=120,
            buildup_s=4,
            flags=FX_FLAG_START,
            position_ms=0,
            params=(80, 200, 200, 200, 0, 0),
        )
        f = parse_frame(wire)
        assert f.message_type == MessageType.LIGHT_FX_RUN
        assert f.source_id == 0x42
        assert f.sequence_number == 7
        assert f.fx_id == 11
        assert f.bpm == 120
        assert f.buildup_s == 4
        assert f.flags == FX_FLAG_START
        assert f.position_ms == 0
        assert f.params == (80, 200, 200, 200, 0, 0)

    def test_position_ms_round_trips_large_value(self):
        # 4 minutes into the FX timeline -> 240_000 ms; fits in u16 only
        # if we accept >65535 saturating; per spec position_ms is u16.
        # Test the boundary explicitly: 65_535 round-trips, larger values
        # are masked to 16 bits on encode.
        wire = encode_light_fx_run(
            source_id=1, sequence_number=1, fx_id=1, position_ms=65_535,
        )
        f = parse_frame(wire)
        assert f.position_ms == 65_535

        wire = encode_light_fx_run(
            source_id=1, sequence_number=1, fx_id=1, position_ms=65_536,
        )
        f = parse_frame(wire)
        # 65_536 wraps to 0 in u16 - documented behaviour, position is
        # 16-bit. Orchestrator splits long tracks into multiple cues
        # well before this matters; the wire field still bounds the
        # maximum addressable timeline offset to ~65 s.
        assert f.position_ms == 0

    def test_cancel_frame_has_fx_id_zero(self):
        wire = encode_light_fx_run(source_id=1, sequence_number=2, fx_id=0)
        f = parse_frame(wire)
        assert f.fx_id == 0

    def test_full_params_round_trip(self):
        wire = encode_light_fx_run(
            source_id=1, sequence_number=1, fx_id=21,
            params=(255, 254, 253, 252, 251, 250),
        )
        f = parse_frame(wire)
        assert f.params == (255, 254, 253, 252, 251, 250)

    def test_params_masked_to_u8(self):
        wire = encode_light_fx_run(
            source_id=1, sequence_number=1, fx_id=1,
            params=(256, 257, 0x1FF, 0, 0, 0),
        )
        f = parse_frame(wire)
        assert f.params == (0, 1, 0xFF, 0, 0, 0)


class TestEncoderValidation:
    def test_rejects_wrong_param_count(self):
        with pytest.raises(FrameError):
            encode_light_fx_run(
                source_id=1, sequence_number=1, fx_id=1,
                params=(0, 0, 0),    # only 3
            )
        with pytest.raises(FrameError):
            encode_light_fx_run(
                source_id=1, sequence_number=1, fx_id=1,
                params=(0,) * 7,     # 7
            )


class TestFrameSize:
    def test_wire_length_is_8_plus_13(self):
        wire = encode_light_fx_run(source_id=1, sequence_number=1, fx_id=1)
        # 8 byte header + 13 byte payload = 21 bytes total
        assert len(wire) == 21

    def test_payload_len_field_matches(self):
        wire = encode_light_fx_run(source_id=1, sequence_number=1, fx_id=1)
        # Byte 7 of the header carries the payload length.
        assert wire[7] == 13

    def test_message_type_byte(self):
        wire = encode_light_fx_run(source_id=1, sequence_number=1, fx_id=1)
        # Byte 6 of the header carries the message type.
        assert wire[6] == 0x09
        assert wire[6] == MessageType.LIGHT_FX_RUN

    def test_reserved_byte_is_zero(self):
        wire = encode_light_fx_run(
            source_id=1, sequence_number=1, fx_id=11,
            params=(1, 2, 3, 4, 5, 6),
        )
        # Last byte (index 20) is the reserved tail of the payload.
        assert wire[20] == 0


class TestMakeFrame:
    def test_make_frame_matches_parse(self):
        # make_light_fx_run_frame should produce a Frame indistinguishable
        # (for the LIGHT_FX_RUN-specific fields) from parse_frame(encode(...)).
        made = make_light_fx_run_frame(
            fx_id=11,
            bpm=138,
            buildup_s=4,
            flags=FX_FLAG_START,
            position_ms=1234,
            params=(50, 200, 0, 0, 0, 0),
            source_id=0x20,
            sequence_number=5,
        )
        wire = encode_light_fx_run(
            source_id=0x20, sequence_number=5,
            fx_id=11, bpm=138, buildup_s=4,
            flags=FX_FLAG_START, position_ms=1234,
            params=(50, 200, 0, 0, 0, 0),
        )
        parsed = parse_frame(wire)

        assert made.message_type == parsed.message_type
        assert made.fx_id == parsed.fx_id
        assert made.bpm == parsed.bpm
        assert made.buildup_s == parsed.buildup_s
        assert made.flags == parsed.flags
        assert made.position_ms == parsed.position_ms
        assert made.params == parsed.params

    def test_make_frame_rejects_wrong_param_count(self):
        with pytest.raises(FrameError):
            make_light_fx_run_frame(fx_id=1, params=(0, 0))


class TestFlagBits:
    def test_replace_running_flag_set(self):
        wire = encode_light_fx_run(
            source_id=1, sequence_number=1, fx_id=11,
            flags=FX_FLAG_START | FX_FLAG_REPLACE_RUNNING,
        )
        f = parse_frame(wire)
        assert f.flags & FX_FLAG_START
        assert f.flags & FX_FLAG_REPLACE_RUNNING

    def test_layered_flag_round_trips_for_future_use(self):
        # bit2 is reserved for layered FX (future epic). Make sure
        # we preserve it on the wire so future receivers see what
        # current senders intended.
        from nocturnation.protocol import FX_FLAG_LAYERED
        wire = encode_light_fx_run(
            source_id=1, sequence_number=1, fx_id=11,
            flags=FX_FLAG_START | FX_FLAG_LAYERED,
        )
        f = parse_frame(wire)
        assert f.flags & FX_FLAG_LAYERED
