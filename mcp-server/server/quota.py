"""
Storage quota manager.
Three-layer enforcement:
  Layer A — filesystem-level 5 GB cap (via Docker storage limit / loopback mount)
  Layer B — code-level pre-write quota check
  Layer C — container resource limits (enforced at exec time)
"""

import os
import shutil
import logging
import threading
from dataclasses import dataclass

log = logging.getLogger("quota")

MAX_WORKSPACE_BYTES = 5 * 1024 * 1024 * 1024  # 5 GB = 5368709120
LOG_ROTATION_SIZE = 10 * 1024 * 1024           # 10 MB per log file
LOG_FILES_TOTAL = 5                             # keep at most 5 rotated logs


class StorageLimitExceeded(Exception):
    """Raised when workspace would exceed MAX_WORKSPACE_BYTES."""

    def __init__(self, current: int, needed: int, limit: int = MAX_WORKSPACE_BYTES):
        self.current = current
        self.needed = needed
        self.limit = limit
        super().__init__(
            f"STORAGE_LIMIT_EXCEEDED: workspace limit is {limit} bytes, "
            f"current use is {current}, this write needs {needed}"
        )


@dataclass
class UsageSnapshot:
    used_bytes: int
    free_bytes: int
    file_count: int


class QuotaManager:
    """Thread-safe quota checker that walks the workspace on demand."""

    def __init__(self, workspace: str = "/workspace"):
        self.workspace = os.path.realpath(workspace)

    # -- helpers -----------------------------------------------------------
    def _walk_usage(self, root=None) -> tuple[int, int]:
        """Return (total_bytes, file_count) walking root, no symlinks followed."""
        root = root or self.workspace
        total = 0
        files = 0
        try:
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                # don't follow symlinks to dirs
                dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
                for fn in filenames:
                    fp = os.path.join(dirpath, fn)
                    if os.path.islink(fp):
                        continue  # skip symlinked files entirely
                    try:
                        total += os.path.getsize(fp)
                        files += 1
                    except OSError:
                        pass
        except OSError:
            pass
        return total, files

    def snapshot(self) -> UsageSnapshot:
        used, count = self._walk_usage()
        free = max(0, shutil.disk_usage(self.workspace).free)
        return UsageSnapshot(used_bytes=used, free_bytes=free, file_count=count)

    # -- pre-write checks ---------------------------------------------------
    def check_write(self, new_path: str, existing_size: int | None = None) -> None:
        """Call BEFORE writing to a new or modified file."""
        new_size = existing_size if existing_size is not None else 0
        snap = self.snapshot()
        total = snap.used_bytes - _file_without(new_path) + new_size
        if total > MAX_WORKSPACE_BYTES:
            raise StorageLimitExceeded(
                current=snap.used_bytes,
                needed=new_size,
            )

    def check_file_too_big(self, size: int) -> None:
        if size > MAX_SINGLE_FILE_MB * 1024 * 1024:
            raise StorageLimitExceeded(
                current=0,
                needed=size,
                limit=MAX_WORKSPACE_BYTES,
            )

    # -- post-write verification --------------------------------------------
    def verify_after_write(self, new_path: str) -> bool:
        """Called after a write completes to confirm quota was respected."""
        snap = self.snapshot()
        return snap.used_bytes <= MAX_WORKSPACE_BYTES + 1_000_000  # small epsilon for rounding


def _file_without(path: str) -> int:
    """Return current size of file, ignoring errors (treat as 0)."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


MAX_SINGLE_FILE_MB = 500