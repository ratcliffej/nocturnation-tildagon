"""Job state machines used by the disk manager.

Each job represents one operation the operator kicked off from the
hub menu (install app to badge, backup app to disk, delete from
either side). Ticks are driven from the app's background_update -
one file per tick for copies, one shot per tick for deletes - so
draw() keeps rendering progress and the badge stays responsive.

Kept in a separate module from app.py so host pytest can drive the
state machines against tmp dirs without importing any badge-runtime
modules.
"""

import json

from . import _fsutil as fsutil
from . import _manifest as manifest


class CopyJob:
    """Copy a directory tree file-by-file.

    Ticks: on the first tick, wipes dst and builds the file queue.
    Each subsequent tick copies one file. When the queue empties,
    writes an optional manifest and marks done.

    `manifest_data` is passed straight to json.dump; omit for
    install (which reuses the source's existing cartridge/disk
    manifest untouched) and set for backup (which writes a fresh
    manifest describing the just-copied contents).
    """

    def __init__(self, src_dir, dst_dir, manifest_data=None):
        self.src_dir = src_dir
        self.dst_dir = dst_dir
        self.manifest_data = manifest_data
        self.queue = None       # lazy - built in first tick
        self.cursor = 0
        self.total = 0
        self.error = None
        self.done = False

    def tick(self):
        if self.done:
            return
        if self.queue is None:
            self._prepare()
            if self.done:
                return
        if self.cursor >= self.total:
            self._finalise()
            self.done = True
            return
        src, dst = self.queue[self.cursor]
        try:
            fsutil.ensure_parent_dir(dst)
            fsutil.copyfile(src, dst)
        except OSError as exc:
            self.error = "Copy: %s" % exc
            self.done = True
            return
        self.cursor += 1

    def _prepare(self):
        try:
            fsutil.rmtree(self.dst_dir)
            fsutil.mkdir_p(self.dst_dir)
        except OSError as exc:
            self.error = "Prepare: %s" % exc
            self.done = True
            return
        self.queue = fsutil.copytree_plan(self.src_dir, self.dst_dir)
        self.total = len(self.queue)

    def _finalise(self):
        if self.manifest_data is None:
            return
        try:
            with open(self.dst_dir + "/" + manifest.MANIFEST_NAME, "w") as f:
                json.dump(self.manifest_data, f)
        except OSError as exc:
            self.error = "Manifest: %s" % exc


class DeleteJob:
    """Recursively delete a directory. Fast enough to complete in
    one tick even for a fully-installed app - rmtree is O(files)
    but each os.remove is microseconds. No progress reporting."""

    def __init__(self, path):
        self.path = path
        self.done = False
        self.error = None

    def tick(self):
        if self.done:
            return
        try:
            fsutil.rmtree(self.path)
        except OSError as exc:
            self.error = "Delete: %s" % exc
        self.done = True
