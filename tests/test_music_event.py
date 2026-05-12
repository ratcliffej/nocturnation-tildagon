"""MUSIC_EVENT synthetic-fire tests.

The Tildagon converts MUSIC_EVENT frames into local LIGHT_COMMAND-
shaped Frame objects so the renderers can dispatch them. The synthetic
fires are local (not on the wire) and are routed to both surfaces via
target_class=0 (All) and target_group=0 (broadcast).
"""

from nocturnation.music_event import (
    synthesize_for,
    synthesize_for_drop,
    synthesize_for_breakdown,
)
from nocturnation.protocol import MessageType
from nocturnation.protocol.constants import Time, Chance, MusicEventType


class TestDispatchByEventType:
    def test_drop_returns_synthetic(self):
        f = synthesize_for(MusicEventType.DROP)
        assert f is not None

    def test_breakdown_returns_synthetic(self):
        f = synthesize_for(MusicEventType.BREAKDOWN)
        assert f is not None

    def test_unknown_returns_none(self):
        # event_type 0 = Unknown
        assert synthesize_for(MusicEventType.UNKNOWN) is None

    def test_build_returns_none(self):
        # event_type 3 = Build (reserved)
        assert synthesize_for(MusicEventType.BUILD) is None

    def test_unrecognised_returns_none(self):
        assert synthesize_for(0xFF) is None
        assert synthesize_for(42) is None


class TestDropSynthesis:
    def test_drop_is_bright_white(self):
        f = synthesize_for_drop()
        assert f.r == 255
        assert f.g == 255
        assert f.b == 255

    def test_drop_is_chance_100(self):
        # Drop should fire on every LED - it's the peak moment.
        assert synthesize_for_drop().chance == Chance.CHANCE_100

    def test_drop_envelope_is_punchy(self):
        # Short attack, medium sustain, decay - reads as a peak hit.
        f = synthesize_for_drop()
        assert f.attack == Time.T_32_MS
        assert f.sustain == Time.T_480_MS
        assert f.release == Time.T_960_MS


class TestBreakdownSynthesis:
    def test_breakdown_is_cool_blue(self):
        f = synthesize_for_breakdown()
        assert f.r == 0
        assert f.g == 60
        assert f.b == 200

    def test_breakdown_envelope_is_slow(self):
        # Long attack, long sustain, very long release - reads as a
        # suspended quiet moment.
        f = synthesize_for_breakdown()
        assert f.attack == Time.T_480_MS
        assert f.sustain == Time.T_960_MS
        assert f.release == Time.T_2400_MS


class TestRoutingFields:
    def test_synthetic_targets_all_classes(self):
        # target_class 0 = All routes the synthetic fire to both
        # perimeter and LCD surfaces.
        for f in (synthesize_for_drop(), synthesize_for_breakdown()):
            assert f.target_class == 0x00

    def test_synthetic_targets_broadcast_group(self):
        # target_group 0 = broadcast bypasses the operator's group
        # filter so the synthetic fire always lands.
        for f in (synthesize_for_drop(), synthesize_for_breakdown()):
            assert f.target_group == 0x00

    def test_synthetic_message_type_is_light_command(self):
        # The renderers expect message_type == LIGHT_COMMAND; the
        # synthetic Frame has to claim that even though it originated
        # from a MUSIC_EVENT.
        for f in (synthesize_for_drop(), synthesize_for_breakdown()):
            assert f.message_type == MessageType.LIGHT_COMMAND
