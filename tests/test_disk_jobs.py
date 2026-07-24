"""Host-side coverage of disk/installer/_jobs.py.

CopyJob + DeleteJob state machines exercised against tmp dirs.
The disk manager app itself is Tildagon-runtime only; these tests
cover the pieces the app orchestrates but doesn't own.
"""

import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from disk.installer import _jobs
from disk.installer import _manifest as manifest


def _write(path, content=b""):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(path, "wb") as f:
        f.write(content)


def _fake_app_tree(root):
    _write(str(root / "app.py"), b"# app\n")
    _write(str(root / "metadata.json"), b'{"version": "1.0"}\n')
    _write(str(root / "sub" / "helper.py"), b"# helper\n")


def test_copyjob_prepare_and_full_run(tmp_path):
    src = tmp_path / "src"
    _fake_app_tree(src)
    dst = tmp_path / "dst"

    job = _jobs.CopyJob(src_dir=str(src), dst_dir=str(dst))
    assert not job.done
    assert job.queue is None

    # First tick builds the queue
    job.tick()
    assert job.queue is not None
    assert job.total == 3
    assert job.cursor == 1  # first tick also copied one file after prepare

    # Actually the first tick after queue-build does one file, so
    # we already ate one. Two more files then finalise.
    while not job.done:
        job.tick()

    assert job.error is None
    assert (dst / "app.py").read_bytes() == b"# app\n"
    assert (dst / "metadata.json").read_bytes() == b'{"version": "1.0"}\n'
    assert (dst / "sub" / "helper.py").read_bytes() == b"# helper\n"


def test_copyjob_wipes_existing_dst(tmp_path):
    src = tmp_path / "src"
    _fake_app_tree(src)
    dst = tmp_path / "dst"
    _write(str(dst / "stale.py"), b"leftover")

    job = _jobs.CopyJob(src_dir=str(src), dst_dir=str(dst))
    while not job.done:
        job.tick()

    assert not (dst / "stale.py").exists()
    assert (dst / "app.py").exists()


def test_copyjob_with_manifest_writes_disk_json(tmp_path):
    src = tmp_path / "src"
    _fake_app_tree(src)
    dst = tmp_path / "dst"

    m = manifest.build("Test App", "testapp", "1.2.3", 3, 42)
    job = _jobs.CopyJob(src_dir=str(src), dst_dir=str(dst),
                        manifest_data=m)
    while not job.done:
        job.tick()

    assert job.error is None
    dj = json.loads((dst / manifest.MANIFEST_NAME).read_text())
    assert dj["slug"] == "testapp"
    assert dj["copied_at"] == 42


def test_copyjob_without_manifest_writes_nothing_extra(tmp_path):
    src = tmp_path / "src"
    _fake_app_tree(src)
    dst = tmp_path / "dst"

    job = _jobs.CopyJob(src_dir=str(src), dst_dir=str(dst))
    while not job.done:
        job.tick()

    assert not (dst / manifest.MANIFEST_NAME).exists()


def test_copyjob_tick_after_done_is_noop(tmp_path):
    src = tmp_path / "src"
    _fake_app_tree(src)
    dst = tmp_path / "dst"
    job = _jobs.CopyJob(src_dir=str(src), dst_dir=str(dst))
    while not job.done:
        job.tick()
    before_cursor = job.cursor
    job.tick()
    job.tick()
    assert job.cursor == before_cursor
    assert job.done


def test_deletejob_removes_tree(tmp_path):
    root = tmp_path / "app"
    _fake_app_tree(root)
    assert root.is_dir()

    job = _jobs.DeleteJob(path=str(root))
    assert not job.done
    job.tick()
    assert job.done
    assert job.error is None
    assert not root.exists()


def test_deletejob_missing_path_is_silent(tmp_path):
    # rmtree short-circuits on missing paths; job succeeds.
    job = _jobs.DeleteJob(path=str(tmp_path / "not-here"))
    job.tick()
    assert job.done
    assert job.error is None


def test_deletejob_tick_after_done_is_noop(tmp_path):
    root = tmp_path / "app"
    _fake_app_tree(root)
    job = _jobs.DeleteJob(path=str(root))
    job.tick()
    job.tick()
    assert job.done
    assert job.error is None
