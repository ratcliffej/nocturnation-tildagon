"""Host-side coverage of disk/installer/_manifest.py."""

import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from disk.installer import _manifest as manifest


def test_read_returns_dict_for_valid_manifest(tmp_path):
    p = tmp_path / "disk.json"
    p.write_text(json.dumps({
        "name": "NocturNation",
        "slug": "nocturnation",
        "version": "1.0.18",
        "files": 51,
        "copied_at": 12345,
        "manifest_version": 1,
    }))
    assert manifest.read(str(p))["slug"] == "nocturnation"
    assert manifest.read(str(p))["files"] == 51


def test_read_returns_empty_on_missing_file(tmp_path):
    assert manifest.read(str(tmp_path / "missing.json")) == {}


def test_read_returns_empty_on_invalid_json(tmp_path):
    p = tmp_path / "disk.json"
    p.write_text("{not json at all")
    assert manifest.read(str(p)) == {}


def test_read_returns_empty_on_non_dict_json(tmp_path):
    p = tmp_path / "disk.json"
    p.write_text("[1, 2, 3]")
    assert manifest.read(str(p)) == {}


def test_build_shape():
    m = manifest.build("NocturNation", "nocturnation", "1.0.18", 51, 999)
    assert m == {
        "manifest_version": 1,
        "name": "NocturNation",
        "slug": "nocturnation",
        "version": "1.0.18",
        "files": 51,
        "copied_at": 999,
    }


def test_display_entry_falls_back_when_slug_missing():
    m = {"name": "Weird App", "version": "0.1"}
    entry = manifest.display_entry(m, fallback_slug="weird_app")
    assert entry["slug"] == "weird_app"
    assert entry["name"] == "Weird App"
    assert entry["version"] == "0.1"


def test_display_entry_falls_back_when_everything_missing():
    entry = manifest.display_entry({}, fallback_slug="mystery")
    assert entry == {
        "slug": "mystery",
        "name": "mystery",
        "version": "?",
        "files": 0,
    }


def test_display_entry_prefers_manifest_over_fallback():
    m = {"slug": "canonical", "name": "Canonical", "version": "9.9.9", "files": 7}
    entry = manifest.display_entry(m, fallback_slug="ignored")
    assert entry["slug"] == "canonical"
    assert entry["files"] == 7
