"""HAL surface for NocturNation Tildagon.

The Tildagon HAL is intentionally thin: the badge's existing
`tildagonos`, `tildagon.imu`, `app_components`, etc. APIs are the
actual hardware abstraction layer. This package provides the
NocturNation-specific contracts that ride on top of them - notably
the cross-platform `Capability` enum and `CapabilityMask` bitset
that mirror the C++ surface on the M5 firmware.

Submodules:
  capability - Capability enum (host hardware + sub-features) and
               CapabilityMask bitset. Plug-ins declare what they need;
               the host declares what it has; the framework checks
               subset_of() to gate plug-in selection.
"""

from .capability import Capability, CapabilityMask

__all__ = ["Capability", "CapabilityMask"]
