"""Show framework for the NocturNation Tildagon Director.

Mirrors `nocturnation::shows` on the M5 firmware: the `Show` base
class (`show.py`), the `ShowContext` services surface
(`show_context.py`), and the registry that auto-discovers concrete
Shows from `apps/nocturnation/shows/<show_id>/` (`registry.py`).

Concrete Shows live at the top of the app directory
(`apps/nocturnation/shows/<show_id>/__init__.py`), separate from
the framework code here. Drop a new folder in there, expose a
factory at module scope, and it appears in the picker at the next
boot.
"""

from .show import Show
from .show_context import ShowContext
from .registry import ShowRegistry, show_registry, discover_shows

__all__ = [
    "Show",
    "ShowContext",
    "ShowRegistry",
    "show_registry",
    "discover_shows",
]
