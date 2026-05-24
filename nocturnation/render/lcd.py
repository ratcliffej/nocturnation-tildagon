"""LCD pulse renderer for the Tildagon's round 240x240 screen.

Renders an accepted LIGHT_COMMAND envelope as a soft full-screen colour
wash that the app's draw() blends underneath its UI text. Pure logic
with a query interface (current_colour returns the colour to use right
now, or None if no wash should be drawn) so the whole contract is host-
testable; the actual rectangle/fill happens in app.py via ctx.

Safety:
  - Calm Mode (default) disables LCD pulsing entirely per architecture
    spec section 15.3 ("screen flashing disabled"). current_colour
    returns None in Calm Mode and the caller falls back to the static
    UI background (black).
  - Full mode arms an envelope on each accepted dispatch with peak
    brightness capped at FULL_BRIGHTNESS_CAP for eye comfort. A
    full-screen 100 percent wash would be uncomfortably bright at the
    badge's face-distance.
  - Frequency cap matches the perimeter renderer's Full mode (4 Hz, per
    architecture spec section 15.1).

Reference manuals:
  Protocol manual section 3.3.4 (LIGHT_COMMAND fields)
  Architecture spec section 15 (photosensitivity / Calm Mode)
  Epic 5 Block 4
"""

from .envelope import TIME_MS, envelope_brightness


# Peak brightness multiplier in Full mode. A full-screen face-distance
# wash at 100 percent is uncomfortably bright; 60 percent is the upper
# bound that bench-tested as comfortable while still visible.
FULL_BRIGHTNESS_CAP = 0.6

# Frequency cap: minimum milliseconds between accepted dispatches when
# the renderer is enabled (Full mode). Matches the perimeter renderer's
# Full-mode interval so both surfaces respect the same 4 Hz upper bound.
LCD_MIN_INTERVAL_MS = 250


class LcdRenderer:
    """Render LIGHT_COMMAND envelopes as a full-screen colour wash.

    Two-phase like the perimeter renderer:
      dispatch(frame, now_ms)   - on every received LIGHT_COMMAND.
      current_colour(now_ms)    - on every draw() call.

    Calm Mode (default) makes current_colour always return None so the
    LCD stays with its static UI. Operator toggles via set_calm_mode().
    """

    __slots__ = (
        "_enabled",
        "_last_dispatch_ms",
        "_envelope",
    )

    def __init__(self, calm_mode=True):
        # Calm Mode disables LCD pulsing entirely; the renderer is
        # effectively a no-op until the operator opts into Full mode.
        self._enabled = not bool(calm_mode)
        # Initial last-dispatch is set so the first dispatch in Full
        # mode is always accepted regardless of when it arrives.
        self._last_dispatch_ms = -LCD_MIN_INTERVAL_MS - 1
        self._envelope = None  # (start_ms, r, g, b, atk, sus, rel, total) or None

    @property
    def enabled(self):
        return self._enabled

    def set_calm_mode(self, on):
        on = bool(on)
        self._enabled = not on
        if on:
            # Drop any in-flight wash so the LCD darkens cleanly when
            # the operator switches back to Calm Mode mid-render.
            self._envelope = None

    def dispatch(self, frame, now_ms):
        """Arm a full-screen envelope from a LIGHT_COMMAND.

        Returns True if the dispatch was accepted (envelope armed),
        False otherwise. Drop reasons (all silent):
          - Calm Mode (renderer disabled)
          - Rate-limited (< LCD_MIN_INTERVAL_MS since last accepted)
          - Primer frame (rgb = 0,0,0); a black wash would be invisible
            anyway, and primers don't consume the rate-limit budget so
            the main fire that follows is allowed.
          - Zero-duration envelope (attack + sustain + release == 0).
        """
        if not self._enabled:
            return False
        if frame.r == 0 and frame.g == 0 and frame.b == 0:
            return False
        if now_ms - self._last_dispatch_ms < LCD_MIN_INTERVAL_MS:
            return False
        attack_ms = TIME_MS[frame.attack]
        sustain_ms = TIME_MS[frame.sustain]
        release_ms = TIME_MS[frame.release]
        total = attack_ms + sustain_ms + release_ms
        if total == 0:
            return False
        self._envelope = (
            now_ms,
            frame.r,
            frame.g,
            frame.b,
            attack_ms,
            sustain_ms,
            release_ms,
            total,
        )
        self._last_dispatch_ms = now_ms
        return True

    def current_colour(self, now_ms):
        """Return (r, g, b) in 0..255 for the screen at now_ms, or None.

        None means "no wash; use static background". Callers in Calm
        Mode always see None.
        """
        if not self._enabled or self._envelope is None:
            return None
        start_ms, r, g, b, atk, sus, rel, total = self._envelope
        elapsed = now_ms - start_ms
        if elapsed < 0:
            return None
        if elapsed >= total:
            self._envelope = None
            return None
        level = envelope_brightness(elapsed, atk, sus, rel) * FULL_BRIGHTNESS_CAP
        return (int(r * level), int(g * level), int(b * level))

    def clear(self):
        """Drop any in-flight envelope. Used on foreground transitions
        so a stale envelope from before a minimise doesn't bleed
        through on re-entry."""
        self._envelope = None
