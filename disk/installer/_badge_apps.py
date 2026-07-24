"""Enumerate apps installed on the Tildagon's internal filesystem.

Used by the disk manager to build the Backup + Delete-from-badge
pickers. Reads `/apps/<slug>/metadata.json` for display names +
versions; entries with `hidden: true` are filtered out (that flag
already keeps them off the launcher).

Kept in its own module so host pytest can drive it against a tmp
tree without importing any badge-runtime modules.
"""

import json
import os

BADGE_APPS_ROOT = "/apps"

_S_IFDIR = 0x4000


def _is_dir(path):
    try:
        return (os.stat(path)[0] & _S_IFDIR) != 0
    except OSError:
        return False


def _read_metadata(folder):
    try:
        with open(folder + "/metadata.json") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def list_installed(root=BADGE_APPS_ROOT, exclude_hidden=True):
    """Return a list of {slug, name, version, path} dicts for apps
    under `root`. `slug` is the folder name; `name` and `version`
    come from metadata.json when present (folder name fallback for
    `name`, "?" for `version`)."""
    apps = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return apps
    for name in names:
        path = root + "/" + name
        if not _is_dir(path):
            continue
        meta = _read_metadata(path)
        if exclude_hidden and meta.get("hidden"):
            continue
        apps.append({
            "slug": name,
            "name": meta.get("name") or name,
            "version": meta.get("version") or "?",
            "path": path,
        })
    return apps
