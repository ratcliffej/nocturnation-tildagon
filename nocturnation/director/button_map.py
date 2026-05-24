"""DirectorButtonMapper - edge-triggered nav buttons -> InputAction.

The Tildagon has six buttons (UP / DOWN / LEFT / RIGHT / CONFIRM /
CANCEL) with only instantaneous pressed state (no long-press). In
Director mode they map as:

    UP    -> InputAction.PICKER      (open Show picker)
    DOWN  -> InputAction.SETTINGS    (open per-Show settings)
    RIGHT -> InputAction.CYCLE       (reaches the Show)
    LEFT  -> InputAction.CYCLE_PREV  (reaches the Show)

CONFIRM is reserved as the manual-tap button (drives the
ButtonTapSource, never routed here) and CANCEL is the app's
exit-Director gesture (handled in app.py), so neither is mapped to an
InputAction.

This mapper does its own rising-edge detection from the polled
pressed-state, so the app does NOT need the badge's button_states
`.clear()` (which would also wipe the held CONFIRM tap state). Pure
logic, host-testable; the app reads `button_states.get(...)` and feeds
the bools in.
"""

from ..shows import InputAction


# Stable poll order so simultaneous presses resolve deterministically
# (picker before settings before cycle): the order rarely matters in
# practice but keeps tests predictable.
_NAV = ("up", "down", "right", "left")
_ACTION_FOR = {
    "up": InputAction.PICKER,
    "down": InputAction.SETTINGS,
    "right": InputAction.CYCLE,
    "left": InputAction.CYCLE_PREV,
}


class DirectorButtonMapper:
    """Rising-edge detector mapping nav buttons to InputActions."""

    __slots__ = ("_prev",)

    def __init__(self):
        self._prev = {name: False for name in _NAV}

    def reset(self):
        """Forget edge state. Call on Director-mode entry so a button
        held at entry doesn't fire on the first poll."""
        for name in _NAV:
            self._prev[name] = True  # treat as already-down: needs a release first

    def poll(self, up=False, down=False, left=False, right=False):
        """Feed the current pressed-state of each nav button. Returns a
        list of InputActions that went down (rising edge) this poll, in
        the stable nav order."""
        current = {"up": up, "down": down, "left": left, "right": right}
        fired = []
        for name in _NAV:
            now = bool(current[name])
            if now and not self._prev[name]:
                fired.append(_ACTION_FOR[name])
            self._prev[name] = now
        return fired
