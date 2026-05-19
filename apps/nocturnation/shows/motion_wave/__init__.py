"""motion_wave - a motion-driven reference Show.

Wave the badge around; the dominant axis of movement picks a colour
(X=red, Y=green, Z=blue) and the movement magnitude sets the
brightness. Demonstrates the IMU motion hook (`on_motion_event`)
alongside simple_tap's tap hook, and gives the Show picker a second
entry.
"""

from nocturnation.shows import Show
from nocturnation.hal import Capability, CapabilityMask
from nocturnation.plugins import PluginKind, PowerProfile, PropertyDef, PropertyType
from nocturnation.render import RgbPulse
from nocturnation.protocol.constants import Time, Chance


# Axis -> base colour. axis is 0=X, 1=Y, 2=Z (from on_motion_event).
_AXIS_RGB = {
    0: (255, 0, 0),   # X - red
    1: (0, 255, 0),   # Y - green
    2: (0, 0, 255),   # Z - blue
}
_AXIS_NAME = {0: "X", 1: "Y", 2: "Z"}


class MotionWave(Show):
    def __init__(self):
        self._last_axis = None
        self._last_mag = 0

    def id(self):
        return "motion_wave"

    def display_name(self):
        return "Motion Wave"

    def kind(self):
        return PluginKind.SHOW

    def required_capabilities(self):
        return CapabilityMask(Capability.DISPLAY, Capability.ESP_NOW)

    def properties(self):
        return _PROPS

    def power(self):
        return PowerProfile(needs_audio_frames=False, tick_hz=0)

    def on_motion_event(self, ctx, axis, magnitude):
        self._last_axis = axis
        self._last_mag = magnitude
        base = _AXIS_RGB.get(axis, (255, 255, 255))
        scale = magnitude / 255.0
        r = int(base[0] * scale)
        g = int(base[1] * scale)
        b = int(base[2] * scale)
        ctx.render_fx("01:00", RgbPulse(
            r, g, b,
            attack=Time.T_96_MS,
            sustain=Time.T_192_MS,
            release=Time.T_480_MS,
            chance=Chance.CHANCE_100,
        ))

    def on_render(self, ctx):
        d = ctx.display()
        if d is None:
            return
        d.clear(0, 0, 0)
        d.text(0, -60, "Motion Wave", size=22, r=255, g=255, b=255)
        axis_label = _AXIS_NAME.get(self._last_axis, "-")
        base = _AXIS_RGB.get(self._last_axis, (180, 180, 180))
        d.text(0, -5, "axis %s" % axis_label, size=28, r=base[0], g=base[1], b=base[2])
        d.text(0, 40, "wave the badge", size=14, r=180, g=180, b=180)
        d.text(0, 95, "F: exit", size=10, r=120, g=120, b=120)


_PROPS = (
    PropertyDef(
        key="sensitivity",
        type=PropertyType.ENUM,
        default_value=1,  # Medium
        min_value=0,
        max_value=2,
        display_name="Sensitivity",
        enum_names=("Low", "Medium", "High"),
    ),
)


def make_show():
    return MotionWave()
