"""Protocol constants. Historically auto-generated from a Docs-repo
constants.yaml SOT; that pipeline is currently absent so edits here
are hand-authored - keep parity with the C++ frame.h in the stickc
repo when touching the wire.

v3 (this file's current version): source_id and target_group both
widened from 1 byte to 2 bytes LE. Header grew 8 -> 9 bytes.
Every LIGHT_* / TEXT / BITMAP / CLEAR payload grew +1 byte. Old-
firmware devices reject v3 at the version check.

Epic 13: 0x09 was LIGHT_FX_RUN until the music-orchestrator move to
universe-driven FX (Epic 10). With on-Lume FX libraries retired (Lume
simplicity thesis), 0x09 is now TEXT_DISPLAY. The display family
0x0A..0x0C added.
"""

MAGIC_0 = 0x4E
MAGIC_1 = 0x4E

PROTOCOL_VERSION = 0x03   # v3: u16 source_id + u16 target_group

HEADER_SIZE = 9           # v3: source_id widened from 1 byte to 2 bytes


class MessageType:
    HEARTBEAT        = 0x00
    LIGHT_PULSE      = 0x03
    LIGHT_WASH       = 0x06
    LIGHT_WASH_END   = 0x07
    LIGHT_WASH_PULSE = 0x08
    TEXT_DISPLAY     = 0x09
    BITMAP_HEADER    = 0x0A
    BITMAP_PLANE     = 0x0B
    CLEAR_SCREEN     = 0x0C
    EXTENSION        = 0xFF


# Fixed-size payloads only. Variable-length payloads (TEXT_DISPLAY,
# BITMAP_PLANE) are validated by their own range checks during parse.
# v3: every entry carrying a target_group grew +1 byte.
PAYLOAD_LENGTHS = {
    MessageType.HEARTBEAT       : 9,
    MessageType.LIGHT_PULSE     : 10,
    MessageType.LIGHT_WASH      : 17,
    MessageType.LIGHT_WASH_END  : 4,
    MessageType.LIGHT_WASH_PULSE: 10,
    MessageType.BITMAP_HEADER   : 38,
    MessageType.CLEAR_SCREEN    : 4,
}
