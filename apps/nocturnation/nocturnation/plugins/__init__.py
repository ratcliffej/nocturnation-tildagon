"""Plugin framework for NocturNation Tildagon.

Mirrors the C++ plugin surface in nocturnation-m5 at
include/plugins/plugin.h. The five public types are:

  PropertyType - tag for the value shape (Bool/U8/U16/Colour/Enum)
  PropertyDef  - schema entry; plug-ins return a list of these from
                 `properties()`
  PowerProfile - what the plug-in needs from the host pipeline
  PluginKind   - which registry / NVS-namespace prefix the plug-in
                 lives under
  Plugin       - the base class

Property *values* are stored as native Python types in PropertyBag
(bool / int). The PropertyType on the PropertyDef tells the Settings
overlay how to render the value; the storage layer doesn't need a
tagged-union wrapper the way C++ does.
"""

from .plugin import (
    PluginKind,
    PowerProfile,
    PropertyDef,
    PropertyType,
    Plugin,
)
from .property_bag import PropertyBag

__all__ = [
    "PluginKind",
    "PowerProfile",
    "PropertyDef",
    "PropertyType",
    "Plugin",
    "PropertyBag",
]
