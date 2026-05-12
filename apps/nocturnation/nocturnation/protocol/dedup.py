"""Sixteen-deep dedup ring on (source_id, sequence_number).

Receivers MUST deduplicate per protocol manual section 2.3. The master
transmits every frame three times in quick succession with the same
sequence number; the ring absorbs the redundancy so a frame is rendered
exactly once.

sequence_number == 0 signals "no sequencing" per section 3.1 and bypasses
the dedup check (always treated as fresh).

Implemented in pure Python with no stdlib dependencies so the module runs
unchanged on MicroPython.
"""

DEFAULT_CAPACITY = 16


class DedupRing:
    """FIFO ring of recently-seen (source_id, sequence_number) pairs.

    Capacity is the number of distinct pairs retained; the oldest entry
    is evicted when capacity is exceeded. The default of 16 absorbs the
    master's 3x redundancy comfortably even when several sources are
    interleaving fires.
    """

    __slots__ = ("_capacity", "_ring")

    def __init__(self, capacity=DEFAULT_CAPACITY):
        self._capacity = capacity
        self._ring = []  # list of (source_id, sequence_number) tuples

    def seen(self, source_id, sequence_number):
        """Test-and-add. Returns True if duplicate (drop the frame), False
        if fresh (process the frame and remember it).
        """
        if sequence_number == 0:
            return False
        key = (source_id, sequence_number)
        if key in self._ring:
            return True
        self._ring.append(key)
        if len(self._ring) > self._capacity:
            self._ring.pop(0)
        return False

    def __len__(self):
        return len(self._ring)
