"""Protocol constants. Mirror of the values defined in the protocol manual.

Implemented as plain integer constants rather than IntEnum so the module runs
unchanged under MicroPython, which doesn't ship enum in its trimmed stdlib.
"""

# 2-byte magic prefix that discriminates NocturNation traffic from other
# ESP-NOW users sharing the channel (a real concern at events like EMF).
# Receivers MUST reject any frame whose first two bytes are not "NN"
# before doing any further header validation.
MAGIC_0 = 0x4E  # 'N'
MAGIC_1 = 0x4E  # 'N'

PROTOCOL_VERSION = 0x02  # bumped from 0x01 for the magic-prefix wire change

# 2 magic + 1 version + 5 metadata fields (source_id, sequence_number,
# hop_count, message_type, payload_len).
HEADER_SIZE = 8
MAX_FRAME_SIZE = 32
MAX_PAYLOAD_SIZE = MAX_FRAME_SIZE - HEADER_SIZE


class MessageType:
    # Spec v0.29 §4.3 + Epic 6C Phase D additions. Active types: HEARTBEAT,
    # LIGHT_PULSE, the WASH family (0x06/0x07/0x08), plus the EXTENSION
    # slot. IDs 0x01 (BEAT_DETECTED), 0x02 (MODE_CHANGE), 0x04
    # (CLOCK_SYNC), 0x05 (TIME_SYNC) remain RESERVED - removed in the
    # protocol trim, MUST NOT be reused. 0x06 (was MUSIC_EVENT) is
    # reclaimed by LIGHT_WASH; 0x07 and 0x08 were previously unassigned.
    # Inbound frames carrying a reserved or unassigned message_type are
    # silently dropped per the spec forward-compat note.
    HEARTBEAT = 0x00
    LIGHT_PULSE = 0x03
    LIGHT_WASH = 0x06           # 16-byte payload: persistent two-colour drift baseline
    LIGHT_WASH_END = 0x07       # 3-byte payload: explicit cancel with operator release_time
    LIGHT_WASH_PULSE = 0x08     # 9-byte payload: pulse that fires only on washing Lumes
    EXTENSION = 0xFF


# Payload lengths per message type. Used to validate inbound frames before
# unpacking; mismatches MUST be dropped silently per protocol manual section
# 3.1. HEARTBEAT carries tick (u32 LE) + days_since_2026 (u16 LE) +
# centiseconds_today (u24 LE) = 9 bytes per spec v0.29 §3.3.1.
# WASH-family lengths per the Epic 6C design doc (16/3/9 bytes).
PAYLOAD_LENGTHS = {
    MessageType.HEARTBEAT:        9,
    MessageType.LIGHT_PULSE:      9,
    MessageType.LIGHT_WASH:       16,
    MessageType.LIGHT_WASH_END:    3,
    MessageType.LIGHT_WASH_PULSE:  9,
}


class DeviceClass:
    ALL = 0x00              # addressing wildcard; never advertised by a receiver
    LIGHT = 0x01            # PixMob bracelets, LED wristbands
    SCREEN = 0x02           # Stick LCD
    MULTI_LED_SCREEN = 0x03  # Tildagon (this device)
    # 0x04..0xFF reserved


# PixMob Time enum values; the LIGHT_PULSE attack/sustain/release bytes
# index into this table. Names mirror the StickC firmware for parity.
class Time:
    T_0_MS = 0
    T_32_MS = 1
    T_96_MS = 2
    T_192_MS = 3
    T_480_MS = 4
    T_960_MS = 5
    T_2400_MS = 6
    T_3840_MS = 7


class Chance:
    CHANCE_100 = 0
    CHANCE_88 = 1
    CHANCE_67 = 2
    CHANCE_50 = 3
    CHANCE_32 = 4
    CHANCE_16 = 5
    CHANCE_10 = 6
    CHANCE_4 = 7
