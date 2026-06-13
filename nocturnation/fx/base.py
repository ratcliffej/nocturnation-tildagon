"""FX base class.

Each concrete FX subclasses Fx, sets the class-level metadata (id, name,
category), and implements start / tick / cancel / is_finished. The
runner instantiates one FX at a time and calls these methods.

Implemented without ABC because MicroPython's trimmed stdlib drops
abc; methods raise NotImplementedError as the contract instead.
"""


class Fx:
    """Base class for FX implementations.

    Subclass attributes:
        id (int): 1..254 unique identifier matching the wire fx_id.
        name (str): human-readable label for diagnostics / manifest.
        category (str): one of 'ambient', 'beat', 'buildup', 'drop',
            'transition'. Convention only - the runner doesn't gate on
            category.
        default_duration_ms (int | None): if non-None and no cancel
            arrives by this time after start, is_finished() returns
            True automatically. None = runs indefinitely until
            cancelled by a new LIGHT_FX_RUN(fx_id=0) or fx_id change.

    Lifecycle (called by FxRunner):
        start(...)        - once at admission
        tick(now_ms)      - every render frame
        cancel(now_ms)    - once when superseded by another FX or
                            an explicit cancel; tick() continues
                            firing until is_finished() returns True
                            (to support fade-out release windows).
        is_finished(now_ms) - the runner stops ticking once True.
    """

    # Subclasses MUST override these three.
    id = 0
    name = ""
    category = ""

    # Optional auto-finish. None = no automatic end.
    default_duration_ms = None

    def __init__(self):
        # Subclasses populate state in start(); base class only sets
        # the bookkeeping fields the runner consults.
        self._started_ms = None
        self._cancelled_ms = None

    def start(self, *, bpm, buildup_s, params, position_ms, now_ms):
        """Initialise FX state. Called once after admission.

        Args:
            bpm (int): 0 = use receiver's stored default, else override.
            buildup_s (int): seconds of buildup ramp before reaching
                full intensity / probability.
            params (tuple[int, int, int, int, int, int]): six u8s
                whose meaning is FX-specific; see the FX library doc
                (Docs/fx-library.md).
            position_ms (int): offset into the FX timeline. Non-zero
                for late-join: the FX should start as if it had
                already run for position_ms milliseconds.
            now_ms (int): the wall-clock 'now' to use as the
                reference. Subsequent tick() calls pass the same
                clock source.
        """
        self._started_ms = now_ms
        self._cancelled_ms = None
        raise NotImplementedError("Fx.start must be overridden")

    def tick(self, now_ms):
        """Advance one frame. Emit any render calls.

        Continues to be called between cancel() and is_finished() so
        the FX can drive a release fade.
        """
        raise NotImplementedError("Fx.tick must be overridden")

    def cancel(self, now_ms):
        """Begin release / fade-out. The runner keeps ticking the FX
        until is_finished() returns True; this lets the FX shape its
        own exit (a fade-out, a snap-off, whatever)."""
        self._cancelled_ms = now_ms
        # Default: subclasses may override for graceful release.
        # The base behaviour is "no exit envelope" - is_finished()
        # returns True immediately after cancel().

    def is_finished(self, now_ms):
        """True when the FX should be torn down. Default behaviour:
        finished as soon as it's been cancelled, or after
        default_duration_ms if set."""
        if self._cancelled_ms is not None:
            return True
        if (self.default_duration_ms is not None
                and self._started_ms is not None
                and (now_ms - self._started_ms) >= self.default_duration_ms):
            return True
        return False
