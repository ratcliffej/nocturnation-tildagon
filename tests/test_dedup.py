"""Dedup ring tests.

The ring exists to absorb the Director's 3x redundant transmission per
protocol manual section 2.3 (every frame goes out three times with the
same sequence number).
"""

from nocturnation.protocol.dedup import DedupRing, DEFAULT_CAPACITY


class TestFreshness:
    def test_fresh_frame_returns_false(self):
        d = DedupRing()
        assert d.seen(1, 42) is False

    def test_immediate_replay_returns_true(self):
        d = DedupRing()
        d.seen(1, 42)
        assert d.seen(1, 42) is True

    def test_three_consecutive_calls_match_master_3x_redundancy(self):
        # Director sends the same (source_id, seq) three times.
        d = DedupRing()
        assert d.seen(1, 100) is False  # first copy: fresh
        assert d.seen(1, 100) is True   # second copy: drop
        assert d.seen(1, 100) is True   # third copy: drop

    def test_different_source_same_sequence_independent(self):
        d = DedupRing()
        d.seen(1, 42)
        assert d.seen(2, 42) is False  # different source, not dedup'd

    def test_different_sequence_same_source_independent(self):
        d = DedupRing()
        d.seen(1, 42)
        assert d.seen(1, 43) is False


class TestSequenceZero:
    def test_seq_zero_bypasses_dedup(self):
        # Protocol manual section 3.1: sequence_number == 0 indicates
        # "no sequencing" and should not be dedup'd.
        d = DedupRing()
        assert d.seen(1, 0) is False
        assert d.seen(1, 0) is False
        assert d.seen(1, 0) is False

    def test_seq_zero_does_not_consume_ring_capacity(self):
        d = DedupRing(capacity=4)
        for _ in range(10):
            d.seen(1, 0)
        assert len(d) == 0


class TestRingEviction:
    def test_default_capacity_is_16(self):
        d = DedupRing()
        for s in range(1, 17):
            d.seen(1, s)
        assert len(d) == DEFAULT_CAPACITY == 16

    def test_capacity_plus_one_evicts_oldest(self):
        d = DedupRing(capacity=4)
        for s in range(1, 5):  # (1,1), (1,2), (1,3), (1,4)
            d.seen(1, s)
        # Ring is full. Adding (1,5) should evict (1,1).
        d.seen(1, 5)
        assert len(d) == 4
        # (1,1) is no longer in the ring, so it appears fresh again.
        assert d.seen(1, 1) is False

    def test_ring_stays_at_capacity_under_load(self):
        d = DedupRing(capacity=8)
        for s in range(1, 50):
            d.seen(1, s)
        assert len(d) == 8

    def test_recent_entry_not_evicted_by_replay(self):
        d = DedupRing(capacity=2)
        d.seen(1, 1)
        d.seen(1, 2)
        # Replaying (1, 2) returns True without changing the ring.
        assert d.seen(1, 2) is True
        # (1, 1) is still in the ring.
        assert d.seen(1, 1) is True


class TestCustomCapacity:
    def test_capacity_one(self):
        d = DedupRing(capacity=1)
        d.seen(1, 1)
        assert d.seen(1, 1) is True
        d.seen(1, 2)  # evicts (1, 1)
        assert d.seen(1, 1) is False

    def test_capacity_zero_treats_everything_as_fresh(self):
        # Edge case: a zero-capacity ring degenerates to "no dedup".
        d = DedupRing(capacity=0)
        assert d.seen(1, 1) is False
        assert d.seen(1, 1) is False
