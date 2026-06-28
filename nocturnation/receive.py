"""Receive-side pipeline.

Drives one inbound raw frame through parse + version-check + dedup +
hop-count check. Returns the parsed Frame to be rendered, or None if
the frame should be dropped (silently per protocol manual section 3.1).

This module is pure logic - no I/O, no hardware - so the full receive
contract is exercised by the host-side pytest suite.

Two entry points:

  parse_admittable(buf)
      Parse + hop-count check only. Returns the frame even when it's
      a duplicate of a recently-seen one. The caller is responsible
      for dedup, AFTER checking TOFU + updating diagnostic state.
      Epic 15 bench follow-up: we need every admitted frame visible
      in the debug overlay (so a hop=1 relay shows up as Hop:1 even
      when it's a dup of a direct hop=0 we already saw).

  process_frame(buf, dedup_ring)
      Combined parse + dedup. Kept for back-compat with the existing
      tests; the app's receive loop now uses parse_admittable +
      explicit dedup.
"""

from .protocol import FrameError, parse_frame

# Per protocol manual section 2.3: a receiver MUST drop frames with
# hop_count > 3 to bound the relay topology.
MAX_HOP_COUNT = 3


def parse_admittable(buf):
    """Parse a raw frame + drop loop-prevention violations.

    Returns the parsed Frame, or None for:
      - structurally invalid (FrameError from parse_frame)
      - protocol_version mismatch (also via FrameError)
      - hop_count > MAX_HOP_COUNT (3)

    Does NOT consult the dedup ring. The caller decides whether to
    render the frame (skip dups) vs only update diagnostics (count
    dups so a relayed frame's hop_count is visible in the debug
    overlay even when it's a dup of a direct one).
    """
    try:
        frame = parse_frame(buf)
    except FrameError:
        return None
    if frame.hop_count > MAX_HOP_COUNT:
        return None
    return frame


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

    Kept for back-compat. The Tildagon's app receive loop has moved
    to parse_admittable + explicit dedup so it can observe relayed
    frames in the debug overlay even when they're dup-suppressed
    for rendering. New code should call parse_admittable + handle
    dedup itself.
    """
    frame = parse_admittable(buf)
    if frame is None:
        return None
    if dedup_ring.seen(frame.source_id, frame.sequence_number):
        return None
    return frame
