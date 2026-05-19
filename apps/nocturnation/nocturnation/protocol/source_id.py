"""Source-id partitioning per protocol manual §3.4.

The 1-byte `source_id` field in the frame header is partitioned by range
to support channel-specific access control on the broadcast side and
Trust-On-First-Use locking on the receive side.

* Community range (`0x00-0x3F`, 64 slots) - used on channel 1 (hobby).
  Director picks a random ID once at first boot and persists; reuses
  across reboots.
* Performance range (`0x40-0xFE`, 191 slots) - used on channel 11
  (Performance mode). Director picks a fresh random ID at every boot
  and listens-before-broadcast to detect collisions.
* `0xFF` - broadcast / anonymous (unchanged from earlier protocol revs).

The partition is a convention layered on top of the existing field;
no wire-format change. This module mirrors the M5 firmware's
`include/transport/espnow/frame.h` constants so behaviour stays in
lockstep across the two codebases.
"""


class SourceId:
    """Source-id partition boundaries.

    Implemented as a class-as-namespace rather than IntEnum because the
    rest of this protocol module follows the same pattern for
    MicroPython compatibility (the trimmed stdlib doesn't ship enum).
    """

    COMMUNITY_MIN = 0x00
    COMMUNITY_MAX = 0x3F   # 64 slots
    PERFORMANCE_MIN = 0x40
    PERFORMANCE_MAX = 0xFE  # 191 slots
    BROADCAST = 0xFF


def is_community_range(source_id):
    """True if ``source_id`` is in the community range (`0x00-0x3F`)."""
    return SourceId.COMMUNITY_MIN <= source_id <= SourceId.COMMUNITY_MAX


def is_performance_range(source_id):
    """True if ``source_id`` is in the Performance range (`0x40-0xFE`)."""
    return SourceId.PERFORMANCE_MIN <= source_id <= SourceId.PERFORMANCE_MAX
