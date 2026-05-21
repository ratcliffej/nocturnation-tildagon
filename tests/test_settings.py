"""Persistent settings tests.

The settings file is tiny and the validation rules exist mainly to
defend against corrupt JSON. These tests cover the coercion paths
(out-of-range, wrong type), round-trip save/load, and the fallback
behaviour when the file is missing.
"""

import os
import tempfile

import pytest

from nocturnation.settings import Settings, DEFAULT_PATH, DEFAULT_HELP_URL


class TestDefaults:
    def test_default_values(self):
        s = Settings()
        assert s.calm_mode is True
        assert s.group == 0
        assert s.channel == "auto"
        assert s.active_show == ""
        assert s.mode == "lume"
        assert s.help_url == DEFAULT_HELP_URL


class TestGroupCoercion:
    def test_int_in_range_kept(self):
        assert Settings(group=5).group == 5
        assert Settings(group=255).group == 255
        assert Settings(group=0).group == 0

    def test_negative_clamps_to_zero(self):
        assert Settings(group=-1).group == 0
        assert Settings(group=-100).group == 0

    def test_too_high_clamps_to_zero(self):
        assert Settings(group=256).group == 0
        assert Settings(group=1000).group == 0

    def test_string_int_coerces(self):
        assert Settings(group="7").group == 7

    def test_non_numeric_clamps_to_zero(self):
        assert Settings(group="abc").group == 0
        assert Settings(group=None).group == 0


class TestChannelCoercion:
    def test_valid_values_kept(self):
        assert Settings(channel="auto").channel == "auto"
        assert Settings(channel="1").channel == "1"
        assert Settings(channel="11").channel == "11"

    def test_invalid_channel_falls_back_to_auto(self):
        # Channel 6 is documented as "advanced override" on the M5
        # side but not in the Tildagon's auto-scan order; reject it
        # rather than honour it silently.
        assert Settings(channel="6").channel == "auto"
        assert Settings(channel="invalid").channel == "auto"
        assert Settings(channel=11).channel == "auto"  # int, not str
        assert Settings(channel=None).channel == "auto"


class TestDictRoundTrip:
    def test_to_dict_has_all_fields(self):
        s = Settings(calm_mode=False, group=3, channel="11",
                     active_show="simple_tap", mode="director",
                     help_url="http://example.com")
        d = s.to_dict()
        assert d == {
            "calm_mode": False,
            "group": 3,
            "channel": "11",
            "active_show": "simple_tap",
            "mode": "director",
            "help_url": "http://example.com",
        }

    def test_from_dict_populates(self):
        s = Settings.from_dict({
            "calm_mode": False, "group": 2, "channel": "1",
            "active_show": "motion_wave", "mode": "director",
            "help_url": "http://example.com",
        })
        assert s.calm_mode is False
        assert s.group == 2
        assert s.channel == "1"
        assert s.active_show == "motion_wave"
        assert s.mode == "director"
        assert s.help_url == "http://example.com"

    def test_from_dict_missing_keys_uses_defaults(self):
        s = Settings.from_dict({"calm_mode": False})
        assert s.calm_mode is False
        assert s.group == 0
        assert s.channel == "auto"
        assert s.active_show == ""
        assert s.mode == "lume"
        assert s.help_url == DEFAULT_HELP_URL

    def test_from_dict_non_dict_returns_defaults(self):
        # Corrupted JSON might decode to a list or string; fall back
        # to defaults rather than crash.
        assert Settings.from_dict("not a dict") == Settings()
        assert Settings.from_dict(None) == Settings()
        assert Settings.from_dict([]) == Settings()


class TestPersistence:
    def test_save_then_load_round_trips(self):
        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".json"
        ) as f:
            path = f.name
        try:
            original = Settings(calm_mode=False, group=2, channel="11")
            original.save(path)
            loaded = Settings.load(path)
            assert loaded == original
        finally:
            os.unlink(path)

    def test_load_missing_file_returns_defaults(self):
        loaded = Settings.load("/tmp/does-not-exist-nocturnation.json")
        assert loaded == Settings()

    def test_load_corrupt_json_returns_defaults(self):
        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".json"
        ) as f:
            f.write("{not valid json")
            path = f.name
        try:
            loaded = Settings.load(path)
            assert loaded == Settings()
        finally:
            os.unlink(path)

    def test_default_path_constant(self):
        # Outside /apps/ so app deploys don't clobber operator settings.
        assert DEFAULT_PATH.startswith("/")
        assert not DEFAULT_PATH.startswith("/apps/")


class TestActiveShow:
    def test_default_empty(self):
        assert Settings().active_show == ""

    def test_string_kept(self):
        assert Settings(active_show="simple_tap").active_show == "simple_tap"

    def test_non_string_falls_back_to_empty(self):
        # Corrupt JSON could decode active_show to a non-string.
        assert Settings(active_show=42).active_show == ""
        assert Settings(active_show=None).active_show == ""

    def test_round_trips(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as f:
            path = f.name
        try:
            Settings(active_show="motion_wave").save(path)
            assert Settings.load(path).active_show == "motion_wave"
        finally:
            os.unlink(path)

    def test_different_active_show_not_equal(self):
        assert Settings(active_show="a") != Settings(active_show="b")


class TestMode:
    def test_default_lume(self):
        assert Settings().mode == "lume"

    def test_valid_modes_kept(self):
        assert Settings(mode="lume").mode == "lume"
        assert Settings(mode="director").mode == "director"

    def test_unknown_mode_falls_back_to_lume(self):
        assert Settings(mode="wizard").mode == "lume"
        assert Settings(mode=None).mode == "lume"
        assert Settings(mode=1).mode == "lume"

    def test_round_trips(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as f:
            path = f.name
        try:
            Settings(mode="director").save(path)
            assert Settings.load(path).mode == "director"
        finally:
            os.unlink(path)

    def test_different_mode_not_equal(self):
        assert Settings(mode="lume") != Settings(mode="director")


class TestHelpUrl:
    def test_default(self):
        assert Settings().help_url == DEFAULT_HELP_URL
        assert DEFAULT_HELP_URL == "http://www.nocturnation.net"

    def test_custom_kept(self):
        assert Settings(help_url="http://example.com/x").help_url == "http://example.com/x"

    def test_empty_or_non_string_falls_back(self):
        assert Settings(help_url="").help_url == DEFAULT_HELP_URL
        assert Settings(help_url=None).help_url == DEFAULT_HELP_URL
        assert Settings(help_url=42).help_url == DEFAULT_HELP_URL

    def test_round_trips(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as f:
            path = f.name
        try:
            Settings(help_url="http://example.com/y").save(path)
            assert Settings.load(path).help_url == "http://example.com/y"
        finally:
            os.unlink(path)


class TestEquality:
    def test_equal_settings_compare_equal(self):
        assert Settings() == Settings()
        assert Settings(group=3) == Settings(group=3)

    def test_different_calm_mode_not_equal(self):
        assert Settings(calm_mode=True) != Settings(calm_mode=False)

    def test_different_type_not_equal(self):
        assert Settings() != "not a Settings"
