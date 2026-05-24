"""ShowRegistry + auto-discovery of concrete Shows.

Concrete Shows live in `shows/<show_id>/` at the repo root -
sibling to `app.py`, separate from the framework code here. Each
Show is a directory containing at least `__init__.py` exposing a
`make_show()` factory.

Discovery happens at framework startup:
  `discover_shows()` walks the `shows/` directory, imports each
  subpackage, calls `make_show()`, and registers the returned
  instance with the singleton `show_registry()`.

This file lives in `nocturnation/shows/` (the framework package)
and reads from the top-level `shows/` package (the concrete-show
library). Both directories sit at the repo root for clarity:

    <repo root> (installed to /apps/<dir>/ on the badge)
      app.py                  <- entry point
      nocturnation/shows/     <- framework (this module)
      shows/                  <- concrete Show library

The convention `make_show()` -> Show instance keeps the registry
contract narrow. A future migration to a metaclass-based
registration is possible if Show authors prefer a decorator
pattern; today's surface is the minimum that works on MicroPython
without surprises.
"""

import os


class ShowRegistry:
    """Holds Show instances in registration order.

    Registration order matters in one place: the picker UI lists
    Shows in registration order, so the directory iteration order
    becomes the picker's cursor-start default. `discover_shows()`
    sorts alphabetically to keep the order stable across builds.
    """

    def __init__(self):
        self._shows = []
        self._by_id = {}

    def register(self, show):
        sid = show.id()
        if sid in self._by_id:
            raise ValueError("Duplicate Show id: %r" % sid)
        self._shows.append(show)
        self._by_id[sid] = show
        return show

    def find(self, show_id):
        return self._by_id.get(show_id)

    def all(self):
        return tuple(self._shows)

    def __len__(self):
        return len(self._shows)

    def __iter__(self):
        return iter(self._shows)

    def __contains__(self, show_id):
        return show_id in self._by_id

    def clear(self):
        """Remove all registered Shows. Intended for test fixtures;
        the singleton registry should not be cleared at runtime."""
        self._shows = []
        self._by_id = {}


_SINGLETON = ShowRegistry()


def show_registry():
    """Return the process-wide Show registry singleton."""
    return _SINGLETON


def discover_shows(shows_root="shows", registry=None):
    """Walk `shows_root` and register every Show found.

    For each subdirectory of `shows_root` containing an
    `__init__.py`, import the subpackage and call `make_show()`
    to get a Show instance, then register it.

    `shows_root` is a Python import path (default `"shows"`,
    which on the Tildagon resolves to the installed app dir's
    `shows/` - app.py puts that dir on sys.path). On the host
    pytest environment the same import path resolves through
    pyproject.toml's `pythonpath` entry.

    Skips:
      - subdirectories without an `__init__.py`
      - subdirectories whose name starts with `_` or `.`
      - subpackages without a module-level `make_show` attribute
        (logged-only; no crash, so a malformed Show doesn't take
        the whole picker out)

    Returns the count of Shows registered.
    """
    if registry is None:
        registry = show_registry()

    # Resolve the filesystem path by importing the shows root and
    # asking for its __path__. This avoids hard-coding "/apps/...".
    try:
        root_pkg = __import__(shows_root)
    except ImportError:
        return 0

    # __path__ may be a list-like or a single path string depending
    # on MicroPython's namespace package handling. Handle both.
    raw_path = getattr(root_pkg, "__path__", None)
    if raw_path is None:
        return 0
    if isinstance(raw_path, str):
        root_dir = raw_path
    else:
        try:
            root_dir = raw_path[0]
        except (IndexError, TypeError):
            return 0

    count = 0
    try:
        entries = sorted(os.listdir(root_dir))
    except OSError:
        return 0

    for name in entries:
        if name.startswith("_") or name.startswith("."):
            continue
        sub_path = root_dir + "/" + name
        try:
            init_stat = os.stat(sub_path + "/__init__.py")
        except OSError:
            continue
        _ = init_stat  # unused; presence is the signal
        module_name = shows_root + "." + name
        try:
            mod = __import__(module_name)
            # __import__("a.b") returns the top-level package; walk
            # down to the leaf module.
            for part in module_name.split(".")[1:]:
                mod = getattr(mod, part)
        except (ImportError, AttributeError):
            continue
        factory = getattr(mod, "make_show", None)
        if factory is None:
            continue
        try:
            show = factory()
        except Exception:
            continue
        if show is None:
            continue
        try:
            registry.register(show)
            count += 1
        except ValueError:
            # Duplicate id; skip rather than crashing the framework.
            continue
    return count
