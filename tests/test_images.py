"""Host tests for the DirID-keyed image loader (Epic 13 Phase 2A).

The loader's policy is what we pin here:

* Filename convention ``dirid_<hex>.raw`` (lowercase, two-digit).
* DirID hit returns the matching file's bytes.
* DirID miss falls back to ``default.raw``.
* No matching file and no default returns ``None``.
* Wrong file size is refused.
* Subsequent calls with the same DirID return the cached buffer (no
  re-read).

The renderer-side composition (ctx.texture + draw) is bench-only;
this test suite covers the pure-logic loader.
"""

from __future__ import annotations

import importlib
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


def _write_blob(path: Path, byte: int = 0xAB) -> None:
    """Write a valid-size RGB565 blob filled with a constant byte."""
    path.write_bytes(bytes([byte]) * images.EXPECTED_SIZE)


def test_filename_for_dir_id_basic():
    assert images._filename_for_dir_id(0xD0) == "dirid_d0.raw"
    assert images._filename_for_dir_id(0x40) == "dirid_40.raw"
    assert images._filename_for_dir_id(0xFE) == "dirid_fe.raw"
    assert images._filename_for_dir_id(0x00) == "dirid_00.raw"


def test_filename_for_dir_id_none_or_out_of_range():
    assert images._filename_for_dir_id(None) is None
    assert images._filename_for_dir_id(-1) is None
    assert images._filename_for_dir_id(0x100) is None


def test_load_hit_returns_blob(image_dir):
    _write_blob(image_dir / "dirid_d0.raw", byte=0x11)
    _write_blob(image_dir / "default.raw", byte=0x22)
    buf = images.load_for_dir_id(0xD0)
    assert buf is not None
    assert len(buf) == images.EXPECTED_SIZE
    # First byte is from the specific file, not the default.
    assert buf[0] == 0x11


def test_load_miss_falls_back_to_default(image_dir):
    _write_blob(image_dir / "default.raw", byte=0x33)
    # 0xA1 has no specific file -> default.
    buf = images.load_for_dir_id(0xA1)
    assert buf is not None
    assert buf[0] == 0x33


def test_load_none_dir_id_uses_default(image_dir):
    _write_blob(image_dir / "default.raw", byte=0x44)
    buf = images.load_for_dir_id(None)
    assert buf is not None
    assert buf[0] == 0x44


def test_load_no_default_returns_none(image_dir):
    # No files at all.
    buf = images.load_for_dir_id(0xD0)
    assert buf is None


def test_load_wrong_size_refused(image_dir):
    # Write a too-small file under a DirID that the loader will try.
    (image_dir / "dirid_d0.raw").write_bytes(b"\x00" * 100)
    # Default is correct size; should fall back to it.
    _write_blob(image_dir / "default.raw", byte=0x55)
    buf = images.load_for_dir_id(0xD0)
    assert buf is not None
    assert buf[0] == 0x55   # used the default, not the bad-sized file


def test_cache_hit_skips_reload(image_dir, monkeypatch):
    _write_blob(image_dir / "dirid_d0.raw", byte=0x66)
    buf1 = images.load_for_dir_id(0xD0)
    # Mutate file on disk; cached call shouldn't see the change.
    (image_dir / "dirid_d0.raw").write_bytes(bytes([0x77]) * images.EXPECTED_SIZE)
    buf2 = images.load_for_dir_id(0xD0)
    # Same Python object (caching is what we're testing).
    assert buf1 is buf2
    assert buf1[0] == 0x66


def test_dir_id_change_invalidates_cache(image_dir):
    _write_blob(image_dir / "dirid_d0.raw", byte=0x88)
    _write_blob(image_dir / "dirid_a1.raw", byte=0x99)
    a = images.load_for_dir_id(0xD0)
    b = images.load_for_dir_id(0xA1)
    assert a[0] == 0x88
    assert b[0] == 0x99


def test_current_returns_dimensions(image_dir):
    _write_blob(image_dir / "default.raw", byte=0xAA)
    images.load_for_dir_id(0xA1)
    buf, w, h, stride = images.current()
    assert buf is not None
    assert w == 240
    assert h == 240
    assert stride == 480   # 240 * 2 bytes/pixel


def test_current_when_no_load_returns_none(image_dir):
    buf, w, h, stride = images.current()
    assert buf is None
    assert w == 0
    assert h == 0
    assert stride == 0


def test_clear_drops_cached_image(image_dir):
    _write_blob(image_dir / "default.raw")
    images.load_for_dir_id(0xA1)
    assert images.current()[0] is not None
    images.clear()
    assert images.current() == (None, 0, 0, 0)
