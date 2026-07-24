"""Host-side coverage of disk/installer/_badge_apps.py.

Exercises list_installed against tmp trees standing in for /apps.
"""

import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from disk.installer import _badge_apps


def _write_metadata(folder, **fields):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "metadata.json"), "w") as f:
        json.dump(fields, f)


def test_list_installed_empty_root(tmp_path):
    assert _badge_apps.list_installed(str(tmp_path)) == []


def test_list_installed_missing_root_is_empty(tmp_path):
    assert _badge_apps.list_installed(str(tmp_path / "nope")) == []


def test_list_installed_reads_metadata(tmp_path):
    _write_metadata(str(tmp_path / "nocturnation"),
                    name="NocturNation", version="1.0.18")
    _write_metadata(str(tmp_path / "some_other_app"),
                    name="Other", version="0.1.0")

    apps = _badge_apps.list_installed(str(tmp_path))
    assert len(apps) == 2
    slugs = sorted(a["slug"] for a in apps)
    assert slugs == ["nocturnation", "some_other_app"]
    noc = [a for a in apps if a["slug"] == "nocturnation"][0]
    assert noc["name"] == "NocturNation"
    assert noc["version"] == "1.0.18"


def test_list_installed_falls_back_to_slug_for_missing_metadata(tmp_path):
    os.makedirs(str(tmp_path / "nometa"))  # dir exists, no metadata.json
    apps = _badge_apps.list_installed(str(tmp_path))
    assert len(apps) == 1
    entry = apps[0]
    assert entry["slug"] == "nometa"
    assert entry["name"] == "nometa"     # falls back to folder name
    assert entry["version"] == "?"       # sentinel for unknown


def test_list_installed_excludes_hidden(tmp_path):
    _write_metadata(str(tmp_path / "visible"), name="V", version="1")
    _write_metadata(str(tmp_path / "invisible"),
                    name="I", version="1", hidden=True)

    apps = _badge_apps.list_installed(str(tmp_path))
    slugs = sorted(a["slug"] for a in apps)
    assert slugs == ["visible"]


def test_list_installed_includes_hidden_when_asked(tmp_path):
    _write_metadata(str(tmp_path / "visible"), name="V", version="1")
    _write_metadata(str(tmp_path / "invisible"),
                    name="I", version="1", hidden=True)

    apps = _badge_apps.list_installed(str(tmp_path), exclude_hidden=False)
    slugs = sorted(a["slug"] for a in apps)
    assert slugs == ["invisible", "visible"]


def test_list_installed_skips_files_at_root(tmp_path):
    # A stray file at /apps/ root shouldn't be listed as an app.
    with open(str(tmp_path / "stray.txt"), "w") as f:
        f.write("noise")
    _write_metadata(str(tmp_path / "real"), name="R", version="1")

    apps = _badge_apps.list_installed(str(tmp_path))
    assert [a["slug"] for a in apps] == ["real"]


def test_list_installed_survives_bad_metadata(tmp_path):
    os.makedirs(str(tmp_path / "bad"))
    with open(str(tmp_path / "bad" / "metadata.json"), "w") as f:
        f.write("{not valid json")

    apps = _badge_apps.list_installed(str(tmp_path))
    assert len(apps) == 1
    assert apps[0]["slug"] == "bad"
    assert apps[0]["name"] == "bad"      # fell back to slug
    assert apps[0]["version"] == "?"


def test_list_installed_sorted_by_slug(tmp_path):
    for name in ("zebra", "apple", "middle"):
        _write_metadata(str(tmp_path / name), name=name, version="1")
    apps = _badge_apps.list_installed(str(tmp_path))
    assert [a["slug"] for a in apps] == ["apple", "middle", "zebra"]
