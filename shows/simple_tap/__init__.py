"""simple_tap - the reference NocturNation Tildagon Show.

Tap the badge (or press C) in time with the music; each tap fires a
coloured pulse to the bracelets + the Director's own perimeter ring,
and flashes the screen. Cycle the palette with the Cycle button.

This is the worked example for show authors: it exercises the whole
B2-B6 surface - id/display_name, a property schema (palette +
sensitivity), the tap hook, render_fx output, input handling, and
on_render drawing via the ShowContext display surface.

See docs/developing-shows.md in the nocturnation-m5 repo for the
authoring guide.
"""

from nocturnation.shows import Show, InputAction
from nocturnation.hal import Capability, CapabilityMask
from nocturnation.plugins import PluginKind, PowerProfile, PropertyDef, PropertyType
from nocturnation.render import RgbPulse
from nocturnation.protocol.constants import Time, Chance


# Palette names (the "palette" enum). Index 3 (Rainbow) cycles hue per
# tap; the rest are fixed colours.
_PALETTE_NAMES = ("Cool", "Natural", "Warm", "Rainbow", "Red", "Green", "Blue")
_RAINBOW = 3

# Fixed palette RGB (0..255). Rainbow is computed per tap, so it has no
# entry here.
_FIXED_RGB = {
    0: (0, 180, 255),     # Cool   - cyan-blue
    1: (255, 200, 150),   # Natural - warm white
    2: (255, 120, 0),     # Warm   - amber
    4: (255, 40, 40),     # Red
    5: (40, 255, 80),     # Green
    6: (40, 90, 255),     # Blue
}

# Screen flash decays over this window after a tap (ms).
_FLASH_MS = 200


def _hsv_to_rgb(h, s, v):
    """h in 0..360, s/v in 0..1 -> (r, g, b) 0..255."""
    c = v * s
    hp = (h % 360) / 60.0
    x = c * (1 - abs(hp % 2 - 1))
    if hp < 1:
        r, g, b = c, x, 0
    elif hp < 2:
        r, g, b = x, c, 0
    elif hp < 3:
        r, g, b = 0, c, x
    elif hp < 4:
        r, g, b = 0, x, c
    elif hp < 5:
        r, g, b = x, 0, c
    else:
        r, g, b = c, 0, x
    m = v - c
    return (int((r + m) * 255), int((g + m) * 255), int((b + m) * 255))


class SimpleTap(Show):
    def __init__(self):
        self._hue = 0          # advances on each Rainbow-palette tap
        self._last_rgb = (0, 0, 0)
        self._last_tap_ms = None

    # -- Identity ---------------------------------------------------------

    def id(self):
        return "simple_tap"

    def display_name(self):
        return "Simple Tap"

    def kind(self):
        return PluginKind.SHOW

    def required_capabilities(self):
        # Draws to the screen and broadcasts. Tap input comes from the
        # IMU *or* the button fallback, so IMU_TAP is preferred but not
        # required - a no-IMU badge still works via button C.
        return CapabilityMask(Capability.DISPLAY, Capability.ESP_NOW)

    def properties(self):
        return _PROPS

    def power(self):
        # Event-driven (tap-fired); no periodic tick needed.
        return PowerProfile(needs_audio_frames=False, tick_hz=0)

    # -- Input ------------------------------------------------------------

    def on_tap_detected(self, ctx, strength):
        palette = ctx.get_property("palette")
        r, g, b = self._colour_for(palette)
        self._last_rgb = (r, g, b)
        self._last_tap_ms = ctx.now_ms()
        # Attack time picks: fixed palettes keep a near-instant T_32_MS
        # attack so each tap feels punchy. The Rainbow palette uses
        # T_192_MS so successive taps crossfade smoothly through the
        # hue wheel (per Epic 6C Phase G - the perimeter / LCD
        # renderers now lerp from the LED's current colour during the
        # attack phase, so a non-zero attack on a fast hue cycle
        # actually blends instead of snapping through black).
        attack = Time.T_192_MS if palette == _RAINBOW else Time.T_32_MS
        # Light class, group 0 = every Light-class Lume regardless of its
        # group setting. A basic tap show shouldn't require operators to
        # configure a matching group on each badge; group targeting is
        # for shows that deliberately address sub-zones.
        ctx.render_fx("01:00", RgbPulse(
            r, g, b,
            attack=attack,
            sustain=Time.T_96_MS,
            release=Time.T_480_MS,
            chance=Chance.CHANCE_100,
        ))

    def on_input_action(self, ctx, action):
        if action == InputAction.CYCLE:
            self._advance_palette(ctx, +1)
        elif action == InputAction.CYCLE_PREV:
            self._advance_palette(ctx, -1)
        elif action == InputAction.CONFIRM:
            # Confirm triggers a synthetic tap (an alternate manual-tap
            # path; mainly relevant on hosts that route Confirm to the
            # Show, e.g. M5).
            self.on_tap_detected(ctx, 192)

    # -- Render -----------------------------------------------------------

    def on_render(self, ctx):
        d = ctx.display()
        if d is None:
            return
        palette = ctx.get_property("palette")

        # Background flashes the tap colour briefly after each tap, then
        # settles to black.
        bg = self._flash_background(ctx)
        d.clear(*bg)

        d.text(0, -60, "Simple Tap", size=22, r=255, g=255, b=255)
        d.text(0, -10, _PALETTE_NAMES[palette] if 0 <= palette < len(_PALETTE_NAMES)
               else "?", size=28,
               r=self._last_rgb[0], g=self._last_rgb[1], b=self._last_rgb[2])
        d.text(0, 40, "tap to beat", size=14, r=180, g=180, b=180)
        d.text(0, 95, "< >: colour   F: exit", size=10, r=120, g=120, b=120)

    # -- Internals --------------------------------------------------------

    def _advance_palette(self, ctx, delta):
        cur = ctx.get_property("palette")
        nxt = (cur + delta) % len(_PALETTE_NAMES)
        ctx.set_property("palette", nxt)

    def _colour_for(self, palette):
        if palette == _RAINBOW:
            rgb = _hsv_to_rgb(self._hue, 1.0, 1.0)
            self._hue = (self._hue + 40) % 360
            return rgb
        return _FIXED_RGB.get(palette, (255, 255, 255))

    def _flash_background(self, ctx):
        if self._last_tap_ms is None:
            return (0, 0, 0)
        elapsed = ctx.now_ms() - self._last_tap_ms
        if elapsed < 0 or elapsed >= _FLASH_MS:
            return (0, 0, 0)
        # Linear decay from ~30% of the tap colour to black.
        scale = (1.0 - elapsed / _FLASH_MS) * 0.3
        r, g, b = self._last_rgb
        return (int(r * scale), int(g * scale), int(b * scale))


_PROPS = (
    PropertyDef(
        key="palette",
        type=PropertyType.ENUM,
        default_value=3,  # Rainbow
        min_value=0,
        max_value=len(_PALETTE_NAMES) - 1,
        display_name="Palette",
        enum_names=_PALETTE_NAMES,
    ),
    PropertyDef(
        key="sensitivity",
        type=PropertyType.ENUM,
        default_value=2,  # High - a hand-held badge tap is gentle (B9 bench)
        min_value=0,
        max_value=2,
        display_name="Sensitivity",
        enum_names=("Low", "Medium", "High"),
    ),
)


def make_show():
    """Factory the registry calls on discovery."""
    return SimpleTap()
