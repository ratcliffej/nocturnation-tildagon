"""cartridge.json manifest helpers.

Schema (v1):
    {
        "name":       str  # display name, e.g. "NocturNation"
        "slug":       str  # folder name to install into, e.g. "nocturnation"
        "version":    str  # version string, e.g. "1.0.18"
        "files":      int  # number of files in the copy job
        "copied_at":  int  # ticks_ms at self-copy time (opaque)
        "manifest_version": 1
    }

Written by the Phase 1 self-copy feature. Read by the Phase 2 installer
to build its selection menu and validate the payload before copying.
"""

import json

MANIFEST_NAME = "cartridge.json"
MANIFEST_VERSION = 1


def read(path):
    """Read a cartridge.json at `path`. Returns dict, or {} on any error."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def build(name, slug, version, file_count, copied_at):
    """Build a manifest dict ready for json.dump()."""
    return {
        "manifest_version": MANIFEST_VERSION,
        "name": name,
        "slug": slug,
        "version": version,
        "files": file_count,
        "copied_at": copied_at,
    }


def display_entry(manifest, fallback_slug):
    """Normalise a manifest into the fields the installer menu needs.

    Missing fields fall back to reasonable defaults so a badly-authored
    cartridge still shows up as installable rather than crashing.
    """
    slug = manifest.get("slug") or fallback_slug
    return {
        "slug": slug,
        "name": manifest.get("name") or slug,
        "version": manifest.get("version") or "?",
        "files": manifest.get("files") or 0,
    }
