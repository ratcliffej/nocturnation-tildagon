"""Receive pipeline tests.

process_frame combines parse + version-check + dedup + hop-count drop
into one entry point. Drop reasons are silent per protocol manual
section 3.1; the function returns None for any of them.
"""

from nocturnation.protocol import DedupRing, MessageType
from nocturnation.receive import process_frame, parse_admittable, MAX_HOP_COUNT


# Manual annex C.1 reference vector. Modified per-test with helpers.
# Spec v4 (Epic 18): 2-byte "NN" magic prefix + protocol_version 0x04
# + LE u16 source_id + LE u16 target_group + 3 trailing LED-addressing
# bytes.
LIGHT_PULSE_VECTOR = bytes([
    0x4E,  # magic byte 0 ('N')
    0x4E,  # magic byte 1 ('N')
    0x04,  # protocol_version (v4)
    0x01, 0x00,  # source_id LE u16 (v3)
    0x2A,  # sequence (42)
    0x00,  # hop_count
    0x03,  # message_type LIGHT_PULSE
    0x0D,  # payload_len (v4: 13, was 10 in v3)
    0x00,  # target_class
    0x00, 0x00,  # target_group LE u16 (v3)
    0xFF,  # r
    0x00,  # g
    0x00,  # b
    0x02,  # attack T_96_MS
    0x00,  # sustain T_0_MS
    0x04,  # release T_480_MS
    0x00,  # chance CHANCE_100
    0x00,  # led_mode (v4: LedMode.ALL default)
    0x00,  # led_modifier1 (v4)
    0x00,  # led_modifier2 (v4)
])


def make_frame(seq=42, hop=0, source=1):
    # v3 header offsets: 3-4 source_id LE, 5 seq, 6 hop
    b = bytearray(LIGHT_PULSE_VECTOR)
    b[3] = source & 0xFF
    b[4] = (source >> 8) & 0xFF
    b[5] = seq
    b[6] = hop
    return bytes(b)


class TestValidFrames:
    def test_valid_frame_returns_parsed(self):
        d = DedupRing()
        f = process_frame(make_frame(), d)
        assert f is not None
        assert f.sequence_number == 42
        assert f.message_type == MessageType.LIGHT_PULSE
        assert f.r == 0xFF


class TestDedup:
    def test_repeat_dropped(self):
        d = DedupRing()
        assert process_frame(make_frame(seq=1), d) is not None
        assert process_frame(make_frame(seq=1), d) is None

    def test_three_redundant_copies_render_once(self):
        d = DedupRing()
        f1 = process_frame(make_frame(seq=7), d)
        f2 = process_frame(make_frame(seq=7), d)
        f3 = process_frame(make_frame(seq=7), d)
        assert f1 is not None
        assert f2 is None
        assert f3 is None

    def test_different_sources_dedup_independently(self):
        d = DedupRing()
        assert process_frame(make_frame(seq=1, source=1), d) is not None
        assert process_frame(make_frame(seq=1, source=2), d) is not None


class TestHopCount:
    def test_hop_count_within_limit_passes(self):
        d = DedupRing()
        for hop in range(MAX_HOP_COUNT + 1):  # 0..3
            f = process_frame(make_frame(seq=hop + 1, hop=hop), d)
            assert f is not None, f"hop={hop} should pass"

    def test_hop_count_above_limit_dropped(self):
        d = DedupRing()
        assert process_frame(make_frame(hop=4), d) is None
        assert process_frame(make_frame(seq=43, hop=255), d) is None


class TestStructuralRejection:
    def test_short_frame_dropped(self):
        d = DedupRing()
        assert process_frame(bytes([0x01, 0x01]), d) is None

    def test_wrong_protocol_version_dropped(self):
        d = DedupRing()
        bad = bytearray(LIGHT_PULSE_VECTOR)
        bad[0] = 0x02
        assert process_frame(bytes(bad), d) is None

    def test_payload_len_mismatch_dropped(self):
        d = DedupRing()
        bad = bytearray(LIGHT_PULSE_VECTOR)
        bad[8] = 0x08  # v4 payload_len offset; claim 8, actual is 13
        assert process_frame(bytes(bad), d) is None


class TestSequenceZeroSemantics:
    def test_seq_zero_always_passes(self):
        # sequence_number == 0 means "no sequencing"; the receive layer
        # bypasses dedup and the frame is processed every time.
        d = DedupRing()
        for _ in range(5):
            f = process_frame(make_frame(seq=0), d)
            assert f is not None


class TestParseAdmittable:
    """Epic 15 bench follow-up. parse_admittable returns the parsed
    frame WITHOUT dedup so the caller can update diagnostic state
    (visible in the debug overlay) for duplicates - specifically,
    a relayed frame with the same (source_id, seq) as a direct one.
    Rendering dedup is then handled by the caller after observing.
    """

    def test_valid_frame_returns_parsed(self):
        f = parse_admittable(make_frame())
        assert f is not None
        assert f.sequence_number == 42

    def test_invalid_frame_returns_none(self):
        # Magic byte mismatch.
        bad = bytearray(make_frame())
        bad[0] = 0x00
        assert parse_admittable(bytes(bad)) is None

    def test_hop_count_above_limit_dropped(self):
        # Loop-prevention check still fires (independent of dedup).
        assert parse_admittable(make_frame(hop=4)) is None
        assert parse_admittable(make_frame(hop=255)) is None

    def test_no_dedup_so_repeats_pass(self):
        # Same (source, seq) returned each call - dedup is now the
        # caller's responsibility. This is what lets a relayed
        # frame's hop_count surface in the Tildagon debug overlay
        # even when it's a dup of a direct hop=0.
        for _ in range(5):
            f = parse_admittable(make_frame(seq=99))
            assert f is not None
            assert f.sequence_number == 99

    def test_hop_count_preserved(self):
        # The diagnostic value the overlay reads.
        f = parse_admittable(make_frame(hop=1))
        assert f is not None
        assert f.hop_count == 1
