"""Conductor - the reference operator-driven Show for Epic 6D (B3).

Cross-platform sibling of `bass-drift` on the StickC. Implements the
architecture spec section 1.2 lighting language adapted for a host that
has no microphone: a continuous wash baseline holds the song-arc layer
(palette per section), operator taps drive the per-beat punctuation
gated by `chance`, and section transitions are picked manually via the
Settings overlay (no analyser to fire them automatically).

v1 shipped palette-cycle as the only Show-reachable button input.
v1.0.1 adds section-cycle on long-press LEFT / RIGHT so the operator
can drive the arc of a song without opening the Settings overlay.
Richer scene gestures (drop release / freeze / brighten / dim) are
still deferred to v2 pending the EMF 2026 keyboard hexagon.

The wash visualisation lives on the Tildagon's own perimeter ring + LCD
(B1's Director-side loopback) and on every other Lume on the channel.
"""

from nocturnation.shows import Show, InputAction
from nocturnation.hal import Capability, CapabilityMask
from nocturnation.plugins import PluginKind, PowerProfile, PropertyDef, PropertyType
from nocturnation.render import RgbPulse, RgbWash
from nocturnation.protocol.constants import Time, Chance


# Palette set names + indices. Same indices as BassAndDriftShow on the
# StickC so a mixed Director-Lume fleet stays visually consistent.
_PALETTE_NAMES = ("Warm", "Cool", "Vivid", "Mono")

# Section names + indices. Manually selected via the Settings overlay.
_SECTION_NAMES = ("Verse", "Chorus", "BuildUp", "Drop", "Breakdown")
_SECTION_IDX_DEFAULT = 0   # Verse on entry

# Chance enum names. Maps directly to the 8 protocol Chance steps in
# descending percentage order. Default index 4 = CHANCE_32 (~25 %),
# matching the section 1.2 "fire on selected beats" guidance.
_CHANCE_NAMES = ("100%", "88%", "67%", "50%", "32%", "16%", "10%", "4%")
_CHANCE_TABLE = (
    Chance.CHANCE_100,
    Chance.CHANCE_88,
    Chance.CHANCE_67,
    Chance.CHANCE_50,
    Chance.CHANCE_32,
    Chance.CHANCE_16,
    Chance.CHANCE_10,
    Chance.CHANCE_4,
)
_CHANCE_DEFAULT_IDX = 4

# Wash speed enum (no Beat-locked options - no analyser, no BPM).
_WASH_SPEED_NAMES = ("Glacial", "Slow", "Medium", "Fast", "Snappy")
_WASH_SPEED_CYCLE_MS = (12000, 8000, 5000, 3000, 2000)
_WASH_SPEED_DEFAULT_IDX = 2   # Medium

# Palette table: [palette_set][section_idx] -> (r1, g1, b1, r2, g2, b2,
# intensity, cycle_ms). Values match the StickC palettes for a coherent
# look on mixed fleets.
_PALETTES = (
    # Warm
    (
        (120,  30,   0,   60,  10,   0, 140, 8000),    # Verse
        (255, 140,  30,  255,  60,  80, 200, 5000),    # Chorus
        (255, 100,   0,  255, 200,   0, 180, 3000),    # BuildUp
        (255, 220, 120,  255,  80,   0, 255, 4000),    # Drop
        ( 40,  10,   0,    5,   0,   0,  80, 12000),   # Breakdown
    ),
    # Cool
    (
        (  0,  60, 100,    0,  20,  60, 140, 8000),
        (  0, 220, 255,  200,   0, 220, 200, 5000),
        (  0, 180, 255,  100, 100, 255, 180, 3000),
        (220, 240, 255,  255,   0, 200, 255, 4000),
        (  0,  10,  40,    0,   0,  10,  80, 12000),
    ),
    # Vivid
    (
        ( 80,   0, 120,    0,  80, 100, 140, 8000),
        (255,   0, 180,   80, 255,  40, 200, 5000),
        (255, 120,   0,  255, 220,   0, 180, 3000),
        (255,  40, 200,  255, 220,   0, 255, 4000),
        ( 30,   0,  40,    0,   0,  10,  80, 12000),
    ),
    # Mono
    (
        ( 80,  60,  40,   40,  30,  20, 140, 8000),
        (220, 200, 160,  255, 220, 180, 200, 5000),
        (200, 180, 140,  255, 240, 200, 180, 3000),
        (255, 240, 220,  255, 255, 255, 255, 4000),
        ( 20,  15,  10,    0,   0,   0,  80, 12000),
    ),
)

# Per-palette pulse colour (RGB tuple) for the bass-driven beat layer.
_PULSE_COLOUR = (
    (255, 180,  60),   # Warm
    (  0, 220, 255),   # Cool
    (255,  60, 180),   # Vivid
    (255, 240, 220),   # Mono
)

# Per-palette starlight overlay colour.
_STARLIGHT_COLOUR = (
    (255, 220, 180),   # Warm
    (200, 220, 255),   # Cool
    (255, 255, 255),   # Vivid
    (255, 240, 220),   # Mono
)

# Sections whose palette-baked cycle_ms takes precedence over the
# wash_speed property (B0.2 - BuildUp / Drop / Breakdown should retain
# their feel regardless of the operator's speed pick).
_SECTION_OVERRIDES_SPEED = (False, False, True, True, True)


def _bound_target(group):
    """Build a render-target string for the Light class + chosen group."""
    return "01:%02x" % (group & 0xFF)


class Conductor(Show):
    """The Epic 6D B3 reference operator-driven Show."""

    def __init__(self):
        self._last_tap_ms = None
        self._last_pulse_rgb = (0, 0, 0)

    # -- Identity ---------------------------------------------------------

    def id(self):
        return "conductor"

    def display_name(self):
        return "Conductor"

    def kind(self):
        return PluginKind.SHOW

    def required_capabilities(self):
        # Draws to the screen and broadcasts. Tap input comes from the
        # IMU or the button fallback - IMU_TAP preferred but not required
        # (badge button C feeds the same code path).
        return CapabilityMask(Capability.DISPLAY, Capability.ESP_NOW)

    def properties(self):
        return _PROPS

    def power(self):
        # Event-driven (tap-fired); no periodic tick needed. The wash's
        # A<->B<->A drift is the receiver's autonomous job - the Director
        # only re-emits on operator action.
        return PowerProfile(needs_audio_frames=False, tick_hz=0)

    # -- Lifecycle --------------------------------------------------------

    def enter(self, ctx):
        # Reset per-session state and lay down the entry wash.
        self._last_tap_ms = None
        self._last_pulse_rgb = (0, 0, 0)
        self._emit_wash(ctx)

    def exit(self, ctx):
        # 1.0 s fade to black on the way out.
        ctx.render_wash_end(
            _bound_target(ctx.get_property("target_group")), 10,
        )

    # -- Input ------------------------------------------------------------

    def on_tap_detected(self, ctx, strength):
        if ctx.paused():
            return
        palette = ctx.get_property("palette_set")
        if palette < 0 or palette >= len(_PULSE_COLOUR):
            palette = 0
        r, g, b = _PULSE_COLOUR[palette]
        self._last_pulse_rgb = (r, g, b)
        self._last_tap_ms = ctx.now_ms()

        chance_idx = ctx.get_property("chance")
        if chance_idx < 0 or chance_idx >= len(_CHANCE_TABLE):
            chance_idx = _CHANCE_DEFAULT_IDX

        target = _bound_target(ctx.get_property("target_group"))
        # Drop section gets a longer sustain for a heavier feel; other
        # sections use a snappier T_96_MS.
        section = ctx.get_property("section")
        sustain = Time.T_192_MS if section == 3 else Time.T_96_MS  # 3 = Drop
        ctx.render_fx(target, RgbPulse(
            r, g, b,
            attack=Time.T_0_MS,
            sustain=sustain,
            release=Time.T_192_MS,
            chance=_CHANCE_TABLE[chance_idx],
        ))

        # Starlight overlay - rare second pulse in a contrasting colour,
        # suppressed during Breakdown so it doesn't fight the quiet.
        if ctx.get_property("starlight") and section != 4:  # 4 = Breakdown
            sr, sg, sb = _STARLIGHT_COLOUR[palette]
            ctx.render_fx(target, RgbPulse(
                sr, sg, sb,
                attack=Time.T_0_MS,
                sustain=Time.T_96_MS,
                release=Time.T_96_MS,
                chance=Chance.CHANCE_4,
            ))

    def on_beat_detected(self, ctx, strength):
        # The Tildagon IMU adapter fires on_beat_detected alongside
        # on_tap_detected (Epic 6B B4 contract). Route both to the same
        # pulse path so a beat-driven Show on M5 and a tap-driven Show
        # on Tildagon share the same authoring shape.
        self.on_tap_detected(ctx, strength)

    def on_input_action(self, ctx, action):
        if action == InputAction.CYCLE:
            self._advance_palette(ctx, +1)
        elif action == InputAction.CYCLE_PREV:
            self._advance_palette(ctx, -1)
        elif action == InputAction.SECTION_NEXT:
            self._advance_section(ctx, +1)
        elif action == InputAction.SECTION_PREV:
            self._advance_section(ctx, -1)
        elif action == InputAction.CONFIRM:
            # Manual-tap path on hosts that route Confirm to the Show.
            self.on_tap_detected(ctx, 192)

    def on_property_changed(self, ctx, key):
        # A property that changes the visible wash re-emits it. palette_set
        # / wash_speed / target_group / section all qualify. starlight and
        # sensitivity take effect on the next tap; chance is read fresh
        # per beat.
        if key in ("palette_set", "wash_speed", "target_group", "section"):
            self._emit_wash(ctx)

    # -- Render -----------------------------------------------------------

    def on_render(self, ctx):
        d = ctx.display()
        if d is None:
            return

        palette = ctx.get_property("palette_set")
        section = ctx.get_property("section")
        palette_name = _PALETTE_NAMES[palette] if 0 <= palette < len(_PALETTE_NAMES) else "?"
        section_name = _SECTION_NAMES[section] if 0 <= section < len(_SECTION_NAMES) else "?"

        bg = self._flash_background(ctx)
        d.clear(*bg)

        d.text(0, -75, "Conductor", size=18, r=255, g=255, b=255)
        d.text(0, -35, palette_name, size=26,
               r=self._last_pulse_rgb[0],
               g=self._last_pulse_rgb[1],
               b=self._last_pulse_rgb[2])
        d.text(0, 10, section_name, size=18, r=200, g=200, b=200)
        d.text(0, 50, "tap to beat", size=12, r=160, g=160, b=160)
        d.text(0, 85, "< >: palette  hold: section", size=10, r=120, g=120, b=120)
        d.text(0, 98, "F: exit", size=10, r=120, g=120, b=120)

    # -- Internals --------------------------------------------------------

    def _advance_palette(self, ctx, delta):
        cur = ctx.get_property("palette_set")
        nxt = (cur + delta) % len(_PALETTE_NAMES)
        # set_property fires on_property_changed which re-emits the wash.
        ctx.set_property("palette_set", nxt)

    def _advance_section(self, ctx, delta):
        cur = ctx.get_property("section")
        nxt = (cur + delta) % len(_SECTION_NAMES)
        # set_property fires on_property_changed which re-emits the wash.
        ctx.set_property("section", nxt)

    def _emit_wash(self, ctx):
        palette = ctx.get_property("palette_set")
        section = ctx.get_property("section")
        if palette < 0 or palette >= len(_PALETTES):
            palette = 0
        if section < 0 or section >= len(_SECTION_NAMES):
            section = _SECTION_IDX_DEFAULT

        entry = _PALETTES[palette][section]
        r1, g1, b1, r2, g2, b2, intensity, palette_cycle_ms = entry

        # Section override wins for BuildUp / Drop / Breakdown.
        if _SECTION_OVERRIDES_SPEED[section]:
            cycle_ms = palette_cycle_ms
        else:
            speed_idx = ctx.get_property("wash_speed")
            if speed_idx < 0 or speed_idx >= len(_WASH_SPEED_CYCLE_MS):
                speed_idx = _WASH_SPEED_DEFAULT_IDX
            cycle_ms = _WASH_SPEED_CYCLE_MS[speed_idx]

        target = _bound_target(ctx.get_property("target_group"))
        ctx.render_wash(target, RgbWash(
            r1, g1, b1, r2, g2, b2,
            attack=20,           # 2.0 s ramp-in
            release=10,          # 1.0 s default fade-out
            intensity=intensity,
            cycle_ms=cycle_ms,
            ttl_seconds=0,       # infinite; Show owns cleanup via exit()
            pulse_response=1,    # overlays accepted
        ))

    def _flash_background(self, ctx):
        # Brief tap-coloured flash that decays to black, mirroring
        # simple_tap's visual feedback so the operator sees that taps
        # registered even when looking at the LCD rather than the wash.
        if self._last_tap_ms is None:
            return (0, 0, 0)
        elapsed = ctx.now_ms() - self._last_tap_ms
        if elapsed < 0 or elapsed >= 200:
            return (0, 0, 0)
        scale = (1.0 - elapsed / 200) * 0.25
        r, g, b = self._last_pulse_rgb
        return (int(r * scale), int(g * scale), int(b * scale))


_PROPS = (
    PropertyDef(
        key="palette_set",
        type=PropertyType.ENUM,
        default_value=0,
        min_value=0,
        max_value=len(_PALETTE_NAMES) - 1,
        display_name="Palette",
        enum_names=_PALETTE_NAMES,
    ),
    PropertyDef(
        key="chance",
        type=PropertyType.ENUM,
        default_value=_CHANCE_DEFAULT_IDX,
        min_value=0,
        max_value=len(_CHANCE_NAMES) - 1,
        display_name="Beat chance",
        enum_names=_CHANCE_NAMES,
    ),
    PropertyDef(
        key="wash_speed",
        type=PropertyType.ENUM,
        default_value=_WASH_SPEED_DEFAULT_IDX,
        min_value=0,
        max_value=len(_WASH_SPEED_NAMES) - 1,
        display_name="Wash speed",
        enum_names=_WASH_SPEED_NAMES,
    ),
    PropertyDef(
        key="sensitivity",
        type=PropertyType.ENUM,
        default_value=1,    # Medium - operator-driven Show; conservative tap floor
        min_value=0,
        max_value=2,
        display_name="Sensitivity",
        enum_names=("Low", "Medium", "High"),
    ),
    PropertyDef(
        key="starlight",
        type=PropertyType.BOOL,
        default_value=False,
        display_name="Starlight",
    ),
    PropertyDef(
        key="section",
        type=PropertyType.ENUM,
        default_value=_SECTION_IDX_DEFAULT,
        min_value=0,
        max_value=len(_SECTION_NAMES) - 1,
        display_name="Section",
        enum_names=_SECTION_NAMES,
    ),
    PropertyDef(
        key="target_group",
        type=PropertyType.U8,
        default_value=0,
        min_value=0,
        max_value=255,
        display_name="Target group",
    ),
)


def make_show():
    """Factory the registry calls on discovery."""
    return Conductor()
