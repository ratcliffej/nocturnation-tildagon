"""Persistent settings for the NocturNation Tildagon app.

Three operator-tunable values:

  calm_mode (bool, default False)
    When True the perimeter LED renderer caps at 50 % peak brightness
    and 2 Hz dispatch, and the LCD pulse wash is disabled entirely
    (the LCD stays on its static UI). Architecture spec section 15
    photosensitivity bounds. Default flipped OFF on 2026-07-12 (v1.0.0)
    so a fresh install renders the authored show at full authored
    brightness; the operator opts INTO calm mode via the in-app menu
    for photosensitivity-sensitive contexts.

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
    LIGHT_PULSE frames. Persisted so a Director badge reopens in
    Director mode. Unknown values fall back to "lume" (the safe,
    receive-only default).

  help_url (str, default "http://www.nocturnation.net")
    URL the Help-screen QR code points to (Epic 6B B9). Configurable so
    a deployment can point it at its own page; defaults to the project
    site. Non-strings / empty fall back to the default.

Persisted as JSON at DEFAULT_PATH (outside the apps directory so the
app's deploy.sh wipe-and-copy doesn't clobber operator preferences).
"""

import json


# Outside /apps/ so the app's wipe-and-redeploy cycle doesn't clobber
# operator preferences. Settings file is a tiny JSON document; survives
# firmware re-flash because Tildagon preserves user files across most
# firmware updates.
DEFAULT_PATH = "/nocturnation_settings.json"
DEFAULT_HELP_URL = "http://www.nocturnation.net"

_VALID_CHANNELS = ("auto", "1", "11")
_VALID_MODES = ("lume", "director")
_VALID_REPEAT = ("AUTO", "OFF")


class Settings:
    __slots__ = ("calm_mode", "group", "channel", "active_show", "mode",
                 "help_url", "debug_mode", "repeat")

    def __init__(self, calm_mode=False, group=0, channel="auto", active_show="",
                 mode="lume", help_url=DEFAULT_HELP_URL, debug_mode=False,
                 repeat="AUTO"):
        self.calm_mode = bool(calm_mode)
        self.group = self._coerce_group(group)
        self.channel = self._coerce_channel(channel)
        self.active_show = self._coerce_active_show(active_show)
        self.mode = self._coerce_mode(mode)
        self.help_url = self._coerce_help_url(help_url)
        # Epic 15 bench feedback: operator-toggle diagnostic overlay
        # on the Lume LCD. Shows last-heartbeat freshness, hop count,
        # heartbeats per 10s, and the TOFU lock label. Used to
        # objectively measure repeat-mode behaviour at range. Default
        # OFF so a typical Lume keeps a clean LCD.
        self.debug_mode = bool(debug_mode)
        # Epic 17: dynamic repeater mode. "AUTO" enables the FSM in
        # Lume mode; the badge silently becomes an audience repeater
        # when no upstream peer is covering its vicinity, and steps
        # down when a peer takes over. Default AUTO because engineered
        # (StickC) repeaters naturally suppress audience election, so
        # AUTO is safe as a default even in fully-covered deployments.
        # "OFF" disables the FSM entirely (no election, no relay).
        self.repeat = self._coerce_repeat(repeat)

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

    @staticmethod
    def _coerce_help_url(u):
        """Non-empty string kept; anything else falls back to the
        project default."""
        if isinstance(u, str) and u:
            return u
        return DEFAULT_HELP_URL

    @staticmethod
    def _coerce_repeat(r):
        """Constrain to AUTO / OFF; anything else falls back to AUTO
        (the audience-mesh default)."""
        if r in _VALID_REPEAT:
            return r
        return "AUTO"

    def to_dict(self):
        return {
            "calm_mode": self.calm_mode,
            "group": self.group,
            "channel": self.channel,
            "active_show": self.active_show,
            "mode": self.mode,
            "help_url": self.help_url,
            "debug_mode": self.debug_mode,
            "repeat": self.repeat,
        }

    @classmethod
    def from_dict(cls, d):
        if not isinstance(d, dict):
            return cls()
        return cls(
            calm_mode=d.get("calm_mode", False),
            group=d.get("group", 0),
            channel=d.get("channel", "auto"),
            active_show=d.get("active_show", ""),
            mode=d.get("mode", "lume"),
            help_url=d.get("help_url", DEFAULT_HELP_URL),
            debug_mode=d.get("debug_mode", False),
            repeat=d.get("repeat", "AUTO"),
        )

    def save(self, path=DEFAULT_PATH):
        with open(path, "w") as f:
            json.dump(self.to_dict(), f)

    @classmethod
    def load(cls, path=DEFAULT_PATH):
        """Load from disk; return defaults if the file is missing or
        corrupt. Errors are silent because the boot path should never
        be blocked by a corrupted settings file - the operator's
        recourse is to re-tune the settings in-app.

        First-install behaviour (EMF stage-team feature 2026-06-23):
        when the settings file doesn't exist (or is unreadable), roll
        a random group in [1, 3] and persist it. With a swarm of
        Tildagon Lumes coming out of the box this gives the LD ~3
        evenly-distributed addressable zones without per-device
        config. The operator can override via the in-app settings
        menu after first boot; the override is sticky from then on.
        """
        try:
            with open(path, "r") as f:
                return cls.from_dict(json.load(f))
        except (OSError, ValueError):
            import random
            s = cls(group=random.randint(1, 3))
            try:
                s.save(path)
            except OSError:
                # Filesystem write failed (read-only? out of space?).
                # In-memory random group is still returned; next boot
                # will re-roll, which is acceptable failure mode.
                pass
            return s

    def __eq__(self, other):
        if not isinstance(other, Settings):
            return NotImplemented
        return (
            self.calm_mode == other.calm_mode
            and self.group == other.group
            and self.channel == other.channel
            and self.active_show == other.active_show
            and self.mode == other.mode
            and self.help_url == other.help_url
            and self.debug_mode == other.debug_mode
            and self.repeat == other.repeat
        )

    def __repr__(self):
        return (
            "Settings(calm_mode=%r, group=%r, channel=%r, active_show=%r, "
            "mode=%r, help_url=%r, debug_mode=%r, repeat=%r)"
            % (
                self.calm_mode,
                self.group,
                self.channel,
                self.active_show,
                self.mode,
                self.help_url,
                self.debug_mode,
                self.repeat,
            )
        )
