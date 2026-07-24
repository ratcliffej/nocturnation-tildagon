"""Self-copy of the running NocturNation Lume app onto a Flopagon
cartridge. Wired into the Lume app's idle menu as "Copy to Flopagon".

Produces the same layout the Phase 2 installer expects:
    <mount>/apps/<slug>/
        cartridge.json      (v1 manifest)
        app.py              (plus all other app files, recursively)
        ...

`<mount>` is auto-detected in this order:
    /cartridge   — our Phase 3 bootstrap's mount
    /flopagon    — Nathan's stock Flopagon app mount

`<slug>` is derived from the source directory basename, so a badge
running `/apps/nocturnation/app.py` writes to `<mount>/apps/nocturnation/`
and a store-installed `/apps/ratcliffej_nocturnation/app.py` writes to
`<mount>/apps/ratcliffej_nocturnation/`. The installer uses the same
slug when copying back to a fresh badge.

Copy progresses file-by-file via .tick() so the caller's UI loop can
show progress without blocking. Errors are captured in .error and
stop the job cleanly instead of raising into the app's event loop.
"""

import os

try:
    import json
except ImportError:  # pragma: no cover
    json = None

from .installer import _fsutil as fsutil
from .installer import _manifest as manifest

# Priority order: our own bootstrap's mount first (semantic clarity),
# Nathan's second (stock Flopagon). Any other storage hexpansion using
# a different mount path won't be detected - v1 scope.
_KNOWN_MOUNTS = ("/cartridge", "/flopagon")


def detect_mount():
    """Return the mount path of an inserted Flopagon-like filesystem
    the operator can copy onto, or None if none is currently
    reachable.
    """
    for path in _KNOWN_MOUNTS:
        try:
            os.listdir(path)
        except OSError:
            continue
        return path
    return None


def source_dir_from_file(file_path):
    """Given an app.py's `__file__` attribute, return the parent
    directory (which is the app's install location on the badge).
    """
    idx = file_path.rfind("/")
    if idx <= 0:
        return "/"
    return file_path[:idx]


def slug_from_source(src_dir):
    """Basename of the source directory - matches the installed folder
    name on any badge that receives this cartridge later.
    """
    idx = src_dir.rfind("/")
    if idx < 0:
        return src_dir
    return src_dir[idx + 1:]


class SelfCopyJob:
    """One-shot copy state machine. Instantiate, call .prepare()
    once, then .tick() per UI frame until .done. Progress via
    .cursor / .total; failure via .error (truthy = failed)."""

    def __init__(self, src_dir, mount_dir, slug, name, version,
                 copied_at=0):
        self.src_dir = src_dir
        self.mount_dir = mount_dir
        self.slug = slug
        self.name = name
        self.version = version
        self.copied_at = copied_at

        self.dst_apps_dir = mount_dir + "/apps"
        self.dst_dir = self.dst_apps_dir + "/" + slug

        self.queue = fsutil.copytree_plan(src_dir, self.dst_dir)
        self.total = len(self.queue)
        self.cursor = 0
        self.error = None
        self.done = False

    def prepare(self):
        """Wipe any existing target folder and recreate directory
        structure. Called once before the first .tick(). Sets .error
        + .done on failure."""
        try:
            fsutil.mkdir_p(self.dst_apps_dir)
            fsutil.rmtree(self.dst_dir)
            fsutil.mkdir_p(self.dst_dir)
        except OSError as exc:
            self.error = "Prepare: %s" % exc
            self.done = True

    def tick(self):
        """Copy one file (or finalise on empty queue). Returns True
        while work remains, False when done (successful or errored)."""
        if self.done:
            return False
        if self.cursor >= self.total:
            self._finalise()
            return False
        src, dst = self.queue[self.cursor]
        try:
            fsutil.ensure_parent_dir(dst)
            fsutil.copyfile(src, dst)
        except OSError as exc:
            self.error = "Copy: %s" % exc
            self.done = True
            return False
        self.cursor += 1
        return True

    def _finalise(self):
        if json is None:
            self.error = "No JSON"
            self.done = True
            return
        try:
            m = manifest.build(
                name=self.name,
                slug=self.slug,
                version=self.version,
                file_count=self.total,
                copied_at=self.copied_at,
            )
            with open(self.dst_dir + "/" + manifest.MANIFEST_NAME, "w") as f:
                json.dump(m, f)
        except OSError as exc:
            self.error = "Manifest: %s" % exc
        self.done = True
