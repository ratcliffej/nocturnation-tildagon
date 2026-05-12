"""Channel scanner state-machine tests.

The scanner doesn't talk to radio hardware; it tracks "which channel
should I be on right now" so the caller can drive the actual receive
operations. These tests cover the rotation, lock, and unlock semantics
from protocol manual section 5.3.
"""

import pytest

from nocturnation.channel_scan import ChannelScanner, SCAN_ORDER


class TestInitialState:
    def test_starts_on_first_channel_in_order(self):
        s = ChannelScanner()
        assert s.current_channel == SCAN_ORDER[0] == 11
        assert s.is_locked is False

    def test_default_listen_window_is_2000_ms(self):
        s = ChannelScanner()
        assert s.listen_ms == 2000


class TestAdvance:
    def test_advance_rotates_to_channel_1(self):
        s = ChannelScanner()
        assert s.advance() == 1
        assert s.current_channel == 1

    def test_advance_wraps_back_to_11(self):
        s = ChannelScanner()
        s.advance()  # -> 1
        s.advance()  # wraps -> 11
        assert s.current_channel == 11

    def test_advance_after_lock_raises(self):
        s = ChannelScanner()
        s.lock()
        with pytest.raises(RuntimeError):
            s.advance()


class TestLock:
    def test_lock_with_no_arg_pins_current_channel(self):
        s = ChannelScanner()
        s.advance()
        assert s.current_channel == 1
        s.lock()
        assert s.is_locked is True
        assert s.current_channel == 1

    def test_lock_with_explicit_channel(self):
        s = ChannelScanner()
        # Operator pre-configured a specific channel
        s.lock(11)
        assert s.is_locked is True
        assert s.current_channel == 11

    def test_lock_rejects_channel_not_in_scan_order(self):
        # Channel 6 is not in SCAN_ORDER per protocol manual section 5.3.
        s = ChannelScanner()
        with pytest.raises(ValueError):
            s.lock(6)


class TestUnlock:
    def test_unlock_resumes_at_scan_order_head(self):
        s = ChannelScanner()
        s.advance()  # on 1
        s.lock()     # lock to 1
        s.unlock()
        assert s.is_locked is False
        assert s.current_channel == SCAN_ORDER[0] == 11

    def test_unlock_idempotent(self):
        s = ChannelScanner()
        s.lock()
        s.unlock()
        s.unlock()  # second call must not raise
        assert s.is_locked is False


class TestCustomScanOrder:
    def test_custom_order_rotates_correctly(self):
        s = ChannelScanner(order=(11, 6, 1))
        assert s.current_channel == 11
        s.advance()
        assert s.current_channel == 6
        s.advance()
        assert s.current_channel == 1
        s.advance()
        assert s.current_channel == 11

    def test_empty_order_rejected(self):
        with pytest.raises(ValueError):
            ChannelScanner(order=())

    def test_custom_listen_window(self):
        s = ChannelScanner(listen_ms=500)
        assert s.listen_ms == 500
