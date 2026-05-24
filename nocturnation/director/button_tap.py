"""Button-as-tap fallback for the Director.

A development aid: when the IMU isn't tuned (or you want deterministic
beats while writing a Show), a button press stands in for a badge tap.
Emits the same `on_tap(strength)` callback the ImuAdapter does, so the
Director host routes it identically (to both `on_tap_detected` and
`on_beat_detected` on the active Show - wired in B6).

Pure logic, like the IMU adapter: `poll(pressed, now_ms)` takes the
current button-pressed bool and the clock; the caller reads the
physical button (B6 reads the badge button in background_task) and
feeds the state in. Two modes:

  - **edge-only** (default, `repeat_ms=0`): one tap per press. Tap the
    button in time with the music, exactly as you'd tap the badge.
  - **auto-repeat** (`repeat_ms > 0`): while the button is held, fire
    a tap every `repeat_ms`. A held-button metronome for sound-check.

Strength is fixed (default 192 - a firm tap, ~75 %) since a button
has no force information.
"""


def _clamp_u8(v):
    if v < 0:
        return 0
    if v > 255:
        return 255
    return int(v)


# Default synthetic-tap strength. A button can't measure force, so we
# pick a firm-but-not-max value: clearly visible without saturating.
DEFAULT_TAP_STRENGTH = 192


class ButtonTapSource:
    """Turn polled button state into synthetic tap events.

    Construct with:
      on_tap    - callable(strength), strength 0..255.
      strength  - fixed strength for every synthetic tap.
      repeat_ms - 0 for edge-only (one tap per press); > 0 to auto-
                  repeat while held at that interval.

    Drive with `poll(pressed, now_ms)` each app tick.
    """

    __slots__ = ("_on_tap", "_strength", "_repeat_ms", "_was_pressed", "_last_tap_ms")

    def __init__(self, on_tap=None, strength=DEFAULT_TAP_STRENGTH, repeat_ms=0):
        self._on_tap = on_tap
        self._strength = _clamp_u8(strength)
        self._repeat_ms = repeat_ms
        self._was_pressed = False
        self._last_tap_ms = None

    def set_callback(self, on_tap):
        """Re-point the tap callback (e.g. when the active Show changes)."""
        self._on_tap = on_tap

    def reset(self):
        """Forget press / timing state. Use on Director-mode entry so a
        button already down at entry doesn't fire a spurious tap."""
        self._was_pressed = False
        self._last_tap_ms = None

    def poll(self, pressed, now_ms):
        """Feed the current button-pressed state. Returns True if a
        synthetic tap fired this poll.

        Rising edge (not-pressed -> pressed) always fires. While held,
        an additional tap fires every `repeat_ms` if auto-repeat is on.
        Release fires nothing.
        """
        fired = False
        if pressed and not self._was_pressed:
            # Rising edge: one tap.
            self._fire(now_ms)
            fired = True
        elif pressed and self._repeat_ms > 0:
            # Held: auto-repeat at the configured interval.
            if self._last_tap_ms is None or (now_ms - self._last_tap_ms) >= self._repeat_ms:
                self._fire(now_ms)
                fired = True
        self._was_pressed = pressed
        return fired

    def _fire(self, now_ms):
        self._last_tap_ms = now_ms
        if self._on_tap is not None:
            self._on_tap(self._strength)
