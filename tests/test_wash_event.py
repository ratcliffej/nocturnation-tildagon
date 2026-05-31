"""RgbWash event-class tests (Epic 6D B1).

Mirror of the M5 LightWashEvent shape on the Tildagon. Verifies field
clamping, defaults, single-colour shorthand, equality + repr.
"""

from nocturnation.render import RgbWash


class TestRgbWashConstruction:
    def test_all_fields_set_explicitly(self):
        w = RgbWash(
            r1=255, g1=140, b1=30,
            r2=120, g2=30,  b2=200,
            attack=20, release=10,
            intensity=200, cycle_ms=5000,
            ttl_seconds=120, pulse_response=1,
        )
        assert (w.r1, w.g1, w.b1) == (255, 140, 30)
        assert (w.r2, w.g2, w.b2) == (120, 30, 200)
        assert w.attack == 20
        assert w.release == 10
        assert w.intensity == 200
        assert w.cycle_ms == 5000
        assert w.ttl_seconds == 120
        assert w.pulse_response == 1

    def test_b_anchor_defaults_to_a_anchor(self):
        # Single-colour wash: omit r2/g2/b2 and the renderer holds A.
        w = RgbWash(r1=255, g1=140, b1=30)
        assert (w.r2, w.g2, w.b2) == (255, 140, 30)

    def test_defaults_match_warm_baseline(self):
        # The defaults are tuned to the M5 wash_demo "warm orange"
        # baseline so the example in the doc-comment works out of the box.
        w = RgbWash(r1=255, g1=140, b1=30)
        assert w.attack == 20
        assert w.release == 10
        assert w.intensity == 200
        assert w.cycle_ms == 5000
        assert w.ttl_seconds == 0
        assert w.pulse_response == 1


class TestRgbWashClamping:
    def test_rgb_clamped_to_byte(self):
        w = RgbWash(r1=300, g1=-5, b1=0xFFFF)
        assert w.r1 == 300 & 0xFF
        assert w.g1 == (-5) & 0xFF
        assert w.b1 == 0xFF

    def test_attack_release_intensity_clamped_to_byte(self):
        w = RgbWash(0, 0, 0, attack=0x1FF, release=0x100, intensity=0x123)
        assert w.attack == 0xFF
        assert w.release == 0x00
        assert w.intensity == 0x23

    def test_cycle_ms_clamped_to_u16(self):
        w = RgbWash(0, 0, 0, cycle_ms=0x1FFFF)
        assert w.cycle_ms == 0xFFFF

    def test_ttl_seconds_clamped_to_u16(self):
        w = RgbWash(0, 0, 0, ttl_seconds=0x10001)
        assert w.ttl_seconds == 0x0001

    def test_pulse_response_normalised_to_bit(self):
        # Any truthy value -> 1; any falsy -> 0.
        assert RgbWash(0, 0, 0, pulse_response=1).pulse_response == 1
        assert RgbWash(0, 0, 0, pulse_response=0).pulse_response == 0
        assert RgbWash(0, 0, 0, pulse_response=True).pulse_response == 1
        assert RgbWash(0, 0, 0, pulse_response=False).pulse_response == 0
        assert RgbWash(0, 0, 0, pulse_response=42).pulse_response == 1


class TestRgbWashEquality:
    def test_equal_when_all_fields_match(self):
        a = RgbWash(255, 140, 30, 120, 30, 200, attack=20, cycle_ms=5000)
        b = RgbWash(255, 140, 30, 120, 30, 200, attack=20, cycle_ms=5000)
        assert a == b

    def test_unequal_when_one_field_differs(self):
        a = RgbWash(255, 140, 30, cycle_ms=5000)
        b = RgbWash(255, 140, 30, cycle_ms=4000)
        assert a != b

    def test_unequal_to_non_wash(self):
        w = RgbWash(0, 0, 0)
        assert w != "RgbWash"
        assert w != (0, 0, 0)


class TestRgbWashRepr:
    def test_repr_contains_key_fields(self):
        w = RgbWash(255, 140, 30, cycle_ms=5000, intensity=200)
        r = repr(w)
        assert "r1=255" in r
        assert "g1=140" in r
        assert "cycle_ms=5000" in r
        assert "intensity=200" in r
