"""DirID-keyed background images for the Lume LCD (Epic 13 Phase 2A).

A Director's persisted Performance-range source_id (0x40..0xFE) is a
stable knowable identity per Director - random on first install,
sticky thereafter, operator-settable via the Stick's Config menu's
hex editor. That stability is what makes it useful as a *content*
key: a Tildagon show can pick which background logo to render based
on which Director its TOFU lock named. Stage D's Director runs with
DirID 0xD0; the badge sees that id on every accepted frame, and the
LCD renders ``images/dirid_d0.raw`` as a background layer.

Storage convention: each pre-rendered image lives at
``nocturnation/images/dirid_<hex>.raw`` (lowercase hex, no ``0x``
prefix, exactly two digits, e.g. ``dirid_d0.raw``). The file is a
flat little-endian RGB565 binary blob at the configured display
dimensions (default 240 x 240 = 115200 bytes per image; see
``png_to_rgb565.py`` in the docs repo for offline authoring).

Fallback: an unknown DirID OR no current lock falls back to
``nocturnation/images/default.raw`` (bundled NocturNation logo, always
present in the firmware deploy). Missing default file is treated as
"no background image", which the renderer handles by drawing the
existing solid wash colour.

The loader is intentionally tiny - one cached image at a time, on-
demand load via ``load_for_dirid()``. ``current()`` returns the bytes
+ dimensions to render this tick (or ``None`` if no image to draw).

Memory note: each 240x240 image is ~115 KB. We keep only the active
image in RAM, not a cache, because the badge is RAM-constrained and
TOFU lock changes are infrequent (10 s scale, not 100 ms scale).
"""

# We avoid micropython-only imports here so the module is host-
# importable for unit tests. ``os`` for path existence is in both
# CPython and MicroPython.
try:
    import uos as os  # type: ignore[import-not-found]
except ImportError:
    import os  # type: ignore[no-redef]


# Display dimensions of the Tildagon's framebuffer-addressable area.
# The bezel masks the corners, but the framebuffer is square and the
# image author can leave safe-zone padding so important content
# centres within the visible circle.
DISPLAY_W = 240
DISPLAY_H = 240
BYTES_PER_PIXEL = 2   # RGB565 = 2 bytes/pixel
EXPECTED_SIZE = DISPLAY_W * DISPLAY_H * BYTES_PER_PIXEL

# Directory containing the .raw blobs. Resolved at module-import time
# relative to this file so the firmware bundle layout is preserved.
_THIS_DIR = os.path.dirname(__file__) if "__file__" in globals() else "."
_IMAGE_DIR = _THIS_DIR + "/images"

DEFAULT_FILENAME = "default.raw"


class _State:
    """Module-level singleton (MicroPython has no class properties)."""
    current_buf = None       # bytes object of the active image, or None
    current_dir_id = None    # int 0..255 of the locked DirID, or None
    current_path = None      # absolute path that produced current_buf
    last_load_failed_path = None   # cache to avoid retry-spam on missing files


_state = _State()


def _filename_for_dir_id(dir_id):
    """Filename convention for a given DirID byte (no path).

    ``0xD0`` -> ``"dirid_d0.raw"``. Lowercase hex, no prefix, exactly
    two digits. Out-of-range inputs are treated as "no specific
    image", returning None (caller falls back to the default).
    """
    if dir_id is None:
        return None
    if not (0x00 <= dir_id <= 0xFF):
        return None
    return "dirid_%02x.raw" % dir_id


def _read_image(path):
    """Open + size-validate an image file.

    Returns the bytes object on success, ``None`` on any failure
    (missing file, wrong size, IO error). Logs the failure once per
    distinct path so a repeatedly-tried-and-missing default doesn't
    spam the console.
    """
    try:
        with open(path, "rb") as f:
            buf = f.read()
    except OSError:
        if path != _state.last_load_failed_path:
            print("[nocturnation.images] not found: %s" % path)
            _state.last_load_failed_path = path
        return None
    if len(buf) != EXPECTED_SIZE:
        # Wrong size = wrong format. Refuse to render rather than
        # producing scrambled output. The encoder enforces size on
        # write, so this should only fire if the file is hand-edited
        # or transferred with a content-mangling tool.
        print(
            "[nocturnation.images] %s: bad size %d (expected %d); skipping"
            % (path, len(buf), EXPECTED_SIZE)
        )
        return None
    return buf


def load_for_dir_id(dir_id):
    """Load the image for ``dir_id``, falling back to the default.

    ``dir_id``: int 0x00..0xFF (the Performance-range Director id, or
    ``None`` if no lock is held). The lookup is:

      1. If ``dir_id`` is not None and ``dirid_<hex>.raw`` exists,
         use it.
      2. Otherwise fall back to ``default.raw``.
      3. If neither exists, return None - the renderer should fall
         back to the solid-colour background layer.

    Caches the loaded buffer in the module singleton; subsequent calls
    with the same ``dir_id`` are no-ops and return the cached bytes.
    Returns the bytes object (or ``None`` if no image is renderable).
    """
    # Cache hit: same DirID as last call.
    if dir_id == _state.current_dir_id and _state.current_buf is not None:
        return _state.current_buf

    # Try the DirID-specific file first.
    buf = None
    chosen_path = None
    fname = _filename_for_dir_id(dir_id)
    if fname is not None:
        candidate = _IMAGE_DIR + "/" + fname
        buf = _read_image(candidate)
        if buf is not None:
            chosen_path = candidate

    # Fall back to default.
    if buf is None:
        default_path = _IMAGE_DIR + "/" + DEFAULT_FILENAME
        buf = _read_image(default_path)
        if buf is not None:
            chosen_path = default_path

    # Update cache (even on None to skip re-trying every tick).
    _state.current_buf    = buf
    _state.current_dir_id = dir_id
    _state.current_path   = chosen_path
    if buf is not None:
        print("[nocturnation.images] loaded %s (DirID=%s)"
              % (chosen_path, ("0x%02X" % dir_id) if dir_id is not None else "none"))
    return buf


def current():
    """Return the active background image as ``(buf, w, h, stride)``.

    Returns the cached image. If no image is loaded (or the last load
    failed), returns ``(None, 0, 0, 0)`` - the caller should fall back
    to the solid-colour background layer.

    Pair with ``ctx.texture(buf, ctx.FORMAT_RGB565, w, h, stride)``
    followed by ``ctx.rectangle(...).fill()`` in the render loop.
    """
    if _state.current_buf is None:
        return (None, 0, 0, 0)
    return (_state.current_buf, DISPLAY_W, DISPLAY_H, DISPLAY_W * BYTES_PER_PIXEL)


def clear():
    """Drop the cached image - used on Lume mode exit / reset."""
    _state.current_buf    = None
    _state.current_dir_id = None
    _state.current_path   = None
