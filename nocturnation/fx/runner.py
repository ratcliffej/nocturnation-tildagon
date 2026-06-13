"""FX runner.

Bridges LIGHT_FX_RUN wire frames to the single-slot FX execution model.
The runner owns one `current_fx` at a time; a fresh LIGHT_FX_RUN
cancels the existing one (which may still be in its fade-out window)
and starts the new FX in parallel during the overlap.

Concurrent FX (layered overlay on background) is out of scope for
Epic 10 and reserved for a future epic via LIGHT_FX_RUN.flags bit 2.

The runner is hardware-free: it doesn't know about LEDs, screens, or
clocks. Concrete FX subclasses make their own render calls during
tick() against whatever surfaces the host wires up. The runner only
sequences start / tick / cancel / teardown.

Usage::

    runner = FxRunner(fx_registry, default_bpm=120)
    # in app.py _observe_frame, when a LIGHT_FX_RUN arrives:
    runner.on_fx_run_frame(frame, now_ms=time.ticks_ms())
    # in the render tick:
    runner.tick(now_ms=time.ticks_ms())
"""

from ..protocol.constants import FX_FLAG_START


class FxRunner:
    """Runs at most one FX (plus its release-fade tail) at a time.

    Attributes:
        registry (FxRegistry): where fx_id -> class lookups go.
        default_bpm (int): used when a LIGHT_FX_RUN.bpm == 0.

    State (read-only outside the class):
        current_fx: the active FX instance, or None.
        cancelling_fx: an FX that was superseded but is still ticking
            through its release tail. Goes to None once is_finished()
            returns True for it.
    """

    __slots__ = (
        "registry",
        "default_bpm",
        "current_fx",
        "cancelling_fx",
        # Diagnostics.
        "_unknown_fx_drops",
        "_runs_started",
        "_runs_cancelled",
    )

    def __init__(self, registry, default_bpm=120):
        self.registry = registry
        self.default_bpm = default_bpm
        self.current_fx = None
        self.cancelling_fx = None
        self._unknown_fx_drops = 0
        self._runs_started = 0
        self._runs_cancelled = 0

    # ------------------------------------------------------------------
    # Wire-frame admission

    def on_fx_run_frame(self, frame, now_ms):
        """Handle an incoming LIGHT_FX_RUN frame.

        Behaviour:
          - fx_id == 0: cancel any running FX. The cancelling FX may
            continue to tick through its release tail.
          - fx_id known and != current_fx.id, or flags.bit1 (replace-
            running) set: cancel current, start new.
          - fx_id known and == current_fx.id, no replace flag: ignore
            (let the existing run continue undisturbed).
          - fx_id unknown: drop silently, bump diagnostic counter.

        ``frame`` is expected to have the LIGHT_FX_RUN-specific slots
        populated (fx_id, bpm, buildup_s, flags, position_ms, params).
        """
        fx_id = frame.fx_id

        if fx_id == 0:
            self._begin_cancel(now_ms)
            return

        cls = self.registry.get(fx_id)
        if cls is None:
            self._unknown_fx_drops += 1
            return

        # Idempotency: same FX already running, no replace flag -> drop.
        if (self.current_fx is not None
                and self.current_fx.id == fx_id
                and not (frame.flags & 0x02)):   # FX_FLAG_REPLACE_RUNNING
            return

        # Cancel old (if any), start new. The old one may continue to
        # tick through its release tail in cancelling_fx until it
        # reports is_finished().
        self._begin_cancel(now_ms)
        bpm = frame.bpm if frame.bpm != 0 else self.default_bpm
        new_fx = cls()
        new_fx.start(
            bpm=bpm,
            buildup_s=frame.buildup_s,
            params=frame.params,
            position_ms=frame.position_ms,
            now_ms=now_ms,
        )
        self.current_fx = new_fx
        self._runs_started += 1

    # ------------------------------------------------------------------
    # Tick

    def tick(self, now_ms):
        """Advance both the current FX and any cancelling FX.

        Called from the host render loop at whatever cadence the host
        chooses (typically ~50 Hz).
        """
        if self.current_fx is not None:
            self.current_fx.tick(now_ms)
            if self.current_fx.is_finished(now_ms):
                # default_duration_ms expiry (or self-cancel)
                self.current_fx = None
        if self.cancelling_fx is not None:
            self.cancelling_fx.tick(now_ms)
            if self.cancelling_fx.is_finished(now_ms):
                self.cancelling_fx = None

    # ------------------------------------------------------------------
    # Diagnostics

    def stats(self):
        """Return a dict of counter values. Useful for status panels."""
        return {
            "current_fx_id":
                self.current_fx.id if self.current_fx is not None else 0,
            "cancelling_fx_id":
                self.cancelling_fx.id if self.cancelling_fx is not None else 0,
            "runs_started": self._runs_started,
            "runs_cancelled": self._runs_cancelled,
            "unknown_fx_drops": self._unknown_fx_drops,
        }

    # ------------------------------------------------------------------
    # Internal

    def _begin_cancel(self, now_ms):
        """Move current_fx -> cancelling_fx and call its cancel().

        If cancelling_fx was already non-None (a rapid succession of
        FX changes), drop the older cancelling one - we only track
        one release tail at a time. In practice fade-outs are short
        enough that this is rarely a concern.
        """
        if self.current_fx is None:
            return
        self.current_fx.cancel(now_ms)
        # Drop any older cancelling FX (its release tail loses out).
        self.cancelling_fx = self.current_fx
        self.current_fx = None
        self._runs_cancelled += 1
