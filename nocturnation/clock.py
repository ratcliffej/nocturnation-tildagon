"""time.ticks_diff() shim.

MicroPython's time.ticks_ms() returns a small positive integer that
wraps at 30 bits (~12.4 days on ESP32-style ports); a plain
``later - earlier`` subtraction across a wrap returns a huge negative
that breaks rate-limit and timeout checks. time.ticks_diff(later,
earlier) handles the wrap correctly.

On CPython (host pytest), time.ticks_diff does not exist; the host
clock is a regular integer with no wrap concern, so the shim falls
back to plain subtraction. Tests that pass plain ints as now_ms see
the identical result.

Usage:

    from nocturnation.clock import ticks_diff
    if ticks_diff(now_ms, then_ms) >= interval_ms:
        ...

instead of the wrap-unsafe ``(now_ms - then_ms) >= interval_ms``.
"""

try:
    from time import ticks_diff as _native_ticks_diff
except ImportError:  # pragma: no cover - CPython host path
    _native_ticks_diff = None

if _native_ticks_diff is None:
    def ticks_diff(later, earlier):
        return later - earlier
else:  # pragma: no cover - MicroPython badge path
    ticks_diff = _native_ticks_diff
