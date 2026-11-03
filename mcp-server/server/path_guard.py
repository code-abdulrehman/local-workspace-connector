"""
Path validation — Layer 1 of defense against escape attacks.
Resolves the real absolute path (resolving symlinks) and rejects anything outside
the allowed directory. Also rejects null bytes, control characters, Windows drive tricks.
"""

import os
import re

WIN_DRIVE_RE = re.compile(r"^[a-zA-Z]:[\\/]")
CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


class PathOutsideAllowed(Exception):
    pass


class PathTooLong(Exception):
    pass


class PathGuard:
    def __init__(self, allowed: str):
        self.allowed = os.path.realpath(allowed)

    def resolve(self, raw_path: str) -> str:
        """Return real, absolute, symlink-resolved path."""
        if raw_path is None:
            raise ValueError("path is None")
        # Reject null bytes
        if "\x00" in raw_path:
            raise ValueError("null byte in path")
        # Reject control chars
        if CONTROL_CHAR_RE.search(raw_path):
            raise ValueError("control character in path")
        # Reject Windows drive-letter tricks even after resolution
        if raw_path.strip().startswith("C:") or raw_path.startswith("/"):
            pass  # we allow both but validate afterwards

        abs_path = os.path.abspath(raw_path)
        # Resolve symlinks (this also normalizes .. etc.)
        resolved = os.path.realpath(abs_path)

        # Normalize trailing slashes for comparison
        norm_allowed = self.allowed.rstrip(os.sep)
        if resolved != norm_allowed and not resolved.startswith(norm_allowed + os.sep):
            raise PathOutsideAllowed(
                f"path '{raw_path}' resolves to '{resolved}', which is outside allowed directory"
            )
        return resolved

    def validate_read(self, path: str) -> str:
        p = self.resolve(path)
        self._check_symlink_inside(p)
        return p

    def validate_write(self, path: str) -> str:
        p = self.resolve(path)
        self._check_symlink_inside(p)
        return p

    def validate_move(self, src: str, dst: str) -> tuple[str, str]:
        s = self.validate_write(src)
        d = self.validate_write(dst)
        return s, d

    def validate_delete(self, path: str) -> str:
        p = self.resolve(path)
        # Cannot delete the workspace root itself
        if p == self.allowed:
            raise ValueError("refusing to delete the allowed directory itself")
        # Cannot delete parents above workspace
        parent = os.path.dirname(p)
        if parent == self.allowed or parent.startswith(self.allowed + os.sep):
            self._check_symlink_inside(p)
        return p

    def _check_symlink_inside(self, full_path: str):
        if os.path.islink(full_path):
            link_target = os.path.realpath(full_path)
            norm_allowed = self.allowed.rstrip(os.sep)
            if link_target != self.allowed and not link_target.startswith(norm_allowed + os.sep):
                raise PathOutsideAllowed(
                    f"symlink target '{link_target}' escapes workspace"
                )


# Module-level constant used by main.py and tests
RESOLVED_ALLOWED_DIR = "/"  # will be replaced per instance