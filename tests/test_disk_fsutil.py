"""Host-side coverage of disk/installer/_fsutil.py.

The installer runs on MicroPython on a Tildagon; these tests exercise
the copy / mkdir / rmtree helpers against CPython's os module in tmp
dirs, which is enough to catch structural bugs before flashing.
"""

import os
import sys
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from disk.installer import _fsutil as fsutil


def _write(path, content=b""):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(path, "wb") as f:
        f.write(content)


def test_path_isdir_and_exists(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    f = tmp_path / "f"
    f.write_bytes(b"hi")
    assert fsutil.path_isdir(str(d))
    assert not fsutil.path_isdir(str(f))
    assert fsutil.path_exists(str(d))
    assert fsutil.path_exists(str(f))
    assert not fsutil.path_exists(str(tmp_path / "missing"))


def test_mkdir_p_creates_nested_and_is_idempotent(tmp_path):
    target = tmp_path / "a" / "b" / "c"
    fsutil.mkdir_p(str(target))
    assert target.is_dir()
    # Second call is a no-op, not an error.
    fsutil.mkdir_p(str(target))
    assert target.is_dir()


def test_mkdir_p_raises_when_target_is_a_file(tmp_path):
    f = tmp_path / "x"
    f.write_bytes(b"")
    with pytest.raises(OSError):
        fsutil.mkdir_p(str(f))


def test_rmtree_removes_recursive_tree(tmp_path):
    root = tmp_path / "app"
    _write(str(root / "app.py"), b"code")
    _write(str(root / "nested" / "helper.py"), b"more")
    _write(str(root / "nested" / "deep" / "leaf.py"), b"leaf")
    assert root.is_dir()
    fsutil.rmtree(str(root))
    assert not root.exists()


def test_rmtree_silent_on_missing(tmp_path):
    fsutil.rmtree(str(tmp_path / "definitely-not-here"))


def test_copyfile_streams_bytes(tmp_path):
    src = tmp_path / "src.bin"
    payload = b"x" * (8192 + 17)  # more than one chunk
    src.write_bytes(payload)
    dst = tmp_path / "dst.bin"
    fsutil.copyfile(str(src), str(dst))
    assert dst.read_bytes() == payload


def test_copytree_plan_pairs_files(tmp_path):
    src = tmp_path / "src"
    _write(str(src / "app.py"), b"a")
    _write(str(src / "assets" / "img.jpg"), b"b")
    _write(str(src / "nocturnation" / "protocol" / "frame.py"), b"c")

    dst = tmp_path / "dst"
    plan = fsutil.copytree_plan(str(src), str(dst))
    # {rel_path from dst}
    dst_relatives = sorted(p[1].replace(str(dst), "").lstrip("/") for p in plan)
    assert dst_relatives == [
        "app.py",
        "assets/img.jpg",
        "nocturnation/protocol/frame.py",
    ]


def test_copytree_plan_then_copy_reproduces_tree(tmp_path):
    src = tmp_path / "src"
    _write(str(src / "app.py"), b"top-level")
    _write(str(src / "nested" / "a.py"), b"nested-a")
    _write(str(src / "nested" / "deep" / "b.py"), b"nested-deep-b")

    dst = tmp_path / "dst"
    for s, d in fsutil.copytree_plan(str(src), str(dst)):
        fsutil.ensure_parent_dir(d)
        fsutil.copyfile(s, d)

    assert (dst / "app.py").read_bytes() == b"top-level"
    assert (dst / "nested" / "a.py").read_bytes() == b"nested-a"
    assert (dst / "nested" / "deep" / "b.py").read_bytes() == b"nested-deep-b"


def test_walk_files_yields_only_files(tmp_path):
    src = tmp_path / "src"
    (src / "empty_dir").mkdir(parents=True)
    _write(str(src / "app.py"), b"a")
    _write(str(src / "sub" / "x.py"), b"b")

    entries = list(fsutil.walk_files(str(src)))
    relatives = sorted(e[1] for e in entries)
    assert relatives == ["app.py", "sub/x.py"]


def test_ensure_parent_dir_creates_missing(tmp_path):
    target = tmp_path / "deep" / "path" / "file.py"
    fsutil.ensure_parent_dir(str(target))
    assert target.parent.is_dir()
    # Idempotent.
    fsutil.ensure_parent_dir(str(target))
