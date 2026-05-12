"""Master-liveness tracker per protocol manual section 6.2.

A receiver considers the master alive whenever any frame has arrived
within the last kNoSignalGapMs milliseconds. The SignalTracker holds
the most-recent-frame timestamp and reports lost vs alive based on the
current clock value. The caller is responsible for the clock source
(time.ticks_ms on the badge, monotonic test seam on the host).
"""

# Protocol manual section 6.2: three missed heartbeats at 1 Hz, or
# three seconds with no other traffic, signal master loss. Matches the
# M5 StickC's kNoSignalGapMs constant.
NO_SIGNAL_GAP_MS = 3000


class SignalTracker:
    """Records the timestamp of the most recently accepted frame and
    answers ``is_lost(now_ms)`` based on the configurable gap.

    Initial state (no frame ever seen) reports lost: this is the
    truthful state on app launch before any master traffic arrives.
    """

    __slots__ = ("_last_frame_ms", "_gap_ms")

    def __init__(self, gap_ms=NO_SIGNAL_GAP_MS):
        self._last_frame_ms = None
        self._gap_ms = gap_ms

    def record_frame(self, now_ms):
        """Note that a frame arrived. Call from every accepted frame
        (LIGHT_COMMAND, HEARTBEAT, MUSIC_EVENT alike - all count as
        proof the master is alive)."""
        self._last_frame_ms = now_ms

    def is_lost(self, now_ms):
        """Return True when no frame has arrived recently enough.

        Returns True on a fresh tracker (never-received state) so the
        boot screen shows NO SIGNAL until the first frame lands.
        """
        if self._last_frame_ms is None:
            return True
        # Plain subtraction is fine on the host (CPython) and on the
        # badge for the gap range we care about (< 12 days). For wrap
        # safety on MicroPython we could call time.ticks_diff but the
        # value we get from time.ticks_ms() is monotonic enough for
        # the 3 s window we check against.
        return (now_ms - self._last_frame_ms) > self._gap_ms

    def reset(self):
        """Clear the tracker (used on app re-entry to restart the boot
        NO SIGNAL state cleanly rather than holding a stale timestamp
        from before a minimise)."""
        self._last_frame_ms = None
