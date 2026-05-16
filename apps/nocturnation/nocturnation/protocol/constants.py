"""Protocol constants. Mirror of the values defined in the protocol manual.

Implemented as plain integer constants rather than IntEnum so the module runs
unchanged under MicroPython, which doesn't ship enum in its trimmed stdlib.
"""

PROTOCOL_VERSION = 0x01

HEADER_SIZE = 6
MAX_FRAME_SIZE = 32
MAX_PAYLOAD_SIZE = MAX_FRAME_SIZE - HEADER_SIZE


class MessageType:
    # Spec v0.29 §4.3: two active types plus the EXTENSION slot. IDs
    # 0x01 (BEAT_DETECTED), 0x02 (MODE_CHANGE), 0x04 (CLOCK_SYNC),
    # 0x05 (TIME_SYNC), 0x06 (MUSIC_EVENT) are RESERVED - they were
    # removed in the protocol trim and MUST NOT be reused. Inbound
    # frames carrying a reserved or unassigned message_type are
    # silently dropped per the spec forward-compat note.
    HEARTBEAT = 0x00
    LIGHT_COMMAND = 0x03
    EXTENSION = 0xFF


# Payload lengths per message type. Used to validate inbound frames before
# unpacking; mismatches MUST be dropped silently per protocol manual section
# 3.1. HEARTBEAT carries tick (u32 LE) + days_since_2026 (u16 LE) +
# centiseconds_today (u24 LE) = 9 bytes per spec v0.29 §3.3.1.
PAYLOAD_LENGTHS = {
    MessageType.HEARTBEAT: 9,
    MessageType.LIGHT_COMMAND: 9,
}


class DeviceClass:
    ALL = 0x00              # addressing wildcard; never advertised by a receiver
    LIGHT = 0x01            # PixMob bracelets, LED wristbands
    SCREEN = 0x02           # Stick LCD
    MULTI_LED_SCREEN = 0x03  # Tildagon (this device)
    # 0x04..0xFF reserved


# PixMob Time enum values; the LIGHT_COMMAND attack/sustain/release bytes
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
