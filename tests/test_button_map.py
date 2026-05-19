"""DirectorButtonMapper tests.

Edge-triggered nav buttons -> InputAction. Does its own rising-edge
detection so the app doesn't need button_states.clear().
"""

from nocturnation.director import DirectorButtonMapper
from nocturnation.shows import InputAction


class TestMapping:
    def test_up_is_picker(self):
        m = DirectorButtonMapper()
        assert m.poll(up=True) == [InputAction.PICKER]

    def test_down_is_settings(self):
        m = DirectorButtonMapper()
        assert m.poll(down=True) == [InputAction.SETTINGS]

    def test_right_is_cycle(self):
        m = DirectorButtonMapper()
        assert m.poll(right=True) == [InputAction.CYCLE]

    def test_left_is_cycle_prev(self):
        m = DirectorButtonMapper()
        assert m.poll(left=True) == [InputAction.CYCLE_PREV]

    def test_nothing_pressed_is_empty(self):
        m = DirectorButtonMapper()
        assert m.poll() == []


class TestEdgeDetection:
    def test_hold_fires_once(self):
        m = DirectorButtonMapper()
        assert m.poll(up=True) == [InputAction.PICKER]
        assert m.poll(up=True) == []      # held, no re-fire
        assert m.poll(up=True) == []

    def test_release_then_press_fires_again(self):
        m = DirectorButtonMapper()
        m.poll(up=True)
        m.poll(up=False)                  # release
        assert m.poll(up=True) == [InputAction.PICKER]

    def test_multiple_buttons_same_poll(self):
        m = DirectorButtonMapper()
        fired = m.poll(up=True, right=True)
        # Stable nav order: up (picker) before right (cycle).
        assert fired == [InputAction.PICKER, InputAction.CYCLE]

    def test_independent_per_button(self):
        m = DirectorButtonMapper()
        assert m.poll(up=True) == [InputAction.PICKER]
        # up still held; down newly pressed -> only down fires.
        assert m.poll(up=True, down=True) == [InputAction.SETTINGS]


class TestReset:
    def test_reset_requires_release_before_firing(self):
        m = DirectorButtonMapper()
        m.reset()
        # Button already down at entry: reset treats it as down, so no
        # immediate fire.
        assert m.poll(up=True) == []
        # Release, then press -> fires.
        m.poll(up=False)
        assert m.poll(up=True) == [InputAction.PICKER]
