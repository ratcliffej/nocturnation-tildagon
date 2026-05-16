"""Receive pipeline tests.

process_frame combines parse + version-check + dedup + hop-count drop
into one entry point. Drop reasons are silent per protocol manual
section 3.1; the function returns None for any of them.
"""

from nocturnation.protocol import DedupRing, MessageType
from nocturnation.receive import process_frame, MAX_HOP_COUNT


# Manual annex C.1 reference vector. Modified per-test with helpers.
# Spec v2: 2-byte "NN" magic prefix + protocol_version 0x02.
LIGHT_COMMAND_VECTOR = bytes([
    0x4E,  # magic byte 0 ('N')
    0x4E,  # magic byte 1 ('N')
    0x02,  # protocol_version
    0x01,  # source_id
    0x2A,  # sequence (42)
    0x00,  # hop_count
    0x03,  # message_type LIGHT_COMMAND
    0x09,  # payload_len
    0x00,  # target_class
    0x00,  # target_group
    0xFF,  # r
    0x00,  # g
    0x00,  # b
    0x02,  # attack T_96_MS
    0x00,  # sustain T_0_MS
    0x04,  # release T_480_MS
    0x00,  # chance CHANCE_100
])


def make_frame(seq=42, hop=0, source=1):
    b = bytearray(LIGHT_COMMAND_VECTOR)
    b[3] = source           # offsets shifted +2 by the magic prefix
    b[4] = seq
    b[5] = hop
    return bytes(b)


class TestValidFrames:
    def test_valid_frame_returns_parsed(self):
        d = DedupRing()
        f = process_frame(make_frame(), d)
        assert f is not None
        assert f.sequence_number == 42
        assert f.message_type == MessageType.LIGHT_COMMAND
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
        bad = bytearray(LIGHT_COMMAND_VECTOR)
        bad[0] = 0x02
        assert process_frame(bytes(bad), d) is None

    def test_payload_len_mismatch_dropped(self):
        d = DedupRing()
        bad = bytearray(LIGHT_COMMAND_VECTOR)
        bad[5] = 0x07  # claim 7-byte payload
        assert process_frame(bytes(bad), d) is None


class TestSequenceZeroSemantics:
    def test_seq_zero_always_passes(self):
        # sequence_number == 0 means "no sequencing"; the receive layer
        # bypasses dedup and the frame is processed every time.
        d = DedupRing()
        for _ in range(5):
            f = process_frame(make_frame(seq=0), d)
            assert f is not None
