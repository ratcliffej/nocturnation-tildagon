"""DirID-keyed background images for the Lume LCD (Epic 13 Phase 2A).

A Director's persisted Performance-range source_id (0x40..0xFE) is a
stable knowable identity per Director - random on first install,
sticky thereafter, operator-settable via the Stick's Config menu's
hex editor. That stability is what makes it useful as a *content*
key: a Tildagon show can pick which background logo to render based
on which Director its TOFU lock named. Stage D's Director runs with
DirID 0xD0; the badge sees that id on every accepted frame, and the
LCD renders ``images/dirid_d0.jpg`` as a background layer.

Storage convention: each image lives at
``nocturnation/images/dirid_<hex>.jpg`` (lowercase hex, no ``0x``
prefix, exactly two digits, e.g. ``dirid_d0.jpg``). JPEG is the
documented Ctx-supported format on the Tildagon - see the badge
docs at https://tildagon.badge.emfcamp.org/tildagon-apps/reference/ctx/
for the canonical API surface. Authoring is "save as JPG at 240x240"
in any image editor; no firmware-side conversion step needed.

Fallback: an unknown DirID OR no current lock falls back to
``nocturnation/images/default.jpg`` (bundled NocturNation logo). If
neither file exists the loader returns ``None`` and the renderer
falls back to the solid wash background.

The loader is intentionally tiny: it just resolves DirID to a
filesystem path. The actual file decode + caching happens inside
Ctx's ``ctx.image(path, ...)`` call, which the renderer invokes
every frame with the same path - Ctx caches internally by path so
the file isn't re-decoded each tick.

This module replaced an earlier RGB565-binary-blob approach that
relied on the ``ctx.texture()`` API. That API hard-faulted the
chip on first call on this badge build; switching to the
documented ``ctx.image()`` path side-stepped the issue.
"""


def _dirname(path):
    """Manual ``os.path.dirname`` equivalent.

    MicroPython on the Tildagon doesn't ship ``os.path`` (it's a
    CPython convenience module not present in standard MicroPython
    embedded builds; first attempt to use it crashed the app at
    import). String-slicing on the last "/" matches CPython
    behaviour for the cases we hit (absolute paths from __file__).
    """
    idx = path.rfind("/")
    if idx < 0:
        return "."
    if idx == 0:
        return "/"
    return path[:idx]


# Directory containing the image files. The loader lives at
# ``nocturnation/images/__init__.py`` (the .jpg files are package
# data sitting alongside this file), so the image directory IS the
# directory containing __file__.
_THIS_DIR = _dirname(__file__) if "__file__" in globals() else "."
_IMAGE_DIR = _THIS_DIR

DEFAULT_FILENAME = "default.jpg"
IMAGE_EXTENSION  = ".jpg"


class _State:
    """Module-level singleton (MicroPython has no class properties)."""
    current_path = None      # path to the active image, or None (no file found)
    current_dir_id = None    # int 0..255 of the locked DirID at last load
    has_cache = False        # distinguishes "never called" from "cached None"
    last_load_failed_path = None   # cache to avoid retry-spam on missing files


_state = _State()


def _filename_for_dir_id(dir_id):
    """Filename convention for a given DirID byte (no path).

    ``0xD0`` -> ``"dirid_d0.jpg"``. Lowercase hex, no prefix,
    exactly two digits. Out-of-range / None returns None - caller
    falls back to the default.
    """
    if dir_id is None:
        return None
    if not (0x00 <= dir_id <= 0xFF):
        return None
    return "dirid_%02x%s" % (dir_id, IMAGE_EXTENSION)


def _file_exists(path):
    """Cheap existence check via open()-and-close.

    MicroPython doesn't have ``os.path.exists`` and ``os.stat`` is
    available but its exception types vary across builds. ``open()``
    raises OSError on any failure path (missing, permission, IO);
    we treat all of those as "no file".
    """
    try:
        f = open(path, "rb")
    except OSError:
        if path != _state.last_load_failed_path:
            print("[nocturnation.images] not found: %s" % path)
            _state.last_load_failed_path = path
        return False
    f.close()
    return True


def path_for_dir_id(dir_id):
    """Resolve ``dir_id`` to a renderable file path.

    Lookup:

      1. If ``dir_id`` is not None and ``dirid_<hex>.jpg`` exists,
         return its absolute path.
      2. Else if ``default.jpg`` exists, return that path.
      3. Else return ``None`` (caller falls back to solid colour).

    The path is cached in the module singleton; subsequent calls
    with the same ``dir_id`` skip the filesystem check entirely and
    return the cached path. A different ``dir_id`` re-resolves
    (because the user has TOFU-re-locked to a different Director).
    """
    if _state.has_cache and dir_id == _state.current_dir_id:
        return _state.current_path

    chosen = None

    fname = _filename_for_dir_id(dir_id)
    if fname is not None:
        candidate = _IMAGE_DIR + "/" + fname
        if _file_exists(candidate):
            chosen = candidate

    if chosen is None:
        default_path = _IMAGE_DIR + "/" + DEFAULT_FILENAME
        if _file_exists(default_path):
            chosen = default_path

    _state.current_path   = chosen
    _state.current_dir_id = dir_id
    _state.has_cache      = True
    if chosen is not None:
        print("[nocturnation.images] resolved %s (DirID=%s)"
              % (chosen, ("0x%02X" % dir_id) if dir_id is not None else "none"))
    return chosen


def current_path():
    """Return the cached path, or None if no image is resolvable."""
    return _state.current_path


def clear():
    """Drop the cached path - used on Lume mode exit / reset."""
    _state.current_path   = None
    _state.current_dir_id = None
    _state.has_cache      = False
