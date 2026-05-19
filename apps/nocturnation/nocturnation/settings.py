"""Persistent settings for the NocturNation Tildagon app.

Three operator-tunable values:

  calm_mode (bool, default True)
    When True the perimeter LED renderer caps at 50 % peak brightness
    and 2 Hz dispatch, and the LCD pulse wash is disabled entirely
    (the LCD stays on its static UI). Architecture spec section 15
    photosensitivity bounds. Operator toggles via the in-app menu.

  group (int 0..255, default 0)
    NocturNation device group per protocol manual section 4.2.
    target_group == 0 is broadcast and is always accepted regardless;
    target_group != 0 is accepted only when it matches this setting
    exactly. A device with group == 0 has no group and only accepts
    broadcasts. The in-app menu cycles 0..3 to keep the operator
    interaction simple; richer values are reserved for future Epics.

  channel (str: "auto", "1", "11", default "auto")
    ESP-NOW receive channel. "auto" delegates to the channel-scan
    state machine; "1" / "11" pin to that single channel. Channel-set
    on STA_IF is known-flaky on Tildagon (Epic 5 Q6); the operator
    may end up locked to channel 11 regardless of this setting.

  active_show (str, default "")
    Director-mode active Show id (Epic 6B). Persisted so the badge
    reopens the last-used Show on the next Director-mode entry. Empty
    string means "no choice yet" - the DirectorController falls back
    to the first registered Show.

  mode (str: "lume", "director", default "lume")
    App role (Epic 6B). "lume" is the original receive-only behaviour;
    "director" runs the Show framework + IMU tap-to-beat and broadcasts
    LIGHT_COMMAND frames. Persisted so a Director badge reopens in
    Director mode. Unknown values fall back to "lume" (the safe,
    receive-only default).

Persisted as JSON at DEFAULT_PATH (outside the apps directory so the
app's deploy.sh wipe-and-copy doesn't clobber operator preferences).
"""

import json


# Outside /apps/ so the app's wipe-and-redeploy cycle doesn't clobber
# operator preferences. Settings file is a tiny JSON document; survives
# firmware re-flash because Tildagon preserves user files across most
# firmware updates.
DEFAULT_PATH = "/nocturnation_settings.json"

_VALID_CHANNELS = ("auto", "1", "11")
_VALID_MODES = ("lume", "director")


class Settings:
    __slots__ = ("calm_mode", "group", "channel", "active_show", "mode")

    def __init__(self, calm_mode=True, group=0, channel="auto", active_show="",
                 mode="lume"):
        self.calm_mode = bool(calm_mode)
        self.group = self._coerce_group(group)
        self.channel = self._coerce_channel(channel)
        self.active_show = self._coerce_active_show(active_show)
        self.mode = self._coerce_mode(mode)

    @staticmethod
    def _coerce_group(g):
        """Clamp to 0..255. Non-int or out-of-range falls back to 0
        (broadcast-only) rather than raising, so a corrupted settings
        file produces a sensible default instead of a crash."""
        try:
            v = int(g)
        except (TypeError, ValueError):
            return 0
        if v < 0 or v > 255:
            return 0
        return v

    @staticmethod
    def _coerce_channel(c):
        """Constrain to known values. Anything else falls back to "auto"."""
        if c in _VALID_CHANNELS:
            return c
        return "auto"

    @staticmethod
    def _coerce_active_show(s):
        """Coerce to a string; non-strings fall back to "" (no choice).
        The DirectorController validates the id against the registry, so
        an unknown-but-well-formed id is harmless here."""
        if isinstance(s, str):
            return s
        return ""

    @staticmethod
    def _coerce_mode(m):
        """Constrain to known modes. Anything else falls back to "lume"
        - the safe, receive-only default."""
        if m in _VALID_MODES:
            return m
        return "lume"

    def to_dict(self):
        return {
            "calm_mode": self.calm_mode,
            "group": self.group,
            "channel": self.channel,
            "active_show": self.active_show,
            "mode": self.mode,
        }

    @classmethod
    def from_dict(cls, d):
        if not isinstance(d, dict):
            return cls()
        return cls(
            calm_mode=d.get("calm_mode", True),
            group=d.get("group", 0),
            channel=d.get("channel", "auto"),
            active_show=d.get("active_show", ""),
            mode=d.get("mode", "lume"),
        )

    def save(self, path=DEFAULT_PATH):
        with open(path, "w") as f:
            json.dump(self.to_dict(), f)

    @classmethod
    def load(cls, path=DEFAULT_PATH):
        """Load from disk; return defaults if the file is missing or
        corrupt. Errors are silent because the boot path should never
        be blocked by a corrupted settings file - the operator's
        recourse is to re-tune the settings in-app."""
        try:
            with open(path, "r") as f:
                return cls.from_dict(json.load(f))
        except (OSError, ValueError):
            return cls()

    def __eq__(self, other):
        if not isinstance(other, Settings):
            return NotImplemented
        return (
            self.calm_mode == other.calm_mode
            and self.group == other.group
            and self.channel == other.channel
            and self.active_show == other.active_show
            and self.mode == other.mode
        )

    def __repr__(self):
        return (
            "Settings(calm_mode=%r, group=%r, channel=%r, active_show=%r, mode=%r)"
            % (
                self.calm_mode,
                self.group,
                self.channel,
                self.active_show,
                self.mode,
            )
        )
