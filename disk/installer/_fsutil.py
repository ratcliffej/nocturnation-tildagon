"""Filesystem helpers for the disk installer.

Kept in a MicroPython + CPython dual-runtime shape:
- Uses only `os` (no `shutil`, no `pathlib` - both missing on Tildagon).
- No Tildagon-only imports, so host pytest can exercise the copytree
  logic end to end against a temp directory.

Design constraints:
- MicroPython os.stat returns a tuple; the mode is at index 0. Bit
  0x4000 = directory (S_IFDIR). Same on CPython.
- os.listdir order is not guaranteed; caller sorts if needed.
- File copies stream in 4 KB chunks so a large install doesn't allocate
  the whole payload at once (the badge has ~1.4 MB free during install).
"""

import os

_S_IFDIR = 0x4000
_COPY_CHUNK = 4096


def path_isdir(path):
    try:
        return (os.stat(path)[0] & _S_IFDIR) != 0
    except OSError:
        return False


def path_exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def mkdir_p(path):
    """Create `path` and every missing parent. No-op if it already exists."""
    if not path or path == "/":
        return
    parts = path.strip("/").split("/")
    cur = ""
    for p in parts:
        cur = cur + "/" + p
        try:
            os.mkdir(cur)
        except OSError:
            if not path_isdir(cur):
                raise


def rmtree(path):
    """Recursively remove `path`. Silent no-op if it doesn't exist."""
    if not path_exists(path):
        return
    if not path_isdir(path):
        os.remove(path)
        return
    for name in os.listdir(path):
        entry = _join(path, name)
        if path_isdir(entry):
            rmtree(entry)
        else:
            os.remove(entry)
    os.rmdir(path)


def copyfile(src, dst):
    """Stream-copy a single file. Overwrites dst if it exists."""
    with open(src, "rb") as fr:
        with open(dst, "wb") as fw:
            while True:
                chunk = fr.read(_COPY_CHUNK)
                if not chunk:
                    break
                fw.write(chunk)


def walk_files(src):
    """Yield (src_path, rel_path) for every file under `src`, recursively.

    `rel_path` is relative to `src` and uses "/" as the separator. Used
    by the installer to enumerate a copy job up front so it can show
    per-file progress and handle each file as its own tick.
    """
    stack = [("",)]
    while stack:
        rel_dir = stack.pop()[0]
        abs_dir = src if not rel_dir else _join(src, rel_dir)
        try:
            names = os.listdir(abs_dir)
        except OSError:
            continue
        for name in names:
            abs_entry = _join(abs_dir, name)
            rel_entry = name if not rel_dir else _join(rel_dir, name)
            if path_isdir(abs_entry):
                stack.append((rel_entry,))
            else:
                yield abs_entry, rel_entry


def copytree_plan(src, dst):
    """Build a list of (src_file, dst_file) pairs for a src -> dst copy.

    Kept separate from execution so the installer can size the progress
    bar accurately + handle each file transactionally.
    """
    return [(sp, _join(dst, rp)) for (sp, rp) in walk_files(src)]


def ensure_parent_dir(path):
    """Make sure the directory containing `path` exists."""
    idx = path.rfind("/")
    if idx <= 0:
        return
    mkdir_p(path[:idx])


def _join(a, b):
    if not a:
        return b
    if a.endswith("/"):
        return a + b
    return a + "/" + b
