"""Source-id partitioning per protocol manual §3.4.

The `source_id` field in the frame header (LE u16 in v3, u8 in v2 and
earlier) is partitioned by range to support channel-specific access
control on the broadcast side and Trust-On-First-Use locking on the
receive side.

* Community range (`0x0000-0x003F`, 64 slots) - used on channel 1 (hobby).
  Director picks a random ID once at first boot and persists; reuses
  across reboots.
* Performance range (`0x0040-0x00FE`, 191 slots) - used on channel 11
  (Performance mode). Director picks a fresh random ID at every boot
  and listens-before-broadcast to detect collisions.
* Extended range (`0x0100-0xFFFE`) - unused by current UI/NVS; reserved
  by the v3 wire so a future NVS/UI widening can populate it without
  another wire break.
* `0xFFFF` - broadcast / anonymous. (Was `0xFF` in v2.)

The partition is a convention layered on top of the wire field.
Partition boundaries stay at u8-natural values in v3 to preserve the
existing hex-editor UI and NVS-persisted values; the wider u16 wire
carries them as `{lo, 0}` for current-generation Directors. Mirrors
the stickc firmware's `include/transport/espnow/frame.h` constants
so behaviour stays in lockstep across the two codebases.
"""


class SourceId:
    """Source-id partition boundaries.

    Implemented as a class-as-namespace rather than IntEnum because the
    rest of this protocol module follows the same pattern for
    MicroPython compatibility (the trimmed stdlib doesn't ship enum).
    """

    COMMUNITY_MIN = 0x0000
    COMMUNITY_MAX = 0x003F   # 64 slots
    PERFORMANCE_MIN = 0x0040
    PERFORMANCE_MAX = 0x00FE  # 191 slots
    BROADCAST = 0xFFFF        # v3: widened from 0xFF


def is_community_range(source_id):
    """True if ``source_id`` is in the community range (`0x0000-0x003F`)."""
    return SourceId.COMMUNITY_MIN <= source_id <= SourceId.COMMUNITY_MAX


def is_performance_range(source_id):
    """True if ``source_id`` is in the Performance range (`0x0040-0x00FE`)."""
    return SourceId.PERFORMANCE_MIN <= source_id <= SourceId.PERFORMANCE_MAX
