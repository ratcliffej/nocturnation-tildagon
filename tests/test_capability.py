"""Capability + CapabilityMask tests.

Mirrors the M5 firmware's CapabilityMask invariants: set/has,
subset_of for plug-in gating, OR composition.
"""

from nocturnation.hal import Capability, CapabilityMask


class TestCapabilityValues:
    def test_imu_subcaps_after_analyser_subcaps(self):
        # B1 sign-off: IMU sub-caps land at indices 17-18, after the
        # Epic-4.5/4.7 analyser sub-caps at 9-16.
        assert Capability.ANALYSER_SECTION_DETECTION == 16
        assert Capability.IMU_TAP == 17
        assert Capability.IMU_MOTION == 18

    def test_coarse_imu_separate_from_subcaps(self):
        # Coarse Capability.IMU stays as the "hardware exists" flag;
        # IMU_TAP / IMU_MOTION compose what the backend produces.
        assert Capability.IMU == 6
        assert Capability.IMU != Capability.IMU_TAP
        assert Capability.IMU != Capability.IMU_MOTION


class TestEmptyMask:
    def test_empty_construction(self):
        m = CapabilityMask()
        assert m.empty() is True
        assert m.raw() == 0

    def test_empty_has_nothing(self):
        m = CapabilityMask()
        assert m.has(Capability.DISPLAY) is False
        assert m.has(Capability.IMU_TAP) is False


class TestVariadicConstruction:
    def test_single_cap(self):
        m = CapabilityMask(Capability.DISPLAY)
        assert m.has(Capability.DISPLAY) is True
        assert m.has(Capability.MIC) is False
        assert m.empty() is False

    def test_multiple_caps(self):
        m = CapabilityMask(Capability.DISPLAY, Capability.ESP_NOW, Capability.IMU_TAP)
        assert m.has(Capability.DISPLAY) is True
        assert m.has(Capability.ESP_NOW) is True
        assert m.has(Capability.IMU_TAP) is True
        assert m.has(Capability.IMU_MOTION) is False


class TestSet:
    def test_set_adds_capability(self):
        m = CapabilityMask()
        m.set(Capability.IMU_TAP)
        assert m.has(Capability.IMU_TAP) is True

    def test_set_returns_self_for_chaining(self):
        m = CapabilityMask().set(Capability.DISPLAY).set(Capability.ESP_NOW)
        assert m.has(Capability.DISPLAY) is True
        assert m.has(Capability.ESP_NOW) is True

    def test_set_is_idempotent(self):
        m = CapabilityMask()
        m.set(Capability.IMU_TAP)
        m.set(Capability.IMU_TAP)
        assert m.has(Capability.IMU_TAP) is True


class TestSubsetOf:
    def test_empty_is_subset_of_anything(self):
        # A Show with no requirements runs on every host.
        host = CapabilityMask(Capability.DISPLAY)
        assert CapabilityMask().subset_of(host) is True
        assert CapabilityMask().subset_of(CapabilityMask()) is True

    def test_exact_match_is_subset(self):
        a = CapabilityMask(Capability.DISPLAY, Capability.ESP_NOW)
        b = CapabilityMask(Capability.DISPLAY, Capability.ESP_NOW)
        assert a.subset_of(b) is True

    def test_strict_subset(self):
        # Show needs DISPLAY; host has DISPLAY + ESP_NOW + IMU_TAP -> Show runs.
        req = CapabilityMask(Capability.DISPLAY)
        host = CapabilityMask(Capability.DISPLAY, Capability.ESP_NOW, Capability.IMU_TAP)
        assert req.subset_of(host) is True

    def test_missing_capability_breaks_subset(self):
        # Show needs IMU_TAP; host doesn't declare it -> Show gated off.
        req = CapabilityMask(Capability.DISPLAY, Capability.IMU_TAP)
        host = CapabilityMask(Capability.DISPLAY)
        assert req.subset_of(host) is False

    def test_extra_capability_breaks_subset(self):
        # subset_of is "every cap in self is in other", so superset is NOT
        # a subset.
        a = CapabilityMask(Capability.DISPLAY, Capability.ESP_NOW)
        b = CapabilityMask(Capability.DISPLAY)
        assert a.subset_of(b) is False


class TestOrComposition:
    def test_or_unions_caps(self):
        a = CapabilityMask(Capability.DISPLAY)
        b = CapabilityMask(Capability.ESP_NOW, Capability.IMU_TAP)
        c = a | b
        assert c.has(Capability.DISPLAY) is True
        assert c.has(Capability.ESP_NOW) is True
        assert c.has(Capability.IMU_TAP) is True
        # Original masks unchanged.
        assert a.has(Capability.ESP_NOW) is False
        assert b.has(Capability.DISPLAY) is False


class TestEquality:
    def test_same_caps_equal(self):
        a = CapabilityMask(Capability.DISPLAY, Capability.ESP_NOW)
        b = CapabilityMask(Capability.ESP_NOW, Capability.DISPLAY)
        assert a == b

    def test_different_caps_not_equal(self):
        a = CapabilityMask(Capability.DISPLAY)
        b = CapabilityMask(Capability.ESP_NOW)
        assert a != b
