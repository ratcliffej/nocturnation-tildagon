"""DirectorButtonMapper - edge-triggered nav buttons -> InputAction.

The Tildagon has six buttons (UP / DOWN / LEFT / RIGHT / CONFIRM /
CANCEL) with only instantaneous pressed state; this mapper derives
rising-edge and hold-duration events from the polled pressed-state.
Director-mode mapping:

    UP    -> InputAction.PICKER                       (rising edge)
    DOWN  -> InputAction.SETTINGS                     (rising edge)
    RIGHT -> InputAction.CYCLE            (release, short) OR
             InputAction.SECTION_NEXT     (threshold, long)
    LEFT  -> InputAction.CYCLE_PREV       (release, short) OR
             InputAction.SECTION_PREV     (threshold, long)

CONFIRM is reserved as the manual-tap button (drives the
ButtonTapSource, never routed here) and CANCEL is the app's
exit-Director gesture (handled in app.py), so neither is mapped to an
InputAction.

Long-press semantics (v1.0.1):
LEFT and RIGHT distinguish a short press (< 500 ms) from a long press
(>= 500 ms) so the palette-cycle short-press can coexist with a
section-cycle long-press without a third button. Short-press fires
on RELEASE (falling edge). Long-press fires at the 500 ms threshold
while still held, and suppresses the short-press release-fire. UP and
DOWN stay rising-edge because their overlay actions have no long-press
sibling.

The mapper does its own edge / hold detection from the polled pressed-
state, so the app does NOT need the badge's `button_states.clear()`
(which would also wipe the held CONFIRM tap state). Pure logic, host-
testable; the app reads `button_states.get(...)` and feeds the bools
in alongside a `now_ms` timestamp.
"""

from ..shows import InputAction


# Long-press threshold. 500 ms is comfortable: shorter than a normal
# hold-to-scroll (which the mapper doesn't support anyway), longer than
# any deliberate button-mash from a Show operator.
LONG_PRESS_MS = 500

# Stable poll order so simultaneous presses resolve deterministically
# (picker before settings before cycle): the order rarely matters in
# practice but keeps tests predictable.
_NAV = ("up", "down", "right", "left")

# Instant-fire mapping (rising edge). UP and DOWN go here because
# their overlay actions have no long-press sibling.
_INSTANT_ACTION_FOR = {
    "up": InputAction.PICKER,
    "down": InputAction.SETTINGS,
}

# Short-press (release-fire) mapping. LEFT / RIGHT fire on release
# only if the long-press threshold was not reached during the hold.
_SHORT_ACTION_FOR = {
    "right": InputAction.CYCLE,
    "left":  InputAction.CYCLE_PREV,
}

# Long-press (threshold-fire) mapping. Fires once, when the button has
# been held for LONG_PRESS_MS; suppresses the eventual release-fire.
_LONG_ACTION_FOR = {
    "right": InputAction.SECTION_NEXT,
    "left":  InputAction.SECTION_PREV,
}


class DirectorButtonMapper:
    """Rising-edge + hold-duration detector mapping nav buttons to
    InputActions."""

    __slots__ = ("_prev", "_press_start_ms", "_long_fired")

    def __init__(self):
        self._prev = {name: False for name in _NAV}
        # Per-button millisecond timestamp of the last rising edge, or
        # None when the button isn't currently held. Only used for
        # LEFT / RIGHT (long-press capable); UP / DOWN entries stay None.
        self._press_start_ms = {name: None for name in _NAV}
        # Per-button flag: True once the long-press action fired for the
        # current hold. Blocks the release-side short-press fire.
        self._long_fired = {name: False for name in _NAV}

    def reset(self):
        """Forget edge state. Call on Director-mode entry so a button
        held at entry doesn't fire on the first poll."""
        for name in _NAV:
            self._prev[name] = True  # treat as already-down: needs a release first
            self._press_start_ms[name] = None
            # LEFT / RIGHT fire on the falling edge, so the phantom
            # "release" from the reset-held state would otherwise fire
            # CYCLE / CYCLE_PREV on the first poll. Marking long_fired
            # suppresses that release fire; the falling-edge branch
            # clears the flag, so a legit subsequent press cycles
            # normally.
            self._long_fired[name] = name in _LONG_ACTION_FOR

    def poll(self, up=False, down=False, left=False, right=False, now_ms=0):
        """Feed the current pressed-state of each nav button + a wall-
        clock millisecond timestamp for the long-press timer. Returns a
        list of InputActions produced this poll, in the stable nav order.

        `now_ms` defaults to 0 so callers that don't care about long-press
        (host tests exercising instant-fire and short-press only) can
        omit it; long-press detection needs monotonic timestamps to work.
        """
        current = {"up": up, "down": down, "left": left, "right": right}
        fired = []
        for name in _NAV:
            now = bool(current[name])
            prev = self._prev[name]

            if now and not prev:
                # Rising edge. UP / DOWN fire instantly; LEFT / RIGHT
                # start their long-press timer and defer to release.
                if name in _INSTANT_ACTION_FOR:
                    fired.append(_INSTANT_ACTION_FOR[name])
                else:
                    self._press_start_ms[name] = now_ms
                    self._long_fired[name] = False
            elif now and prev:
                # Held. Only LEFT / RIGHT check the long-press threshold;
                # UP / DOWN already fired on the rising edge.
                if name in _LONG_ACTION_FOR and not self._long_fired[name]:
                    start = self._press_start_ms[name]
                    if start is not None and (now_ms - start) >= LONG_PRESS_MS:
                        fired.append(_LONG_ACTION_FOR[name])
                        self._long_fired[name] = True
            elif not now and prev:
                # Falling edge. LEFT / RIGHT fire their short-press action
                # UNLESS the long-press already fired during the hold.
                # UP / DOWN have nothing to do on release.
                if name in _SHORT_ACTION_FOR and not self._long_fired[name]:
                    fired.append(_SHORT_ACTION_FOR[name])
                self._press_start_ms[name] = None
                self._long_fired[name] = False

            self._prev[name] = now
        return fired
