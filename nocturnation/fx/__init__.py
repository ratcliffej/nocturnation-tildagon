"""FX library framework (Epic 10).

Generic parameterised effects engine. The wire protocol carries a
LIGHT_FX_RUN frame with an fx_id + BPM + buildup + six generic
params; the receiver looks up fx_id in this registry and runs the
matching Fx class. Adding a new effect = subclassing Fx + decorating
with @fx_registry.register.

Effects authored here run on the Tildagon Director-as-Lume; the
StickC will get its own port of the same framework in a later epic
(currently uses the Show framework directly).
"""

from .base import Fx
from .registry import FxRegistry, fx_registry
from .runner import FxRunner

__all__ = [
    "Fx",
    "FxRegistry",
    "fx_registry",
    "FxRunner",
]
