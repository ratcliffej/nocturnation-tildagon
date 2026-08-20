"""Protocol constants. Historically auto-generated from a Docs-repo
constants.yaml SOT; that pipeline is currently absent so edits here
are hand-authored - keep parity with the C++ frame.h in the stickc
repo when touching the wire.

v4 (this file's current version): LIGHT_PULSE / LIGHT_WASH /
LIGHT_WASH_END / LIGHT_WASH_PULSE each gain 3 trailing bytes for
LED-level addressing (Epic 18):
    led_mode      (1 byte)  - 0=all pixels, 1=single LED, 2=RepeatPattern
    led_modifier1 (1 byte)  - mode 1: chain ID (0=all chains); mode 2: mask low
    led_modifier2 (1 byte)  - mode 1: LED index within chain; mode 2: mask high
Header layout unchanged from v3 (HEADER_SIZE stays 9, HOP_COUNT_OFFSET
stays 6). Old-firmware devices reject v4 at the version check.

v3: source_id and target_group both widened from 1 byte to 2 bytes LE.
Header grew 8 -> 9 bytes. Every LIGHT_* / TEXT / BITMAP / CLEAR payload
grew +1 byte.

Epic 13: 0x09 was LIGHT_FX_RUN until the music-orchestrator move to
universe-driven FX (Epic 10). With on-Lume FX libraries retired (Lume
simplicity thesis), 0x09 is now TEXT_DISPLAY. The display family
0x0A..0x0C added.
"""

MAGIC_0 = 0x4E
MAGIC_1 = 0x4E

PROTOCOL_VERSION = 0x04   # v4: LED-level addressing (Epic 18)

HEADER_SIZE = 9           # v3+: unchanged in v4


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


# LedMode values (Epic 18, v4). Only meaningful on LIGHT_* messages.
class LedMode:
    ALL             = 0x00   # whole strip; modifier bytes reserved (0)
    SINGLE_LED      = 0x01   # modifier1 = chain ID (0=all chains), modifier2 = LED index
    REPEAT_PATTERN  = 0x02   # modifier1 = mask low byte, modifier2 = mask high nibble (upper 4 bits reserved)


# Fixed-size payloads only. Variable-length payloads (TEXT_DISPLAY,
# BITMAP_PLANE) are validated by their own range checks during parse.
# v4: LIGHT_* entries gained +3 bytes for LED addressing.
PAYLOAD_LENGTHS = {
    MessageType.HEARTBEAT       : 9,
    MessageType.LIGHT_PULSE     : 13,   # v4: +3
    MessageType.LIGHT_WASH      : 20,   # v4: +3
    MessageType.LIGHT_WASH_END  : 7,    # v4: +3
    MessageType.LIGHT_WASH_PULSE: 13,   # v4: +3
    MessageType.BITMAP_HEADER   : 38,
    MessageType.CLEAR_SCREEN    : 4,
}
