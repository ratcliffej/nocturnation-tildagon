"""Trust-On-First-Use lock on Director source_id (Epic 5.5 B6).

A Lume locks to the source_id of the first valid frame it sees after
scan or rescan, then ignores frames from any other source_id for the
session. The lock clears on:

* heartbeat timeout (no frames from the locked source for ``timeout_ms``,
  default ten seconds shared with the channel-rescan threshold)
* explicit ``clear()`` call (operator "Rescan" menu, channel change,
  reboot - all expressed through the same hook)

Cross-range filter (spec §3.4): on channel 11, only Performance-range
source_ids (``0x40``-``0xFE``) are eligible to be locked; community-range
IDs on channel 11 are silently dropped. Channels 1 and 6 accept any
non-broadcast source_id.

The spec's literal "first valid HEARTBEAT" was relaxed during B6 to
"first valid frame": the Director's heartbeat is suppressed by
skip-if-recent during active music, so a Lume joining mid-song would
otherwise sit idle for the duration of the song. Locking on any valid
frame matches the §6.2 liveness model.

Pure logic: no I/O, no clock; the caller injects ``now_ms`` and the
current channel. Unit-tested host-side; runs unchanged on the badge.
"""

from .protocol import MessageType
from .protocol.source_id import SourceId, is_performance_range


DEFAULT_TIMEOUT_MS = 10000   # mirrors the M5 firmware's kRescanMs


class TofuLock:
    """TOFU state for a single Lume instance.

    Construct once at app boot. Call ``admit(frame, channel, now_ms)``
    after the dedup ring has accepted a frame; the return value tells
    the caller whether to dispatch to renderers. Call ``tick(now_ms)``
    from the main loop to expire the lock on inactivity. Call
    ``clear()`` to manually unlock (operator-initiated rescan).
    """

    __slots__ = ("_locked_id", "_last_frame_ms", "_timeout_ms")

    def __init__(self, timeout_ms=DEFAULT_TIMEOUT_MS):
        self._locked_id     = None
        self._last_frame_ms = None
        self._timeout_ms    = timeout_ms

    # ---------- observers ----------

    @property
    def locked_id(self):
        """The source_id currently locked, or ``None`` if no lock held."""
        return self._locked_id

    def is_locked(self):
        return self._locked_id is not None

    # ---------- decision ----------

    def admit(self, frame, channel, now_ms):
        """Decide whether ``frame`` should be dispatched to renderers.

        Returns ``True`` if the frame passes TOFU + cross-range checks
        (caller proceeds with normal observation / rendering). Returns
        ``False`` if it must be silently dropped. Side effects:

        * Establishes the lock on the first eligible frame.
        * Updates the "last frame from locked source" timestamp on
          every admitted frame, deferring the timeout expiry.
        """
        # Broadcast / wildcard source_id is never a valid Director peer.
        # (A Director never emits 0xFF as its own id; broadcast is for
        # senders that intentionally identify as anonymous, which a
        # Lume MUST NOT lock to.)
        if frame.source_id == SourceId.BROADCAST:
            return False

        # Cross-range filter on channel 11: only Performance-range IDs
        # are eligible. A community-range id on ch 11 is a misconfigured
        # Director announcing on the wrong channel; silently drop.
        if channel == 11 and not is_performance_range(frame.source_id):
            return False

        if self._locked_id is None:
            # First eligible frame establishes the lock.
            self._locked_id     = frame.source_id
            self._last_frame_ms = now_ms
            return True

        # Already locked: only frames from the locked source proceed.
        if frame.source_id != self._locked_id:
            return False

        self._last_frame_ms = now_ms
        return True

    # ---------- lifecycle ----------

    def tick(self, now_ms):
        """Expire the lock on inactivity.

        Returns ``True`` if the lock just expired (caller can use this
        to surface a status change or trigger a rescan). Returns
        ``False`` otherwise.

        Idempotent: calling ``tick`` repeatedly past the timeout
        returns ``True`` once and ``False`` after, because the lock
        is already cleared.
        """
        if self._locked_id is None or self._last_frame_ms is None:
            return False
        # ticks_ms wraps at 30 bits on MicroPython, but the wrap window
        # (~12.4 days) is far beyond any reasonable lock duration so a
        # plain subtraction is fine in practice.
        if now_ms - self._last_frame_ms >= self._timeout_ms:
            self._locked_id     = None
            self._last_frame_ms = None
            return True
        return False

    def clear(self):
        """Explicit unlock. Resets to scan state.

        Used for operator-initiated "Rescan" menu actions, channel
        changes, and similar discontinuities.
        """
        self._locked_id     = None
        self._last_frame_ms = None
