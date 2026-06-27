"""Host tests for the DirID-keyed image-path resolver (Epic 13 Phase 2A).

The loader's policy is what we pin here:

* Filename convention ``dirid_<hex>.jpg`` (lowercase, two-digit).
* DirID hit returns the matching file's path.
* DirID miss falls back to ``default.jpg``.
* No matching file and no default returns ``None``.
* Subsequent calls with the same DirID return the cached path
  without re-statting the filesystem.

The renderer-side composition (``ctx.image(path, x, y, w, h)``) is
bench-only; this test suite covers the pure-logic resolver.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nocturnation import images


@pytest.fixture(autouse=True)
def reset_loader_state():
    images.clear()
    images._state.last_load_failed_path = None
    yield
    images.clear()
    images._state.last_load_failed_path = None


@pytest.fixture
def image_dir(tmp_path, monkeypatch):
    """Redirect the loader's image directory into a temp path."""
    d = tmp_path / "images"
    d.mkdir()
    monkeypatch.setattr(images, "_IMAGE_DIR", str(d))
    return d


def _touch(path: Path, contents: bytes = b"jpg-stub") -> None:
    """Create a stub file at the expected path. Content doesn't
    matter for the resolver - existence is what's checked."""
    path.write_bytes(contents)


def test_filename_for_dir_id_basic():
    assert images._filename_for_dir_id(0xD0) == "dirid_d0.jpg"
    assert images._filename_for_dir_id(0x40) == "dirid_40.jpg"
    assert images._filename_for_dir_id(0xFE) == "dirid_fe.jpg"
    assert images._filename_for_dir_id(0x00) == "dirid_00.jpg"


def test_filename_for_dir_id_none_or_out_of_range():
    assert images._filename_for_dir_id(None) is None
    assert images._filename_for_dir_id(-1) is None
    assert images._filename_for_dir_id(0x100) is None


def test_path_hit_returns_specific(image_dir):
    _touch(image_dir / "dirid_d0.jpg", b"specific")
    _touch(image_dir / "default.jpg",  b"default")
    p = images.path_for_dir_id(0xD0)
    assert p is not None
    assert p.endswith("dirid_d0.jpg")


def test_path_miss_falls_back_to_default(image_dir):
    _touch(image_dir / "default.jpg")
    p = images.path_for_dir_id(0xA1)
    assert p is not None
    assert p.endswith("default.jpg")


def test_path_none_dir_id_uses_default(image_dir):
    _touch(image_dir / "default.jpg")
    p = images.path_for_dir_id(None)
    assert p is not None
    assert p.endswith("default.jpg")


def test_no_default_returns_none(image_dir):
    assert images.path_for_dir_id(0xD0) is None


def test_cache_hit_skips_filesystem(image_dir):
    _touch(image_dir / "dirid_d0.jpg")
    p1 = images.path_for_dir_id(0xD0)
    # Remove the file on disk; cached call should still return the
    # cached path without rechecking.
    (image_dir / "dirid_d0.jpg").unlink()
    p2 = images.path_for_dir_id(0xD0)
    assert p1 == p2


def test_dir_id_change_invalidates_cache(image_dir):
    _touch(image_dir / "dirid_d0.jpg")
    _touch(image_dir / "dirid_a1.jpg")
    p_d0 = images.path_for_dir_id(0xD0)
    p_a1 = images.path_for_dir_id(0xA1)
    assert p_d0.endswith("dirid_d0.jpg")
    assert p_a1.endswith("dirid_a1.jpg")


def test_current_path_after_load(image_dir):
    _touch(image_dir / "default.jpg")
    images.path_for_dir_id(0xA1)
    assert images.current_path() is not None
    assert images.current_path().endswith("default.jpg")


def test_current_path_when_no_load(image_dir):
    assert images.current_path() is None


def test_clear_drops_cached_path(image_dir):
    _touch(image_dir / "default.jpg")
    images.path_for_dir_id(0xA1)
    assert images.current_path() is not None
    images.clear()
    assert images.current_path() is None
