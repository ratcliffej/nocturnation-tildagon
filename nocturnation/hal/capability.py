"""Capability enum + CapabilityMask bitset.

Cross-platform mirror of `hal::Capability` and `hal::CapabilityMask`
in the M5 firmware (see include/hal/hal.h and include/hal/
capability_mask.h in the nocturnation-m5 repo). Identical numeric
values to the C++ enum so a plug-in's required-capabilities mask is
portable across platforms in concept (in practice each platform has
its own Plugin/Show base classes, but the capability model is the
shared vocabulary).

A plug-in declares its requirements via
`Plugin.required_capabilities()`. The host builds a mask of what its
backend actually supports. The framework gates plug-in selection
with `req.subset_of(host)`.

The Tildagon does not have a microphone, so the analyser sub-caps
(`ANALYSER_BEAT_DETECTION` etc.) are present in the enum for
cross-platform consistency but never declared by any Tildagon host
mask. The Tildagon-specific sub-caps are the IMU ones added in
Epic 6B (`IMU_TAP`, `IMU_MOTION`).
"""


class Capability:
    """Capability enum (integer-valued).

    Values match the C++ `hal::Capability` enum in nocturnation-m5 at
    include/hal/hal.h. Add new values at the end and update the M5
    enum in lockstep.
    """

    # Coarse hardware (Epic 4.6 set on M5)
    MIC          = 0
    IR_TX        = 1
    IR_RX        = 2
    ESP_NOW      = 3
    DISPLAY      = 4
    BUTTONS      = 5
    IMU          = 6
    BATTERY      = 7
    BLUETOOTH    = 8

    # Audio analyser sub-caps (Epic 4.5 - lit on M5, reserved on Tildagon)
    ANALYSER_BEAT_DETECTION    = 9
    ANALYSER_DROP_DETECTION    = 10
    ANALYSER_SPECTRUM_FRAME    = 11
    ANALYSER_BAND_SUMMARY      = 12

    # Analyser sub-caps (Epic 4.7 - reserved on both hosts)
    ANALYSER_MULTI_BAND_ONSET  = 13
    ANALYSER_SPECTRAL_CENTROID = 14
    ANALYSER_ENERGY_ENVELOPE   = 15
    ANALYSER_SECTION_DETECTION = 16

    # IMU sub-caps (Epic 6B - lit on Tildagon, reserved on M5)
    IMU_TAP                    = 17
    IMU_MOTION                 = 18


class CapabilityMask:
    """Compact bitset of Capability values.

    Mirrors `hal::CapabilityMask` in nocturnation-m5 at
    include/hal/capability_mask.h, including the subset_of semantics.

    Construction:
        CapabilityMask(Capability.DISPLAY, Capability.ESP_NOW)
        CapabilityMask()  # empty
    """

    __slots__ = ("_bits",)

    def __init__(self, *caps):
        self._bits = 0
        for cap in caps:
            self._bits |= (1 << cap)

    def set(self, cap):
        self._bits |= (1 << cap)
        return self

    def has(self, cap):
        return bool(self._bits & (1 << cap))

    def subset_of(self, other):
        """True when every capability in self is also in other.

        Used for plug-in gating: `plugin.required_capabilities().subset_of(host_mask)`.
        """
        return (self._bits & ~other._bits) == 0

    def empty(self):
        return self._bits == 0

    def raw(self):
        return self._bits

    def __or__(self, other):
        out = CapabilityMask()
        out._bits = self._bits | other._bits
        return out

    def __eq__(self, other):
        return isinstance(other, CapabilityMask) and self._bits == other._bits

    def __hash__(self):
        return hash(self._bits)

    def __repr__(self):
        return "CapabilityMask(0x{:x})".format(self._bits)
