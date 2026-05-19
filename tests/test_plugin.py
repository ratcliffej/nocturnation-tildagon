"""Plugin / PropertyDef / PowerProfile / PropertyBag tests."""

import json

import pytest

from nocturnation.hal import Capability, CapabilityMask
from nocturnation.plugins import (
    Plugin,
    PluginKind,
    PowerProfile,
    PropertyBag,
    PropertyDef,
    PropertyType,
)


class _StubPlugin(Plugin):
    """Concrete Plugin used to exercise the abstract base."""

    _PROPS = (
        PropertyDef(
            key="enabled",
            type=PropertyType.BOOL,
            default_value=True,
            display_name="Enabled",
        ),
        PropertyDef(
            key="level",
            type=PropertyType.U8,
            default_value=42,
            min_value=0,
            max_value=255,
            display_name="Level",
        ),
        PropertyDef(
            key="palette",
            type=PropertyType.ENUM,
            default_value=1,
            min_value=0,
            max_value=3,
            display_name="Palette",
            enum_names=("Cool", "Natural", "Warm", "Rainbow"),
        ),
        PropertyDef(
            key="colour",
            type=PropertyType.COLOUR,
            default_value=0x00FF8800,
            display_name="Tint",
        ),
    )

    def __init__(self, plugin_id="stub"):
        self._id = plugin_id

    def id(self):
        return self._id

    def display_name(self):
        return "Stub"

    def kind(self):
        return PluginKind.SHOW

    def required_capabilities(self):
        return CapabilityMask(Capability.DISPLAY, Capability.IMU_TAP)

    def properties(self):
        return self._PROPS

    def power(self):
        return PowerProfile(needs_audio_frames=False, tick_hz=10)


class TestPluginBaseRequiresOverrides:
    def test_id_must_be_overridden(self):
        with pytest.raises(NotImplementedError):
            Plugin().id()

    def test_display_name_must_be_overridden(self):
        with pytest.raises(NotImplementedError):
            Plugin().display_name()

    def test_kind_must_be_overridden(self):
        with pytest.raises(NotImplementedError):
            Plugin().kind()

    def test_default_required_capabilities_is_empty(self):
        # A plug-in that doesn't override required_capabilities() should
        # run on every host (empty mask is subset of anything).
        class NoCaps(Plugin):
            def id(self): return "x"
            def display_name(self): return "X"
            def kind(self): return PluginKind.SHOW
        assert NoCaps().required_capabilities().empty() is True


class TestPropertyDefValidation:
    def test_long_key_rejected(self):
        # 15-char limit is the ESP-IDF Preferences key cap on M5, kept
        # for cross-platform parity.
        with pytest.raises(ValueError):
            PropertyDef(key="x" * 16, type=PropertyType.U8, default_value=0)

    def test_15_char_key_accepted(self):
        PropertyDef(key="x" * 15, type=PropertyType.U8, default_value=0)


class TestPropertyDefClamp:
    def test_bool_coerces_truthy(self):
        d = PropertyDef("b", PropertyType.BOOL, True)
        assert d.clamp(1) is True
        assert d.clamp(0) is False
        # Wrong type returns the default rather than crashing.
        assert d.clamp("not a bool") is True

    def test_u8_clamps_to_bounds(self):
        d = PropertyDef("v", PropertyType.U8, 100, min_value=10, max_value=200)
        assert d.clamp(50) == 50
        assert d.clamp(5) == 10     # below min
        assert d.clamp(500) == 200  # above max

    def test_enum_clamps_to_range(self):
        d = PropertyDef("p", PropertyType.ENUM, 1, min_value=0, max_value=3)
        assert d.clamp(2) == 2
        assert d.clamp(5) == 3
        assert d.clamp(-1) == 0

    def test_colour_masks_high_bits(self):
        d = PropertyDef("c", PropertyType.COLOUR, 0x00FF0000)
        assert d.clamp(0x00ABCDEF) == 0x00ABCDEF
        # High byte (alpha) is dropped because the wire/display surface
        # only carries 24-bit colour.
        assert d.clamp(0xFFABCDEF) == 0x00ABCDEF

    def test_clamp_returns_default_on_type_error(self):
        d = PropertyDef("v", PropertyType.U8, 99)
        assert d.clamp("not a number") == 99


class TestPropertyBagPersistence:
    def test_get_returns_schema_default_initially(self, tmp_path):
        plugin = _StubPlugin()
        bag = PropertyBag(plugin, path=str(tmp_path / "p.json"))
        assert bag.get("enabled") is True
        assert bag.get("level") == 42
        assert bag.get("palette") == 1
        assert bag.get("colour") == 0x00FF8800

    def test_set_persists_round_trip(self, tmp_path):
        path = str(tmp_path / "p.json")
        plugin = _StubPlugin()
        bag1 = PropertyBag(plugin, path=path)
        bag1.set("level", 100)
        bag1.set("palette", 2)
        # Re-open: values survive.
        bag2 = PropertyBag(plugin, path=path)
        assert bag2.get("level") == 100
        assert bag2.get("palette") == 2

    def test_set_clamps_returns_stored(self, tmp_path):
        plugin = _StubPlugin()
        bag = PropertyBag(plugin, path=str(tmp_path / "p.json"))
        # 500 clamps to 255 (U8 max).
        stored = bag.set("level", 500)
        assert stored == 255
        assert bag.get("level") == 255

    def test_unknown_key_raises(self, tmp_path):
        plugin = _StubPlugin()
        bag = PropertyBag(plugin, path=str(tmp_path / "p.json"))
        with pytest.raises(KeyError):
            bag.get("nope")
        with pytest.raises(KeyError):
            bag.set("nope", 1)

    def test_two_plugins_share_file(self, tmp_path):
        # Single file with one section per plug-in id - matches the M5
        # NVS namespace shape (each id gets its own slot).
        path = str(tmp_path / "p.json")
        a = PropertyBag(_StubPlugin("alpha"), path=path)
        b = PropertyBag(_StubPlugin("beta"),  path=path)
        a.set("level", 11)
        b.set("level", 99)
        # Read back both - no cross-talk.
        assert PropertyBag(_StubPlugin("alpha"), path=path).get("level") == 11
        assert PropertyBag(_StubPlugin("beta"),  path=path).get("level") == 99

    def test_corrupt_file_falls_back_to_defaults(self, tmp_path):
        path = str(tmp_path / "p.json")
        with open(path, "w") as f:
            f.write("{ not json")
        plugin = _StubPlugin()
        bag = PropertyBag(plugin, path=path)
        assert bag.get("level") == 42  # default kept

    def test_disk_format_is_sectioned_by_plugin_id(self, tmp_path):
        # Direct read: confirm the file shape is { plugin_id: { key: value } }.
        path = str(tmp_path / "p.json")
        plugin = _StubPlugin("simple_tap")
        bag = PropertyBag(plugin, path=path)
        bag.set("level", 77)
        with open(path, "r") as f:
            data = json.load(f)
        assert "simple_tap" in data
        assert data["simple_tap"]["level"] == 77


class TestPowerProfileDefaults:
    def test_defaults_match_m5(self):
        # Defaults match the C++ PowerProfile defaults so a Show writer
        # gets the same "ordinary audio-driven vis" baseline on both
        # platforms.
        p = PowerProfile()
        assert p.needs_audio_frames is True
        assert p.needs_spectrum_frame is False
        assert p.needs_8band_summary is False
        assert p.lcd_refresh_hz_max == 20
        assert p.tick_hz == 0
