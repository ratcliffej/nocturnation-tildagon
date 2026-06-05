# SPDX-License-Identifier: MIT
"""Enttec DMX USB Pro framing parser - MicroPython port of the StickC
B1 parser.

Consumes a byte stream (typically from USB-CDC) and emits complete
DMX frames as 512-byte payloads. Pure-Python state machine, no
MicroPython-specific deps - runs unchanged on host pytest and on the
badge's MicroPython interpreter.

Wire format (Enttec DMX USB Pro Output Only):

    offset  bytes  field
    0       1      0x7E             start byte
    1       1      label            (0x06 = Output Only Send DMX Packet)
    2-3     2      length           little-endian, INCLUDES start code byte
    4       1      DMX start code   (0x00 for standard DMX-512)
    5..     N-1    channel data     N-1 channel bytes
    end     1      0xE7             end byte

The parser feeds one byte at a time and returns a result enum: bytes
that don't complete a frame return ``NEED_MORE_BYTES``; the byte that
closes a valid frame returns ``FRAME_COMPLETE`` with the frame
accessible via ``last_payload()``. Garbage / desync resets the parser
to the WAITING_FOR_START state, signalled by ``RESET``.
"""

# Result codes - small ints, MicroPython-friendly.
NEED_MORE_BYTES = 0
FRAME_COMPLETE = 1
RESET = 2

_START = 0x7E
_END = 0xE7

# Match the StickC parser's safety cap. Enttec Pro's length field is
# 16-bit but in practice nothing legitimate exceeds a full DMX universe
# plus the start code byte (513). Larger frames are dropped as garbage.
_MAX_PAYLOAD = 600


class DmxParser:
    """Byte-at-a-time Enttec Pro framing parser.

    Usage:
        p = DmxParser()
        for b in incoming_bytes:
            r = p.feed_byte(b)
            if r == FRAME_COMPLETE and p.last_label() == 0x06:
                channels = p.last_payload()    # 512 bytes (start code stripped)
                # ... dispatch ...
    """

    # State machine states.
    _S_WAIT_START = 0
    _S_LABEL = 1
    _S_LEN_LO = 2
    _S_LEN_HI = 3
    _S_PAYLOAD = 4
    _S_END = 5

    def __init__(self):
        self._state = self._S_WAIT_START
        self._label = 0
        self._payload_len = 0      # length field value (includes start code)
        self._payload_idx = 0
        # Pre-allocate a single max-size buffer to avoid per-frame
        # allocation churn. MicroPython on the Tildagon GC-thrashes
        # if we allocate a fresh 513-byte bytearray every frame at 44
        # fps; with this pre-allocation the parser does zero heap
        # work in the steady state.
        self._buf = bytearray(_MAX_PAYLOAD)
        self._last_label = 0
        self._last_payload = b""   # excludes the start code byte
        self._frames_complete = 0
        self._frames_dropped = 0

    def feed_byte(self, b):
        """Feed one byte; return one of NEED_MORE_BYTES / FRAME_COMPLETE
        / RESET.

        FRAME_COMPLETE means a well-formed Enttec frame just ended;
        last_label() + last_payload() are valid.
        RESET means the parser hit a garbage byte and re-anchored on a
        fresh 0x7E; the previous in-progress frame (if any) was dropped.
        """
        s = self._state

        # Mid-HEADER 0x7E means the previous frame was interrupted while
        # we were still reading the header bytes (label / length); the
        # new 0x7E re-anchors us. We do NOT do this once we're inside
        # the payload: 0x7E (126) is a legal channel value, and the
        # length prefix tells us exactly how many bytes to expect.
        # Genuine desync inside the payload surfaces at the end-byte
        # check (where != 0xE7 triggers a drop + resync).
        if b == _START and s in (self._S_LABEL, self._S_LEN_LO, self._S_LEN_HI):
            self._reset()
            self._state = self._S_LABEL
            self._frames_dropped += 1
            return RESET
        if s == self._S_WAIT_START:
            if b == _START:
                self._state = self._S_LABEL
            # otherwise: silently discard noise before sync.
            return NEED_MORE_BYTES

        if s == self._S_LABEL:
            self._label = b
            self._state = self._S_LEN_LO
            return NEED_MORE_BYTES

        if s == self._S_LEN_LO:
            self._payload_len = b
            self._state = self._S_LEN_HI
            return NEED_MORE_BYTES

        if s == self._S_LEN_HI:
            self._payload_len |= (b << 8)
            if self._payload_len == 0 or self._payload_len > _MAX_PAYLOAD:
                # Sanity-check: drop and resync.
                self._reset()
                self._frames_dropped += 1
                return RESET
            self._payload_idx = 0
            # Reuse pre-allocated buffer; no per-frame allocation.
            self._state = self._S_PAYLOAD
            return NEED_MORE_BYTES

        if s == self._S_PAYLOAD:
            self._buf[self._payload_idx] = b
            self._payload_idx += 1
            if self._payload_idx >= self._payload_len:
                self._state = self._S_END
            return NEED_MORE_BYTES

        if s == self._S_END:
            if b == _END:
                # Frame valid - store and reset for next. Slice up to
                # the actual payload length we recorded; the buffer is
                # max-size but only the first _payload_len bytes are
                # valid this frame. Strip the DMX start code (byte 0).
                self._last_label = self._label
                self._last_payload = bytes(self._buf[1:self._payload_len])
                self._frames_complete += 1
                self._reset()
                return FRAME_COMPLETE
            # End byte wrong - drop frame, resync.
            self._reset()
            self._frames_dropped += 1
            return RESET

        # Unreachable.
        self._reset()
        return RESET

    def feed_bytes(self, data):
        """Feed an arbitrary byte buffer; yield FRAME_COMPLETE result
        codes only. Convenience for the typical "I just read N bytes
        from USB-CDC, hand them to the parser" pattern.

        Yields the integer FRAME_COMPLETE each time a frame closes;
        RESET / NEED_MORE_BYTES are not yielded (the parser maintains
        the running state internally).
        """
        for b in data:
            if self.feed_byte(b) == FRAME_COMPLETE:
                yield FRAME_COMPLETE

    def last_label(self):
        """Label byte of the most recently completed frame."""
        return self._last_label

    def last_payload(self):
        """Channel bytes of the most recently completed frame.

        The DMX start code byte (first byte of the on-wire payload) is
        stripped, so this returns N-1 bytes for an N-byte declared
        length. For a standard 513-length frame this is exactly 512
        channel bytes.
        """
        return self._last_payload

    def frames_complete(self):
        return self._frames_complete

    def frames_dropped(self):
        return self._frames_dropped

    def _reset(self):
        self._state = self._S_WAIT_START
        self._payload_len = 0
        self._payload_idx = 0
        # Note: _last_label / _last_payload preserved for the consumer to
        # read after FRAME_COMPLETE.


def wrap_enttec_pro(channel_bytes, label=0x06):
    """Build an Enttec Pro Output Only frame around the given channel
    bytes. Used by tests to construct synthetic frames; symmetric with
    the parser so a round-trip equals identity.
    """
    if len(channel_bytes) > 512:
        channel_bytes = channel_bytes[:512]
    elif len(channel_bytes) < 512:
        channel_bytes = bytes(channel_bytes) + b"\x00" * (512 - len(channel_bytes))
    length = 512 + 1   # +1 for DMX start code
    return (bytes((_START, label, length & 0xFF, (length >> 8) & 0xFF, 0x00))
            + bytes(channel_bytes)
            + bytes((_END,)))
