"""Per-sender numonce LRU for CONFIG_WRITE replay protection.

Mirror of nocturnation-stickc's src/transport/espnow/numonce_lru.{h,cpp}.
The receiver side of Epic 21's authenticated config channel: when a
CONFIG_WRITE arrives, the Lume looks up the sender's highest-seen
numonce and admits the frame only if the incoming numonce is strictly
greater. The slot is updated BEFORE HMAC verification so an unauthen-
ticated flood of fake numonces cannot be used to exhaust the slot's
counter space from under a legitimate Director.

16 slots is sized for small-fleet operation with multiple Directors;
eviction is LRU. Numonce 0 is a reserved sentinel ("no numonce seen
yet") and MUST never be transmitted; `check_and_update` rejects it.

MicroPython: this module is pure Python with no stdlib imports beyond
language built-ins. Safe on both CPython and MicroPython.

Terminology: `numonce`, not "nonce" - see
[[feedback-vocab-numonce-not-nonce]] in memory.
"""

_SLOTS = 16


class NumonceLru:
    """Fixed-size LRU of (source_id, highest_numonce) pairs.

    Entries are kept in insertion-recency order; the oldest entry is
    evicted when a new source_id arrives and the table is full. Lookup,
    update, and eviction are all O(_SLOTS).
    """

    __slots__ = ("_entries",)

    def __init__(self):
        # List of [source_id, highest_numonce] pairs, in LRU order:
        # most-recently-used at the end.
        self._entries = []

    def check_and_update(self, source_id, numonce):
        """Admit the frame iff `numonce` is strictly greater than the
        highest previously-seen numonce from this `source_id`, and
        update the slot. Numonce 0 is always rejected (reserved
        sentinel - a well-behaved Director's first emit is 1).

        Returns True on admission, False on rejection (including the
        0-sentinel case).
        """
        if numonce == 0:
            return False
        for i, entry in enumerate(self._entries):
            if entry[0] == source_id:
                if numonce <= entry[1]:
                    # Replay or out-of-order: reject AND leave the slot
                    # untouched. (The spec says update-before-HMAC, but
                    # the slot is already current - we don't downgrade.)
                    return False
                entry[1] = numonce
                # Move to end (most-recently-used).
                if i != len(self._entries) - 1:
                    self._entries.append(self._entries.pop(i))
                return True
        # New sender. Admit, evict oldest if full.
        if len(self._entries) >= _SLOTS:
            self._entries.pop(0)
        self._entries.append([source_id, numonce])
        return True

    def highest(self, source_id):
        """Diagnostic: return the highest numonce seen from this
        sender, or 0 if unknown. Not used by the hot path.
        """
        for entry in self._entries:
            if entry[0] == source_id:
                return entry[1]
        return 0

    def clear(self):
        """Reset all slots. Operator-triggered recovery hook."""
        self._entries = []

    def __len__(self):
        return len(self._entries)
