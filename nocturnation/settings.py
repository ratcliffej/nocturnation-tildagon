"""Persistent settings for the NocturNation Tildagon app.

Operator-tunable values:

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
    __slots__ = ("group", "channel", "active_show", "mode",
                 "help_url", "debug_mode", "repeat",
                 # Epic 20 B5: BLE-writable fields. `friendly_name`
                 # overrides the MAC-derived advertising name;
                 # `pair_win_s` controls the pairing-window duration.
                 # Both persist in the same JSON as everything else.
                 "friendly_name", "pair_win_s",
                 # Accepted-and-remembered for cross-host consistency
                 # with the property bag, even though Tildagon has a
                 # fixed 12-pixel perimeter. Values written via BLE
                 # are stored so the phone's view of the fleet stays
                 # coherent; rendering ignores them in v0x01.
                 "led_power", "strip_chain", "strip_group_size")

    def __init__(self, group=0, channel="auto", active_show="",
                 mode="lume", help_url=DEFAULT_HELP_URL, debug_mode=False,
                 repeat="OFF",
                 friendly_name="", pair_win_s=30,
                 led_power=100, strip_chain=12, strip_group_size=12):
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
        # down when a peer takes over. "OFF" disables the FSM entirely
        # (no election, no relay). Default flipped AUTO -> OFF on
        # 2026-07-12 (v1.0.1): the audience-mesh election adds airtime
        # and battery load to every badge in the fleet, and the ex-
        # perienced case at EMF is that dedicated (StickC) repeaters
        # cover the venue; opt INTO AUTO from Settings when running a
        # coverage-marginal deployment without engineered repeaters.
        self.repeat = self._coerce_repeat(repeat)
        # Epic 20 B5: BLE-writable fields.
        self.friendly_name = self._coerce_friendly_name(friendly_name)
        self.pair_win_s = self._coerce_pair_win_s(pair_win_s)
        self.led_power = self._coerce_u8(led_power, 100)
        self.strip_chain = self._coerce_u16(strip_chain, 12)
        self.strip_group_size = self._coerce_u8(strip_group_size, 12)

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
        """Constrain to AUTO / OFF; anything else falls back to OFF
        (the safe default - no audience-mesh election / relay)."""
        if r in _VALID_REPEAT:
            return r
        return "OFF"

    @staticmethod
    def _coerce_friendly_name(n):
        """Non-string / non-str-coercible falls back to empty. Clipped
        to 20 bytes (matches spec section 5.1)."""
        if not isinstance(n, str):
            return ""
        return n[:20]

    @staticmethod
    def _coerce_pair_win_s(v):
        """Clamp to [5, 255]; non-int falls back to 30 (spec default)."""
        try:
            n = int(v)
        except (TypeError, ValueError):
            return 30
        if n < 5:
            return 5
        if n > 255:
            return 255
        return n

    @staticmethod
    def _coerce_u8(v, default):
        try:
            n = int(v)
        except (TypeError, ValueError):
            return default
        if n < 0 or n > 255:
            return default
        return n

    @staticmethod
    def _coerce_u16(v, default):
        try:
            n = int(v)
        except (TypeError, ValueError):
            return default
        if n < 0 or n > 65535:
            return default
        return n

    def to_dict(self):
        return {
            "group": self.group,
            "channel": self.channel,
            "active_show": self.active_show,
            "mode": self.mode,
            "help_url": self.help_url,
            "debug_mode": self.debug_mode,
            "repeat": self.repeat,
            "friendly_name": self.friendly_name,
            "pair_win_s": self.pair_win_s,
            "led_power": self.led_power,
            "strip_chain": self.strip_chain,
            "strip_group_size": self.strip_group_size,
        }

    @classmethod
    def from_dict(cls, d):
        # Old on-disk settings that carry a `calm_mode` key are loaded
        # cleanly: d.get() ignores unknown keys, the field simply
        # disappears from the in-memory Settings (Epic 19). Missing
        # BLE fields fall back to constructor defaults (Epic 20 B5).
        if not isinstance(d, dict):
            return cls()
        return cls(
            group=d.get("group", 0),
            channel=d.get("channel", "auto"),
            active_show=d.get("active_show", ""),
            mode=d.get("mode", "lume"),
            help_url=d.get("help_url", DEFAULT_HELP_URL),
            debug_mode=d.get("debug_mode", False),
            repeat=d.get("repeat", "OFF"),
            friendly_name=d.get("friendly_name", ""),
            pair_win_s=d.get("pair_win_s", 30),
            led_power=d.get("led_power", 100),
            strip_chain=d.get("strip_chain", 12),
            strip_group_size=d.get("strip_group_size", 12),
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
            self.group == other.group
            and self.channel == other.channel
            and self.active_show == other.active_show
            and self.mode == other.mode
            and self.help_url == other.help_url
            and self.debug_mode == other.debug_mode
            and self.repeat == other.repeat
            and self.friendly_name == other.friendly_name
            and self.pair_win_s == other.pair_win_s
            and self.led_power == other.led_power
            and self.strip_chain == other.strip_chain
            and self.strip_group_size == other.strip_group_size
        )

    def __repr__(self):
        return (
            "Settings(group=%r, channel=%r, active_show=%r, "
            "mode=%r, help_url=%r, debug_mode=%r, repeat=%r, "
            "friendly_name=%r, pair_win_s=%r, led_power=%r, "
            "strip_chain=%r, strip_group_size=%r)"
            % (
                self.group,
                self.channel,
                self.active_show,
                self.mode,
                self.help_url,
                self.debug_mode,
                self.repeat,
                self.friendly_name,
                self.pair_win_s,
                self.led_power,
                self.strip_chain,
                self.strip_group_size,
            )
        )
