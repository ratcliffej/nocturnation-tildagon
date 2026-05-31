"""LCD pulse + wash renderer for the Tildagon's round 240x240 screen.

Renders LIGHT_PULSE envelopes as a soft full-screen colour wash that
the app's draw() blends underneath its UI text. Epic 6C Phase G adds
LIGHT_WASH support: a persistent breathing-palette baseline with
optional pulse overlay. Pure logic with a query interface
(current_colour returns the colour to use right now, or None if no
wash should be drawn) so the whole contract is host-testable; the
actual rectangle/fill happens in app.py via ctx.

Safety:
  - Calm Mode (default) disables LCD pulsing AND wash entirely per
    architecture spec section 15.3 ("screen flashing disabled").
    current_colour returns None in Calm Mode and the caller falls back
    to the static UI background (black).
  - Full mode arms an envelope on each accepted dispatch with peak
    brightness capped at FULL_BRIGHTNESS_CAP for eye comfort. A
    full-screen 100 percent wash would be uncomfortably bright at the
    badge's face-distance.
  - Frequency cap matches the perimeter renderer's Full mode (4 Hz, per
    architecture spec section 15.1) - PULSE only; WASH is a state
    transition, not a periodic flash, so it isn't rate-limited.

Reference manuals:
  Protocol manual section 3.3.2/3/4/5 (LIGHT_PULSE + LIGHT_WASH family)
  Architecture spec section 15 (photosensitivity / Calm Mode)
  lume-capabilities-design.md (wash semantics)
  Epic 5 Block 4, Epic 6C Phase G
"""

import math

from .envelope import TIME_MS, envelope_brightness  # noqa: F401 (re-exported)


# Peak brightness multiplier in Full mode. A full-screen face-distance
# wash at 100 percent is uncomfortably bright; 60 percent is the upper
# bound that bench-tested as comfortable while still visible.
FULL_BRIGHTNESS_CAP = 0.6

# Frequency cap: minimum milliseconds between accepted PULSE dispatches
# when the renderer is enabled (Full mode). Matches the perimeter
# renderer's Full-mode interval so both surfaces respect the same 4 Hz
# upper bound.
LCD_MIN_INTERVAL_MS = 250


_WASH_INACTIVE = 0
_WASH_ATTACK   = 1
_WASH_HOLD     = 2
_WASH_RELEASE  = 3


def _clip(v):
    if v < 0: return 0
    if v > 255: return 255
    return int(v)


def _lerp(a, b, t):
    if t <= 0.0: return int(a)
    if t >= 1.0: return int(b)
    return int(a + (b - a) * t)


class LcdRenderer:
    """Render LIGHT_PULSE envelopes + LIGHT_WASH baseline as a full-
    screen colour wash.

    Two-phase like the perimeter renderer:
      dispatch(frame, now_ms)            - LIGHT_PULSE.
      on_light_wash(frame, now_ms)       - LIGHT_WASH.
      on_light_wash_end(frame, now_ms)   - LIGHT_WASH_END.
      on_light_wash_pulse(frame, now_ms) - LIGHT_WASH_PULSE.
      current_colour(now_ms)             - on every draw() call.

    Calm Mode (default) makes current_colour always return None so the
    LCD stays with its static UI. Operator toggles via set_calm_mode().
    """

    __slots__ = (
        "_enabled",
        "_last_dispatch_ms",
        "_pulse",       # (start_ms, src_r, src_g, src_b, dst_r, dst_g, dst_b, atk, sus, rel, total)
        "_wash",
        "_last_rendered",  # (r, g, b) last output, for next pulse's src-lerp
    )

    def __init__(self, calm_mode=True):
        # Calm Mode disables LCD pulsing AND wash entirely; the renderer
        # is effectively a no-op until the operator opts into Full mode.
        self._enabled = not bool(calm_mode)
        self._last_dispatch_ms = -LCD_MIN_INTERVAL_MS - 1
        self._pulse = None
        self._wash = None
        self._last_rendered = (0, 0, 0)

    @property
    def enabled(self):
        return self._enabled

    def set_calm_mode(self, on):
        on = bool(on)
        self._enabled = not on
        if on:
            # Drop any in-flight wash / pulse so the LCD darkens cleanly
            # when the operator switches back to Calm Mode mid-render.
            self._pulse = None
            self._wash = None
            self._last_rendered = (0, 0, 0)

    # ------------------------------------------------------------------
    # Wash baseline (Epic 6C Phase G)
    # ------------------------------------------------------------------

    def _wash_baseline_at(self, now_ms):
        w = self._wash
        if w is None or w["phase"] == _WASH_INACTIVE:
            return (0, 0, 0)
        if w["cycle_ms"] != 0:
            ph = 2.0 * math.pi * (now_ms - w["started_ms"]) / w["cycle_ms"]
            t = 0.5 - 0.5 * math.cos(ph)
            base_r = _lerp(w["r1"], w["r2"], t)
            base_g = _lerp(w["g1"], w["g2"], t)
            base_b = _lerp(w["b1"], w["b2"], t)
        else:
            base_r, base_g, base_b = w["r1"], w["g1"], w["b1"]
        scale = (w["intensity"] / 255.0) * FULL_BRIGHTNESS_CAP
        post = (_clip(base_r * scale), _clip(base_g * scale), _clip(base_b * scale))
        if w["phase"] == _WASH_HOLD:
            return post
        elapsed = now_ms - w["phase_started_ms"]
        if w["phase"] == _WASH_ATTACK:
            atk_ms = w["attack_units"] * 100
            if atk_ms == 0 or elapsed >= atk_ms:
                return post
            t = elapsed / atk_ms
            return (
                _lerp(w["pre_wash_r"], post[0], t),
                _lerp(w["pre_wash_g"], post[1], t),
                _lerp(w["pre_wash_b"], post[2], t),
            )
        rel_ms = w["release_units_active"] * 100
        if w["phase"] == _WASH_RELEASE:
            if rel_ms == 0 or elapsed >= rel_ms:
                return (w["release_end_r"], w["release_end_g"], w["release_end_b"])
            t = elapsed / rel_ms
            return (
                _lerp(w["pre_wash_r"], w["release_end_r"], t),
                _lerp(w["pre_wash_g"], w["release_end_g"], t),
                _lerp(w["pre_wash_b"], w["release_end_b"], t),
            )
        return (0, 0, 0)

    def on_light_wash(self, frame, now_ms):
        if not self._enabled:
            return
        pre = self._wash_baseline_at(now_ms) if self._wash is not None else (0, 0, 0)
        self._wash = {
            "phase":               _WASH_ATTACK,
            "started_ms":          now_ms,
            "phase_started_ms":    now_ms,
            "r1": frame.r1, "g1": frame.g1, "b1": frame.b1,
            "r2": frame.r2, "g2": frame.g2, "b2": frame.b2,
            "attack_units":        frame.wash_attack,
            "release_units":       frame.wash_release,
            "intensity":           frame.intensity,
            "cycle_ms":            frame.cycle_ms,
            "ttl_seconds":         frame.ttl_seconds,
            "pulse_response":      frame.pulse_response,
            "pre_wash_r":          pre[0],
            "pre_wash_g":          pre[1],
            "pre_wash_b":          pre[2],
            "release_units_active": frame.wash_release,
            "release_end_r":       0,
            "release_end_g":       0,
            "release_end_b":       0,
        }

    def on_light_wash_end(self, frame, now_ms):
        if self._wash is None or self._wash["phase"] == _WASH_INACTIVE:
            return
        cur = self._wash_baseline_at(now_ms)
        self._wash["pre_wash_r"]            = cur[0]
        self._wash["pre_wash_g"]            = cur[1]
        self._wash["pre_wash_b"]            = cur[2]
        self._wash["release_units_active"]  = frame.release_time
        self._wash["release_end_r"]         = 0
        self._wash["release_end_g"]         = 0
        self._wash["release_end_b"]         = 0
        self._wash["phase"]                 = _WASH_RELEASE
        self._wash["phase_started_ms"]      = now_ms

    def is_washing(self):
        if self._wash is None:
            return False
        return self._wash["phase"] in (_WASH_ATTACK, _WASH_HOLD)

    # ------------------------------------------------------------------
    # Pulse dispatch
    # ------------------------------------------------------------------

    def dispatch(self, frame, now_ms):
        """Arm a full-screen envelope from a LIGHT_PULSE.

        Returns True if the dispatch was accepted (envelope armed),
        False otherwise. Drop reasons (all silent):
          - Calm Mode (renderer disabled)
          - Wash active with pulse_response = 0
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
        if self._wash is not None and self._wash["phase"] in (_WASH_ATTACK, _WASH_HOLD):
            if self._wash["pulse_response"] == 0:
                return False
        attack_ms = TIME_MS[frame.attack]
        sustain_ms = TIME_MS[frame.sustain]
        release_ms = TIME_MS[frame.release]
        total = attack_ms + sustain_ms + release_ms
        if total == 0:
            return False
        src_r, src_g, src_b = self._last_rendered
        self._pulse = (
            now_ms,
            src_r, src_g, src_b,
            _clip(frame.r * FULL_BRIGHTNESS_CAP),
            _clip(frame.g * FULL_BRIGHTNESS_CAP),
            _clip(frame.b * FULL_BRIGHTNESS_CAP),
            attack_ms, sustain_ms, release_ms, total,
        )
        self._last_dispatch_ms = now_ms
        return True

    def on_light_wash_pulse(self, frame, now_ms):
        if not self.is_washing():
            return False
        # Force-accept the overlay regardless of pulse_response.
        original = self._wash["pulse_response"]
        self._wash["pulse_response"] = 1
        try:
            return self.dispatch(frame, now_ms)
        finally:
            self._wash["pulse_response"] = original

    # ------------------------------------------------------------------
    # Per-tick render (query - app.py calls this from its draw())
    # ------------------------------------------------------------------

    def current_colour(self, now_ms):
        """Return (r, g, b) in 0..255 for the screen at now_ms, or None.

        None means "no wash; use static background". Callers in Calm
        Mode always see None.
        """
        if not self._enabled:
            return None
        # Advance wash phase machine.
        if self._wash is not None and self._wash["phase"] != _WASH_INACTIVE:
            self._advance_wash_phase(now_ms)
        # Compute baseline.
        base = self._wash_baseline_at(now_ms)
        r, g, b = base
        # Overlay any active pulse.
        env = self._pulse
        if env is not None:
            start_ms, src_r, src_g, src_b, dst_r, dst_g, dst_b, atk, sus, rel, total = env
            elapsed = now_ms - start_ms
            if elapsed < 0:
                pass
            elif elapsed >= total:
                self._pulse = None
            else:
                if elapsed < atk:
                    if atk == 0:
                        r, g, b = dst_r, dst_g, dst_b
                    else:
                        t = elapsed / atk
                        r = _lerp(src_r, dst_r, t)
                        g = _lerp(src_g, dst_g, t)
                        b = _lerp(src_b, dst_b, t)
                elif elapsed < atk + sus:
                    r, g, b = dst_r, dst_g, dst_b
                else:
                    rel_elapsed = elapsed - atk - sus
                    if rel == 0:
                        r, g, b = base
                    else:
                        t = rel_elapsed / rel
                        r = _lerp(dst_r, base[0], t)
                        g = _lerp(dst_g, base[1], t)
                        b = _lerp(dst_b, base[2], t)

        self._last_rendered = (r, g, b)
        # Return None when there's nothing to paint (no wash, no live pulse).
        if r == 0 and g == 0 and b == 0 and self._wash is None and self._pulse is None:
            return None
        return (r, g, b)

    def _advance_wash_phase(self, now_ms):
        w = self._wash
        phase = w["phase"]
        if phase == _WASH_ATTACK:
            atk_ms = w["attack_units"] * 100
            if atk_ms == 0 or (now_ms - w["phase_started_ms"]) >= atk_ms:
                w["phase"] = _WASH_HOLD
                w["phase_started_ms"] = now_ms
        elif phase == _WASH_HOLD:
            if w["ttl_seconds"] != 0:
                ttl_ms = w["ttl_seconds"] * 1000
                if (now_ms - w["started_ms"]) >= ttl_ms:
                    cur = self._wash_baseline_at(now_ms)
                    w["pre_wash_r"]            = cur[0]
                    w["pre_wash_g"]            = cur[1]
                    w["pre_wash_b"]            = cur[2]
                    w["release_units_active"]  = w["release_units"]
                    w["release_end_r"]         = 0
                    w["release_end_g"]         = 0
                    w["release_end_b"]         = 0
                    w["phase"]                 = _WASH_RELEASE
                    w["phase_started_ms"]      = now_ms
        elif phase == _WASH_RELEASE:
            rel_ms = w["release_units_active"] * 100
            if rel_ms == 0 or (now_ms - w["phase_started_ms"]) >= rel_ms:
                self._wash = None

    def clear(self):
        """Drop any in-flight envelope + wash. Used on foreground
        transitions so stale state from before a minimise doesn't bleed
        through on re-entry."""
        self._pulse = None
        self._wash = None
        self._last_rendered = (0, 0, 0)
