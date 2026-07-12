"""DirectorButtonMapper tests.

Rising-edge UP / DOWN, release-fire LEFT / RIGHT with long-press
sibling. See nocturnation/director/button_map.py for the semantics.
"""

from nocturnation.director import DirectorButtonMapper
from nocturnation.director.button_map import LONG_PRESS_MS
from nocturnation.shows import InputAction


class TestInstantFire:
    def test_up_is_picker(self):
        m = DirectorButtonMapper()
        assert m.poll(up=True) == [InputAction.PICKER]

    def test_down_is_settings(self):
        m = DirectorButtonMapper()
        assert m.poll(down=True) == [InputAction.SETTINGS]

    def test_nothing_pressed_is_empty(self):
        m = DirectorButtonMapper()
        assert m.poll() == []


class TestShortPressLeftRight:
    def test_right_fires_cycle_on_release(self):
        m = DirectorButtonMapper()
        # Rising edge: nothing fires yet - short vs long is unknown.
        assert m.poll(right=True, now_ms=0) == []
        # Release before threshold: CYCLE fires.
        assert m.poll(right=False, now_ms=100) == [InputAction.CYCLE]

    def test_left_fires_cycle_prev_on_release(self):
        m = DirectorButtonMapper()
        assert m.poll(left=True, now_ms=0) == []
        assert m.poll(left=False, now_ms=100) == [InputAction.CYCLE_PREV]

    def test_short_press_at_threshold_minus_one_still_short(self):
        # Released 1 ms before the long-press threshold: short-press wins.
        m = DirectorButtonMapper()
        m.poll(right=True, now_ms=0)
        assert m.poll(right=False, now_ms=LONG_PRESS_MS - 1) == [InputAction.CYCLE]


class TestLongPressLeftRight:
    def test_right_fires_section_next_at_threshold(self):
        m = DirectorButtonMapper()
        m.poll(right=True, now_ms=0)
        # Still held below threshold: nothing fires.
        assert m.poll(right=True, now_ms=LONG_PRESS_MS - 1) == []
        # At threshold: SECTION_NEXT fires.
        assert m.poll(right=True, now_ms=LONG_PRESS_MS) == [InputAction.SECTION_NEXT]

    def test_left_fires_section_prev_at_threshold(self):
        m = DirectorButtonMapper()
        m.poll(left=True, now_ms=0)
        assert m.poll(left=True, now_ms=LONG_PRESS_MS) == [InputAction.SECTION_PREV]

    def test_long_press_fires_once_per_hold(self):
        m = DirectorButtonMapper()
        m.poll(right=True, now_ms=0)
        assert m.poll(right=True, now_ms=LONG_PRESS_MS) == [InputAction.SECTION_NEXT]
        # Continued hold past the threshold: no re-fire.
        assert m.poll(right=True, now_ms=LONG_PRESS_MS + 100) == []
        assert m.poll(right=True, now_ms=LONG_PRESS_MS + 1000) == []

    def test_long_press_suppresses_short_release(self):
        # Long-press fires -> the eventual release must NOT fire CYCLE.
        m = DirectorButtonMapper()
        m.poll(right=True, now_ms=0)
        m.poll(right=True, now_ms=LONG_PRESS_MS)   # section-next fired
        assert m.poll(right=False, now_ms=LONG_PRESS_MS + 200) == []

    def test_next_press_after_long_starts_fresh_cycle(self):
        # Long-press then release, then press again: the new press
        # should behave normally (short-press releases as CYCLE).
        m = DirectorButtonMapper()
        m.poll(right=True, now_ms=0)
        m.poll(right=True, now_ms=LONG_PRESS_MS)
        m.poll(right=False, now_ms=LONG_PRESS_MS + 100)
        m.poll(right=True, now_ms=LONG_PRESS_MS + 500)
        assert m.poll(right=False, now_ms=LONG_PRESS_MS + 550) == [InputAction.CYCLE]


class TestInstantFireEdges:
    def test_hold_fires_once_up(self):
        m = DirectorButtonMapper()
        assert m.poll(up=True) == [InputAction.PICKER]
        assert m.poll(up=True) == []
        assert m.poll(up=True) == []

    def test_release_then_press_fires_up_again(self):
        m = DirectorButtonMapper()
        m.poll(up=True)
        m.poll(up=False)
        assert m.poll(up=True) == [InputAction.PICKER]

    def test_up_plus_right_fires_up_only_on_press(self):
        # UP fires instantly; RIGHT defers until release. Sequence:
        # both pressed together -> only UP appears in the fired list.
        m = DirectorButtonMapper()
        fired = m.poll(up=True, right=True, now_ms=0)
        assert fired == [InputAction.PICKER]
        # Release both without hitting threshold: RIGHT fires CYCLE.
        assert m.poll(up=False, right=False, now_ms=100) == [InputAction.CYCLE]

    def test_up_plus_down_both_fire(self):
        m = DirectorButtonMapper()
        assert m.poll(up=True, down=True) == [
            InputAction.PICKER, InputAction.SETTINGS,
        ]


class TestReset:
    def test_reset_requires_release_before_firing(self):
        m = DirectorButtonMapper()
        m.reset()
        # UP already down at entry: reset treats it as down, so no fire.
        assert m.poll(up=True) == []
        m.poll(up=False)
        assert m.poll(up=True) == [InputAction.PICKER]

    def test_reset_clears_left_right_hold_state(self):
        # A long-press timer in flight before reset must not carry over
        # into the next Director-mode entry.
        m = DirectorButtonMapper()
        m.poll(right=True, now_ms=0)
        m.reset()
        # Fresh press below threshold: short-press fires as CYCLE.
        m.poll(right=False)                          # release the reset-held state
        m.poll(right=True, now_ms=1000)
        assert m.poll(right=False, now_ms=1050) == [InputAction.CYCLE]
