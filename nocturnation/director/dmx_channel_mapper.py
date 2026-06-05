# SPDX-License-Identifier: MIT
"""NocturNation 12-channel DMX layout interpreter - MicroPython port
of the StickC B2 mapper.

Walks the 512-byte channel buffer from the parser and emits semantic
events on meaningful change:

  * Rising-edge on the Pulse Trigger channel    -> emit one pulse.
  * Any change in Wash A / Wash B anchor RGBs   -> emit a wash event.
  * Strobe rate > 0                              -> emit a continuous
                                                     train of pulses at
                                                     the configured rate
                                                     (independent of the
                                                     manual trigger).

Master Intensity (channel 1) is applied to the emitted RGB values, so
a master of 0 produces black events even if anchor colours are bright.

Channel layout (Epic 7 Q2, 12 channels per fixture group, base address
configurable but not exposed in v1):

  Ch 01  Master intensity
  Ch 02  Strobe rate         (0=off, 255=4 Hz cap)
  Ch 03  Pulse R
  Ch 04  Pulse G
  Ch 05  Pulse B
  Ch 06  Pulse trigger       (rising edge >=128)
  Ch 07  Wash A R
  Ch 08  Wash A G
  Ch 09  Wash A B
  Ch 10  Wash B R
  Ch 11  Wash B G
  Ch 12  Wash B B

The mapper is transport-agnostic and tick-driven: feed it the latest
512-byte channel buffer plus the current monotonic time, and it
returns the events to dispatch. Pure-Python; host-testable.
"""


# Channel indices (0-based into the channel buffer).
CH_MASTER       = 0
CH_STROBE       = 1
CH_PULSE_R      = 2
CH_PULSE_G      = 3
CH_PULSE_B      = 4
CH_PULSE_TRIG   = 5
CH_WASH_A_R     = 6
CH_WASH_A_G     = 7
CH_WASH_A_B     = 8
CH_WASH_B_R     = 9
CH_WASH_B_G     = 10
CH_WASH_B_B     = 11

CHANNELS_PER_GROUP = 12

# Trigger rising-edge threshold per Epic 7 Q2 fixture spec.
TRIGGER_THRESHOLD = 128

# Strobe rate cap per architecture spec §15.1 (4 Hz safety floor for
# bracelet wear). DMX value 255 maps to this rate; linear interpolation
# down to ~0.5 Hz at value 1; 0 disables strobe.
STROBE_MAX_HZ = 4.0
STROBE_MIN_HZ = 0.5


# Event types - small ints, simpler to dispatch on the badge than a
# class hierarchy.
EVENT_PULSE = 1   # payload: (r, g, b)  -- master already applied
EVENT_WASH  = 2   # payload: (ar, ag, ab, br, bg, bb)  -- master already applied


def _scale(value, master):
    """Multiply an 8-bit channel value by the 8-bit master intensity,
    returning an 8-bit result. (value * master) / 255 with integer math.
    """
    return (value * master) // 255


class DmxChannelMapper:
    """Stateful channel-change-to-event translator.

    The mapper holds the last-seen wash anchor values, the last
    trigger state (for rising-edge detection), and the last strobe
    fire timestamp (for rate-driven pulse trains).

    Usage:
        mapper = DmxChannelMapper()
        events = mapper.process(channel_buffer, now_ms)
        for evt_type, payload in events:
            dispatch(evt_type, payload)
    """

    __slots__ = (
        "_base",
        "_last_trigger",
        "_last_wash_a",
        "_last_wash_b",
        "_last_master_for_wash",
        "_next_strobe_ms",
    )

    def __init__(self, base_address=0):
        """base_address is the 0-indexed start of this fixture's
        channels within the universe (channel 1 = index 0).
        """
        self._base = base_address
        self._last_trigger = 0
        self._last_wash_a = (-1, -1, -1)   # sentinel: forces first emit
        self._last_wash_b = (-1, -1, -1)
        self._last_master_for_wash = -1    # so master-only changes re-emit
        self._next_strobe_ms = 0

    def process(self, channels, now_ms):
        """Consume the latest channel buffer; return a list of
        (event_type, payload) tuples to dispatch.

        Returns an empty list when nothing notable changed - the common
        case for steady cues, which is why the bridge doesn't flood the
        radio.
        """
        events = []
        # Bounds check: if the buffer doesn't extend through our
        # fixture's 12 channels, drop silently. (Defensive; shouldn't
        # happen with a full 512-byte universe.)
        if len(channels) < self._base + CHANNELS_PER_GROUP:
            return events

        b = self._base
        master = channels[b + CH_MASTER]

        # ----- Pulse trigger: rising-edge detection. -----
        trig = channels[b + CH_PULSE_TRIG]
        if trig >= TRIGGER_THRESHOLD and self._last_trigger < TRIGGER_THRESHOLD:
            r = _scale(channels[b + CH_PULSE_R], master)
            g = _scale(channels[b + CH_PULSE_G], master)
            pb = _scale(channels[b + CH_PULSE_B], master)
            events.append((EVENT_PULSE, (r, g, pb)))
        self._last_trigger = trig

        # ----- Strobe: rate-driven continuous train. -----
        strobe = channels[b + CH_STROBE]
        if strobe > 0:
            rate_hz = STROBE_MIN_HZ + (STROBE_MAX_HZ - STROBE_MIN_HZ) * (strobe / 255.0)
            interval_ms = int(1000.0 / rate_hz)
            if now_ms >= self._next_strobe_ms:
                r = _scale(channels[b + CH_PULSE_R], master)
                g = _scale(channels[b + CH_PULSE_G], master)
                pb = _scale(channels[b + CH_PULSE_B], master)
                events.append((EVENT_PULSE, (r, g, pb)))
                self._next_strobe_ms = now_ms + interval_ms
        else:
            # Reset so re-enabling strobe fires immediately, not "in
            # however-much-time the disabled-strobe cycle had left".
            self._next_strobe_ms = now_ms

        # ----- Wash: emit on any change of anchor RGB or master. -----
        wash_a_raw = (channels[b + CH_WASH_A_R],
                      channels[b + CH_WASH_A_G],
                      channels[b + CH_WASH_A_B])
        wash_b_raw = (channels[b + CH_WASH_B_R],
                      channels[b + CH_WASH_B_G],
                      channels[b + CH_WASH_B_B])
        if (wash_a_raw != self._last_wash_a
                or wash_b_raw != self._last_wash_b
                or master != self._last_master_for_wash):
            ar = _scale(wash_a_raw[0], master)
            ag = _scale(wash_a_raw[1], master)
            ab = _scale(wash_a_raw[2], master)
            br = _scale(wash_b_raw[0], master)
            bg = _scale(wash_b_raw[1], master)
            bb = _scale(wash_b_raw[2], master)
            events.append((EVENT_WASH, (ar, ag, ab, br, bg, bb)))
            self._last_wash_a = wash_a_raw
            self._last_wash_b = wash_b_raw
            self._last_master_for_wash = master

        return events
