"""Protocol constants. Mirror of the values defined in the protocol manual.

Implemented as plain integer constants rather than IntEnum so the module runs
unchanged under MicroPython, which doesn't ship enum in its trimmed stdlib.
"""

PROTOCOL_VERSION = 0x01

HEADER_SIZE = 6
MAX_FRAME_SIZE = 32
MAX_PAYLOAD_SIZE = MAX_FRAME_SIZE - HEADER_SIZE


class MessageType:
    HEARTBEAT = 0x00
    BEAT_DETECTED = 0x01
    MODE_CHANGE = 0x02
    LIGHT_COMMAND = 0x03
    CLOCK_SYNC = 0x04
    TIME_SYNC = 0x05
    MUSIC_EVENT = 0x06
    EXTENSION = 0xFF


# Payload lengths per message type. Used to validate inbound frames before
# unpacking; mismatches MUST be dropped silently per protocol manual section
# 3.1.
PAYLOAD_LENGTHS = {
    MessageType.HEARTBEAT: 0,
    MessageType.BEAT_DETECTED: 3,
    MessageType.MODE_CHANGE: 2,
    MessageType.LIGHT_COMMAND: 9,
    MessageType.CLOCK_SYNC: 4,
    MessageType.TIME_SYNC: 5,
    MessageType.MUSIC_EVENT: 1,
}


class DeviceClass:
    ALL = 0x00              # addressing wildcard; never advertised by a receiver
    LIGHT = 0x01            # PixMob bracelets, LED wristbands
    SCREEN = 0x02           # Stick LCD
    MULTI_LED_SCREEN = 0x03  # Tildagon (this device)
    # 0x04..0xFF reserved


class MusicEventType:
    UNKNOWN = 0
    DROP = 1
    BREAKDOWN = 2
    BUILD = 3  # reserved


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
