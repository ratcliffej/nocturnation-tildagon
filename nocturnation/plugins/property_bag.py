"""PropertyBag - per-plug-in property storage with JSON persistence.

Mirrors `nocturnation::plugins::PropertyBag` on M5 (which uses
ESP-IDF Preferences for NVS-backed storage under namespace
`ns_<plugin_id>`). Here we use a single JSON file at
`DEFAULT_PATH` with one section per plug-in id, keyed by the
plug-in's `id()`.

Why one shared file rather than one-per-plug-in: the Tildagon
filesystem has a small inode budget and the property surface is
small; one file is simpler to atomically rewrite and matches the
shape of the existing `nocturnation_settings.json` neighbour.

The file lives outside `/apps/` so `deploy.sh`'s wipe-and-copy of
the app dir doesn't clobber operator-tuned property values.
"""

import json


DEFAULT_PATH = "/nocturnation_plugins.json"


class PropertyBag:
    """Per-plug-in property storage.

    Construct with a Plugin (or anything that exposes `id()` and
    `properties()`) and the bag picks up the plug-in's schema. Values
    are loaded from `path` lazily on the first get; writes flush to
    disk synchronously (the property-write rate is operator-driven,
    not high-frequency).
    """

    __slots__ = ("_plugin_id", "_defs", "_values", "_path", "_loaded")

    def __init__(self, plugin, path=DEFAULT_PATH):
        self._plugin_id = plugin.id()
        self._defs = {p.key: p for p in plugin.properties()}
        self._values = {}
        self._path = path
        self._loaded = False

    def _ensure_loaded(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(self._path, "r") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        section = data.get(self._plugin_id, {}) if isinstance(data, dict) else {}
        for key, pd in self._defs.items():
            stored = section.get(key)
            if stored is None:
                self._values[key] = pd.default_value
            else:
                self._values[key] = pd.clamp(stored)

    def get(self, key):
        """Get the current value for `key`. Returns the default if
        the key is in the schema but has never been set; raises
        KeyError if the key isn't in the schema at all."""
        self._ensure_loaded()
        if key not in self._defs:
            raise KeyError("Property %r not declared in plug-in %r schema" % (key, self._plugin_id))
        return self._values.get(key, self._defs[key].default_value)

    def set(self, key, value):
        """Set `key` to `value` (clamped to the schema's bounds) and
        flush to disk. Returns the value actually stored."""
        self._ensure_loaded()
        if key not in self._defs:
            raise KeyError("Property %r not declared in plug-in %r schema" % (key, self._plugin_id))
        clamped = self._defs[key].clamp(value)
        self._values[key] = clamped
        self._flush()
        return clamped

    def items(self):
        """Iterate (key, value) for every property in the schema."""
        self._ensure_loaded()
        return ((k, self._values.get(k, self._defs[k].default_value)) for k in self._defs)

    def _flush(self):
        try:
            with open(self._path, "r") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data[self._plugin_id] = dict(self._values)
        try:
            with open(self._path, "w") as f:
                json.dump(data, f)
        except OSError:
            # Best-effort persistence: a failed write does not crash
            # the show. The in-memory value remains correct for the
            # rest of the session.
            pass
