"""Host-side coverage of cartridge/self_copy.py.

Exercises the SelfCopyJob state machine + detect_mount + slug helpers
against tmp dirs. The Lume-app integration (menu wiring, draw / update
overlay) is Tildagon-runtime only and needs hardware validation.
"""

import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from cartridge import self_copy
from cartridge.installer import _manifest as manifest


def _write(path, content=b""):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(path, "wb") as f:
        f.write(content)


def _fake_app_tree(root):
    _write(str(root / "app.py"), b"# app entry\n")
    _write(str(root / "metadata.json"), b'{"version": "9.9.9"}\n')
    _write(str(root / "sub" / "helper.py"), b"# helper\n")
    _write(str(root / "sub" / "deeper" / "leaf.py"), b"leaf")


def test_source_dir_from_file_strips_basename():
    assert self_copy.source_dir_from_file("/apps/nocturnation/app.py") == "/apps/nocturnation"
    assert self_copy.source_dir_from_file("/apps/ratcliffej_nocturnation/app.py") == "/apps/ratcliffej_nocturnation"


def test_source_dir_from_file_root_fallback():
    assert self_copy.source_dir_from_file("app.py") == "/"


def test_slug_from_source_takes_basename():
    assert self_copy.slug_from_source("/apps/nocturnation") == "nocturnation"
    assert self_copy.slug_from_source("/apps/ratcliffej_nocturnation") == "ratcliffej_nocturnation"


def test_selfcopyjob_prepare_and_full_tick(tmp_path):
    src = tmp_path / "src"
    _fake_app_tree(src)
    mount = tmp_path / "mount"
    mount.mkdir()

    job = self_copy.SelfCopyJob(
        src_dir=str(src),
        mount_dir=str(mount),
        slug="testapp",
        name="Test App",
        version="1.2.3",
        copied_at=42,
    )
    assert job.total == 4  # 4 files in _fake_app_tree
    assert job.cursor == 0
    assert not job.done
    assert job.error is None

    job.prepare()
    assert not job.done
    assert (mount / "apps" / "testapp").is_dir()

    # Tick through every file.
    while job.tick():
        pass
    assert job.done
    assert job.error is None

    dst = mount / "apps" / "testapp"
    assert (dst / "app.py").read_bytes() == b"# app entry\n"
    assert (dst / "metadata.json").read_bytes() == b'{"version": "9.9.9"}\n'
    assert (dst / "sub" / "helper.py").read_bytes() == b"# helper\n"
    assert (dst / "sub" / "deeper" / "leaf.py").read_bytes() == b"leaf"

    m = json.loads((dst / manifest.MANIFEST_NAME).read_text())
    assert m == {
        "manifest_version": 1,
        "name": "Test App",
        "slug": "testapp",
        "version": "1.2.3",
        "files": 4,
        "copied_at": 42,
    }


def test_selfcopyjob_wipes_prior_dst(tmp_path):
    src = tmp_path / "src"
    _fake_app_tree(src)
    mount = tmp_path / "mount"
    stale_dst = mount / "apps" / "testapp" / "stale_file.py"
    _write(str(stale_dst), b"leftover from earlier install")

    job = self_copy.SelfCopyJob(
        src_dir=str(src), mount_dir=str(mount),
        slug="testapp", name="x", version="0", copied_at=0)
    job.prepare()
    while job.tick():
        pass

    assert not stale_dst.exists()
    assert (mount / "apps" / "testapp" / "app.py").exists()


def test_selfcopyjob_progress_reporting(tmp_path):
    src = tmp_path / "src"
    _fake_app_tree(src)
    mount = tmp_path / "mount"
    mount.mkdir()
    job = self_copy.SelfCopyJob(
        src_dir=str(src), mount_dir=str(mount),
        slug="testapp", name="x", version="0", copied_at=0)
    job.prepare()

    total = job.total
    assert total > 0
    for expected in range(1, total + 1):
        assert job.tick()
        assert job.cursor == expected
    # One more tick finalises + writes manifest.
    assert not job.tick()
    assert job.done
    assert job.error is None


def test_detect_mount_returns_none_when_no_paths(monkeypatch):
    # Force detect to look at paths that don't exist.
    monkeypatch.setattr(self_copy, "_KNOWN_MOUNTS",
                        ("/no/such/path/1", "/no/such/path/2"))
    assert self_copy.detect_mount() is None


def test_detect_mount_finds_first_available(tmp_path, monkeypatch):
    m1 = tmp_path / "cartridge"
    m1.mkdir()
    m2 = tmp_path / "flopagon"
    m2.mkdir()
    monkeypatch.setattr(self_copy, "_KNOWN_MOUNTS", (str(m1), str(m2)))
    assert self_copy.detect_mount() == str(m1)


def test_detect_mount_falls_back_to_second(tmp_path, monkeypatch):
    m2 = tmp_path / "flopagon"
    m2.mkdir()
    monkeypatch.setattr(self_copy, "_KNOWN_MOUNTS",
                        ("/no/such", str(m2)))
    assert self_copy.detect_mount() == str(m2)
