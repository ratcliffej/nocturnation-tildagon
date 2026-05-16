"""Channel auto-scan state machine.

Implements the scan-order and lock semantics from protocol manual section
5.3. Channel 11 (show) is tried first, then channel 1 (hobby), each for
``listen_ms`` milliseconds; the first channel that produces a valid
NocturNation frame is locked and receive proceeds there. Channel 6 is
NOT auto-scanned per the protocol - a Lume on channel 6 must be locked
explicitly.

This module is the state machine only. Actual radio operations (set
channel, attempt receive) are caller responsibilities and differ between
real hardware, the Tildagon simulator, and host tests.
"""

# Default listen window per channel during scan.
DEFAULT_LISTEN_MS = 2000

# Channel priority order. Channel 11 first because it is the suggested
# show channel and presumed higher priority.
SCAN_ORDER = (11, 1)


class ChannelScanner:
    """Tracks the current scan channel and the locked-after-receive state.

    Usage:
        s = ChannelScanner()
        while not s.is_locked:
            radio.set_channel(s.current_channel)
            buf = radio.recv(timeout_ms=s.listen_ms)
            if buf is not None and protocol_validates(buf):
                s.lock()
                break
            s.advance()
        # ... main receive loop on s.current_channel
    """

    __slots__ = ("_order", "_listen_ms", "_index", "_locked")

    def __init__(self, order=SCAN_ORDER, listen_ms=DEFAULT_LISTEN_MS):
        self._order = tuple(order)
        if not self._order:
            raise ValueError("scan order must not be empty")
        self._listen_ms = listen_ms
        self._index = 0
        self._locked = None

    @property
    def listen_ms(self):
        return self._listen_ms

    @property
    def current_channel(self):
        if self._locked is not None:
            return self._locked
        return self._order[self._index]

    @property
    def is_locked(self):
        return self._locked is not None

    def advance(self):
        """Rotate to the next channel in scan order. Returns the new channel.

        Raises RuntimeError if the scanner is already locked (advancing
        after lock is a programmer error: the receive loop should be
        running on the locked channel, not scanning).
        """
        if self._locked is not None:
            raise RuntimeError("cannot advance after lock")
        self._index = (self._index + 1) % len(self._order)
        return self.current_channel

    def lock(self, channel=None):
        """Pin to the current (or specified) channel. Returns it.

        ``channel=None`` locks to whatever ``current_channel`` is now -
        the natural call when a valid frame just arrived on the channel
        being scanned. Pass an explicit channel to lock to a specific
        operator-configured value.
        """
        if channel is None:
            channel = self.current_channel
        if channel not in self._order:
            raise ValueError("channel not in scan order")
        self._locked = channel
        return channel

    def unlock(self):
        """Drop the lock and resume scanning from the head of scan_order.

        Called on a NO-SIGNAL timeout (Director traffic absent for > 3 s
        per protocol manual section 6.2) when the operator originally
        wanted auto-scan. A locked-by-config channel (set explicitly via
        operator preference rather than auto-scan) should not be unlocked
        on timeout - the caller decides.
        """
        self._locked = None
        self._index = 0
