"""Plugin base class + supporting types.

Cross-platform mirror of `nocturnation::plugins::Plugin` in the M5
firmware. Concrete subclasses on Tildagon (`Show` in
`nocturnation.shows.show`) extend this and add their own
event/lifecycle hooks.
"""

from ..hal import CapabilityMask


class PropertyType:
    """Type tag for a PropertyDef. Mirrors C++ `PropertyType` enum."""

    BOOL   = 0
    U8     = 1     # 0..255
    U16    = 2     # 0..65535
    COLOUR = 3     # packed 0x00RRGGBB
    ENUM   = 4     # U8 with named values for UI; min/max bound the index range


class PropertyDef:
    """Schema entry for one persisted/editable property.

    Plug-ins return a tuple/list of these from `properties()`. The
    framework uses the schema for: persistence, UI auto-generation,
    bounds clamping, type validation on writes.

    `key` is the storage key. Must be <= 15 ASCII chars (matches the
    ESP-IDF Preferences key cap on M5; same constraint kept here for
    cross-platform parity).
    """

    __slots__ = (
        "key",
        "type",
        "default_value",
        "min_value",
        "max_value",
        "display_name",
        "unit",
        "enum_names",
    )

    def __init__(
        self,
        key,
        type,
        default_value,
        min_value=None,
        max_value=None,
        display_name=None,
        unit=None,
        enum_names=None,
    ):
        if len(key) > 15:
            raise ValueError("PropertyDef.key too long (max 15 chars): %r" % key)
        self.key           = key
        self.type          = type
        self.default_value = default_value
        self.min_value     = min_value
        self.max_value     = max_value
        self.display_name  = display_name or key
        self.unit          = unit
        self.enum_names    = enum_names

    def clamp(self, value):
        """Clamp a candidate value to the schema's bounds.

        Returns the clamped value, or the default if `value` is the
        wrong type for `self.type`.
        """
        t = self.type
        if t == PropertyType.BOOL:
            return bool(value) if isinstance(value, (bool, int)) else self.default_value
        if t in (PropertyType.U8, PropertyType.U16, PropertyType.ENUM):
            try:
                v = int(value)
            except (TypeError, ValueError):
                return self.default_value
            lo = self.min_value if self.min_value is not None else 0
            hi = self.max_value if self.max_value is not None else (255 if t == PropertyType.U8 else 65535)
            if v < lo:
                v = lo
            if v > hi:
                v = hi
            return v
        if t == PropertyType.COLOUR:
            try:
                v = int(value)
            except (TypeError, ValueError):
                return self.default_value
            return v & 0x00FFFFFF
        return self.default_value


class PowerProfile:
    """What a plug-in needs from the host pipeline.

    Mirrors C++ `PowerProfile`. Defaults match M5 ("ordinary
    audio-driven vis") for cross-platform consistency; Tildagon
    backends ignore the audio fields since the host has no microphone.
    """

    __slots__ = (
        "needs_audio_frames",
        "needs_spectrum_frame",
        "needs_8band_summary",
        "lcd_refresh_hz_max",
        "tick_hz",
    )

    def __init__(
        self,
        needs_audio_frames=True,
        needs_spectrum_frame=False,
        needs_8band_summary=False,
        lcd_refresh_hz_max=20,
        tick_hz=0,
    ):
        self.needs_audio_frames   = needs_audio_frames
        self.needs_spectrum_frame = needs_spectrum_frame
        self.needs_8band_summary  = needs_8band_summary
        self.lcd_refresh_hz_max   = lcd_refresh_hz_max
        self.tick_hz              = tick_hz


class PluginKind:
    """Plug-in kind tag - used for storage namespace prefix.

    Mirrors C++ `PluginKind`. NVS-namespace convention from M5
    (`nv_<id>` / `nb_<id>` / `ns_<id>`) is preserved here for
    documentation parity, but Tildagon persistence is JSON-backed.
    """

    VISUALISATION = 0
    OUTPUT_BINDING = 1
    SHOW = 2


class Plugin:
    """Base class for NocturNation plug-ins on Tildagon.

    Subclasses override the identity methods (`id`, `display_name`,
    `kind`) and optionally `required_capabilities`, `properties`,
    `power`. The Show base class in `nocturnation.shows.show`
    extends this with event/render hooks.
    """

    def id(self):
        """Stable string identity. <= 12 ASCII chars by convention
        (matches M5's NVS-namespace constraint of `ns_<id>` <= 15)."""
        raise NotImplementedError("Plugin.id() must be overridden")

    def display_name(self):
        """Human-readable name for menus / settings screens."""
        raise NotImplementedError("Plugin.display_name() must be overridden")

    def kind(self):
        """Which plug-in kind / registry this lives in. Show
        subclasses return PluginKind.SHOW; future Visualisation /
        OutputBinding subclasses return their kind."""
        raise NotImplementedError("Plugin.kind() must be overridden")

    def required_capabilities(self):
        """CapabilityMask declaring what host capabilities are
        required for this plug-in to run. Empty mask = no
        requirements. The framework gates plug-in selection with
        `req.subset_of(host_mask)`."""
        return CapabilityMask()

    def properties(self):
        """Tuple of PropertyDef. Empty = no properties."""
        return ()

    def power(self):
        """PowerProfile declaring what the plug-in needs from the
        pipeline. Default is the M5 'ordinary audio-driven vis'
        profile; Tildagon backends ignore audio fields."""
        return PowerProfile()
