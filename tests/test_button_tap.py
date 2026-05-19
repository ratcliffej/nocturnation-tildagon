"""ButtonTapSource tests.

Button-as-tap fallback: polled button state -> synthetic tap events,
same on_tap(strength) shape as the IMU adapter.
"""

from nocturnation.director import ButtonTapSource, DEFAULT_TAP_STRENGTH


class _Recorder:
    def __init__(self):
        self.taps = []

    def on_tap(self, strength):
        self.taps.append(strength)


class TestEdgeMode:
    def test_press_fires_one_tap(self):
        rec = _Recorder()
        s = ButtonTapSource(on_tap=rec.on_tap)
        assert s.poll(True, 0) is True
        assert rec.taps == [DEFAULT_TAP_STRENGTH]

    def test_no_refire_while_held(self):
        rec = _Recorder()
        s = ButtonTapSource(on_tap=rec.on_tap)
        s.poll(True, 0)       # rising edge -> tap
        s.poll(True, 20)      # still held -> nothing (edge-only)
        s.poll(True, 1000)    # still held, long time -> still nothing
        assert rec.taps == [DEFAULT_TAP_STRENGTH]

    def test_release_then_press_fires_again(self):
        rec = _Recorder()
        s = ButtonTapSource(on_tap=rec.on_tap)
        s.poll(True, 0)    # tap 1
        s.poll(False, 20)  # release
        s.poll(True, 40)   # tap 2
        assert len(rec.taps) == 2

    def test_release_alone_fires_nothing(self):
        rec = _Recorder()
        s = ButtonTapSource(on_tap=rec.on_tap)
        assert s.poll(False, 0) is False
        assert rec.taps == []

    def test_not_pressed_stays_quiet(self):
        rec = _Recorder()
        s = ButtonTapSource(on_tap=rec.on_tap)
        for t in range(5):
            s.poll(False, t * 20)
        assert rec.taps == []


class TestAutoRepeat:
    def test_held_repeats_at_interval(self):
        rec = _Recorder()
        s = ButtonTapSource(on_tap=rec.on_tap, repeat_ms=100)
        s.poll(True, 0)     # rising edge -> tap 1
        s.poll(True, 50)    # 50 ms held, < 100 -> nothing
        s.poll(True, 100)   # 100 ms since last -> tap 2
        s.poll(True, 150)   # < 100 since tap 2 -> nothing
        s.poll(True, 200)   # tap 3
        assert len(rec.taps) == 3

    def test_repeat_stops_on_release(self):
        rec = _Recorder()
        s = ButtonTapSource(on_tap=rec.on_tap, repeat_ms=100)
        s.poll(True, 0)     # tap 1
        s.poll(True, 100)   # tap 2
        s.poll(False, 150)  # release
        s.poll(False, 300)  # stays released
        assert len(rec.taps) == 2

    def test_edge_only_default_does_not_repeat(self):
        rec = _Recorder()
        s = ButtonTapSource(on_tap=rec.on_tap)  # repeat_ms=0
        s.poll(True, 0)
        s.poll(True, 10000)  # held forever; no repeat
        assert len(rec.taps) == 1


class TestStrength:
    def test_custom_strength(self):
        rec = _Recorder()
        s = ButtonTapSource(on_tap=rec.on_tap, strength=255)
        s.poll(True, 0)
        assert rec.taps == [255]

    def test_strength_clamped(self):
        rec = _Recorder()
        s = ButtonTapSource(on_tap=rec.on_tap, strength=500)
        s.poll(True, 0)
        assert rec.taps == [255]

    def test_strength_clamped_negative(self):
        rec = _Recorder()
        s = ButtonTapSource(on_tap=rec.on_tap, strength=-5)
        s.poll(True, 0)
        assert rec.taps == [0]


class TestReset:
    def test_reset_clears_held_state(self):
        rec = _Recorder()
        s = ButtonTapSource(on_tap=rec.on_tap)
        s.poll(True, 0)      # tap 1, now "was_pressed"
        s.reset()
        # After reset, a held button reads as a fresh rising edge.
        assert s.poll(True, 20) is True
        assert len(rec.taps) == 2

    def test_button_down_at_entry_no_spurious_tap_after_reset_then_release(self):
        # reset() then a held button does fire (rising edge from the
        # reset baseline) - documented behaviour. To avoid a spurious
        # tap, the caller resets when the button is known up.
        rec = _Recorder()
        s = ButtonTapSource(on_tap=rec.on_tap)
        s.reset()
        s.poll(False, 0)  # button up at entry
        s.poll(True, 20)  # genuine press
        assert len(rec.taps) == 1


class TestNoCallback:
    def test_poll_without_callback_does_not_crash(self):
        s = ButtonTapSource(on_tap=None)
        assert s.poll(True, 0) is True  # fired, but no callback to call

    def test_set_callback_late_binds(self):
        rec = _Recorder()
        s = ButtonTapSource(on_tap=None)
        s.poll(True, 0)        # fires, no callback
        s.poll(False, 20)
        s.set_callback(rec.on_tap)
        s.poll(True, 40)       # now records
        assert rec.taps == [DEFAULT_TAP_STRENGTH]
