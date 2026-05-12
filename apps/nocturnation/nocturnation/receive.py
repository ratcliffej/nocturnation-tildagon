"""Receive-side pipeline.

Drives one inbound raw frame through parse + version-check + dedup +
hop-count check. Returns the parsed Frame to be rendered, or None if
the frame should be dropped (silently per protocol manual section 3.1).

This module is pure logic - no I/O, no hardware - so the full receive
contract is exercised by the host-side pytest suite.
"""

from .protocol import FrameError, parse_frame

# Per protocol manual section 2.3: a receiver MUST drop frames with
# hop_count > 3 to bound the relay topology.
MAX_HOP_COUNT = 3


def process_frame(buf, dedup_ring):
    """Process one inbound frame.

    ``buf`` is the raw ESP-NOW payload (bytes-like).
    ``dedup_ring`` is a DedupRing instance maintained across calls.

    Returns the parsed Frame on success, or None when the frame must
    be dropped. Drop reasons (all silent per the protocol manual):
      - structurally invalid (FrameError from parse_frame)
      - protocol_version mismatch (also surfaced via FrameError)
      - hop_count > MAX_HOP_COUNT (3)
      - duplicate (already in dedup ring)
    """
    try:
        frame = parse_frame(buf)
    except FrameError:
        return None

    if frame.hop_count > MAX_HOP_COUNT:
        return None

    if dedup_ring.seen(frame.source_id, frame.sequence_number):
        return None

    return frame
