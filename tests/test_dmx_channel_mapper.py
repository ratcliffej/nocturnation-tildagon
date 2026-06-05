# SPDX-License-Identifier: MIT
"""Host-side tests for the NocturNation 12-channel DMX layout mapper."""

import pytest

from nocturnation.director.dmx_channel_mapper import (
    DmxChannelMapper,
    EVENT_PULSE,
    EVENT_WASH,
    CH_MASTER,
    CH_STROBE,
    CH_PULSE_R,
    CH_PULSE_G,
    CH_PULSE_B,
    CH_PULSE_TRIG,
    CH_WASH_A_R,
    CH_WASH_A_G,
    CH_WASH_A_B,
    CH_WASH_B_R,
    CH_WASH_B_G,
    CH_WASH_B_B,
    TRIGGER_THRESHOLD,
)


def _make_universe():
    """Return a fresh 512-byte channel buffer initialised to zero."""
    return bytearray(512)


def _pulse_events(events):
    return [e for e in events if e[0] == EVENT_PULSE]


def _wash_events(events):
    return [e for e in events if e[0] == EVENT_WASH]


def test_first_tick_emits_initial_wash():
    """First call always emits a wash (sentinel -> real state diff)."""
    m = DmxChannelMapper()
    events = m.process(_make_universe(), 0)
    assert len(_wash_events(events)) == 1
    # All-zero wash; (0,0,0,0,0,0).
    et, payload = _wash_events(events)[0]
    assert payload == (0, 0, 0, 0, 0, 0)


def test_unchanged_wash_no_re_emit():
    """Steady wash state doesn't re-emit on every tick."""
    m = DmxChannelMapper()
    u = _make_universe()
    u[CH_WASH_A_R] = 200
    u[CH_MASTER] = 255
    m.process(u, 0)              # initial emit
    events = m.process(u, 50)    # tick again, unchanged
    assert _wash_events(events) == []


def test_wash_emits_on_anchor_change():
    """Change any wash anchor channel; one wash event emitted."""
    m = DmxChannelMapper()
    u = _make_universe()
    u[CH_MASTER] = 255
    m.process(u, 0)              # baseline
    u[CH_WASH_A_R] = 128
    events = m.process(u, 50)
    assert len(_wash_events(events)) == 1
    et, payload = _wash_events(events)[0]
    # Master is 255 so scale is 1:1.
    assert payload == (128, 0, 0, 0, 0, 0)


def test_wash_emits_on_master_change():
    """Master scales emitted RGB; master-only change must re-emit so
    Lumes see the new brightness."""
    m = DmxChannelMapper()
    u = _make_universe()
    u[CH_WASH_A_R] = 200
    u[CH_MASTER] = 255
    m.process(u, 0)
    u[CH_MASTER] = 128
    events = m.process(u, 50)
    assert len(_wash_events(events)) == 1
    et, payload = _wash_events(events)[0]
    # 200 * 128 / 255 = ~100.
    assert payload[0] == (200 * 128) // 255


def test_pulse_rising_edge_fires_once():
    """Trigger crossing the threshold from below fires exactly one
    pulse; staying high doesn't refire."""
    m = DmxChannelMapper()
    u = _make_universe()
    u[CH_MASTER] = 255
    u[CH_PULSE_R] = 255
    m.process(u, 0)              # baseline

    # Rising edge.
    u[CH_PULSE_TRIG] = TRIGGER_THRESHOLD
    events = m.process(u, 50)
    assert len(_pulse_events(events)) == 1

    # Held high - no refire.
    events2 = m.process(u, 100)
    assert _pulse_events(events2) == []

    # Drop below threshold, re-arm, raise again.
    u[CH_PULSE_TRIG] = 0
    m.process(u, 150)
    u[CH_PULSE_TRIG] = 255
    events3 = m.process(u, 200)
    assert len(_pulse_events(events3)) == 1


def test_pulse_below_threshold_no_fire():
    """Trigger values < threshold don't fire even on rising edges."""
    m = DmxChannelMapper()
    u = _make_universe()
    m.process(u, 0)
    u[CH_PULSE_TRIG] = TRIGGER_THRESHOLD - 1
    events = m.process(u, 50)
    assert _pulse_events(events) == []


def test_strobe_fires_at_configured_rate():
    """Strobe > 0 emits pulses at the rate set by the strobe channel,
    independent of the manual trigger.

    First tick with strobe enabled fires immediately (start the train);
    subsequent ticks fire on the configured interval.
    """
    m = DmxChannelMapper()
    u = _make_universe()
    u[CH_MASTER] = 255
    u[CH_PULSE_R] = 255
    u[CH_STROBE] = 255   # max rate (4 Hz -> 250 ms interval)

    # First tick with strobe enabled - immediate fire.
    events_t0 = m.process(u, 0)
    assert len(_pulse_events(events_t0)) == 1

    # Tick at t=100 - too soon, no fire.
    events_t100 = m.process(u, 100)
    assert _pulse_events(events_t100) == []

    # Tick at t=300 - past the 250ms interval, fire.
    events_t300 = m.process(u, 300)
    assert len(_pulse_events(events_t300)) == 1


def test_strobe_off_no_pulses():
    """Strobe channel = 0 produces no strobe pulses, ever."""
    m = DmxChannelMapper()
    u = _make_universe()
    u[CH_MASTER] = 255
    u[CH_PULSE_R] = 255
    m.process(u, 0)
    for t in (10, 100, 1000, 10000):
        events = m.process(u, t)
        assert _pulse_events(events) == []


def test_master_zero_emits_black_wash():
    """Master = 0 scales the wash to all zeros even with bright
    anchors."""
    m = DmxChannelMapper()
    u = _make_universe()
    u[CH_WASH_A_R] = 255
    u[CH_WASH_A_G] = 255
    u[CH_WASH_A_B] = 255
    u[CH_MASTER] = 0
    events = m.process(u, 0)
    et, payload = _wash_events(events)[0]
    assert payload == (0, 0, 0, 0, 0, 0)


def test_short_buffer_dropped_silently():
    """A channel buffer that doesn't cover the fixture's 12 channels
    returns no events, no crash."""
    m = DmxChannelMapper()
    short = bytearray(8)   # only 8 channels
    events = m.process(short, 0)
    assert events == []


def test_base_address_offset():
    """A non-zero base_address reads channels from later in the buffer."""
    m = DmxChannelMapper(base_address=13)   # fixture starts at channel 14
    u = _make_universe()
    # Channel 14 (index 13) is master.
    u[13 + CH_MASTER] = 255
    u[13 + CH_WASH_A_R] = 100
    events = m.process(u, 0)
    et, payload = _wash_events(events)[0]
    assert payload[0] == 100
