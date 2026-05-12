"""SignalTracker tests.

Tracks the gap since the last accepted frame; reports True from
is_lost when the gap exceeds the NO_SIGNAL threshold (or when no
frame has ever been seen).
"""

from nocturnation.signal_tracker import SignalTracker, NO_SIGNAL_GAP_MS


class TestInitialState:
    def test_fresh_tracker_reports_lost(self):
        # On app launch before any frame arrives, NO SIGNAL is the
        # truthful state.
        s = SignalTracker()
        assert s.is_lost(now_ms=0) is True
        assert s.is_lost(now_ms=100000) is True

    def test_default_gap_is_3000ms(self):
        # Protocol manual section 6.2: three missed heartbeats at 1 Hz.
        assert NO_SIGNAL_GAP_MS == 3000


class TestRecordFrame:
    def test_record_then_same_time_not_lost(self):
        s = SignalTracker()
        s.record_frame(now_ms=1000)
        assert s.is_lost(now_ms=1000) is False

    def test_within_gap_not_lost(self):
        s = SignalTracker()
        s.record_frame(now_ms=1000)
        assert s.is_lost(now_ms=1000 + NO_SIGNAL_GAP_MS) is False

    def test_past_gap_lost(self):
        s = SignalTracker()
        s.record_frame(now_ms=1000)
        assert s.is_lost(now_ms=1000 + NO_SIGNAL_GAP_MS + 1) is True

    def test_fresh_record_clears_lost_state(self):
        s = SignalTracker()
        s.record_frame(now_ms=1000)
        # Drift past the gap
        assert s.is_lost(now_ms=5000) is True
        # New frame arrives
        s.record_frame(now_ms=5100)
        assert s.is_lost(now_ms=5100) is False
        assert s.is_lost(now_ms=8000) is False


class TestReset:
    def test_reset_clears_record(self):
        s = SignalTracker()
        s.record_frame(now_ms=1000)
        assert s.is_lost(now_ms=1500) is False
        s.reset()
        assert s.is_lost(now_ms=1500) is True


class TestCustomGap:
    def test_custom_gap_honoured(self):
        s = SignalTracker(gap_ms=500)
        s.record_frame(now_ms=0)
        assert s.is_lost(now_ms=500) is False
        assert s.is_lost(now_ms=501) is True
