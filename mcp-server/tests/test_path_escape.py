"""
Test: Path traversal and symlink escape prevention.
"""
import os
import tempfile
import shutil
from pathlib import Path

import pytest
from server.path_guard import PathGuard, PathOutsideAllowed


def test_path_guard_resolves_symlinks():
    """PathGuard should resolve symlinks and reject paths outside /workspace."""
    guard = PathGuard("/workspace")
    # Normal path should pass
    assert guard.resolve("/workspace/file.txt") == "/workspace/file.txt"
    # Symlink inside workspace should resolve correctly
    link = "/workspace/link_to_file.txt"
    target = "/workspace/real_file.txt"
    os.symlink(target, link)
    assert guard.resolve(link) == target
    # Symlink pointing outside should be rejected
    outside = "/etc/passwd"
    try:
        guard.resolve(outside)
        pytest.fail("Should have rejected path outside workspace")
    except PathOutsideAllowed:
        pass
    os.unlink(link)


def test_path_guard_rejects_absolute_outside():
    """Absolute paths outside workspace should be rejected."""
    guard = PathGuard("/workspace")
    try:
        guard.resolve("/etc/passwd")
        pytest.fail("Should reject absolute path outside workspace")
    except PathOutsideAllowed:
        pass


def test_path_guard_rejects_null_byte():
    """Paths with null bytes should be rejected."""
    guard = PathGuard("/workspace")
    try:
        guard.resolve("/workspace/file\x00.txt")
        pytest.fail("Should reject null byte in path")
    except PathOutsideAllowed:
        pass


def test_path_guard_rejects_control_chars():
    """Control characters should be rejected."""
    guard = PathGuard("/workspace")
    try:
        guard.resolve("/workspace/file\x01.txt")
        pytest.fail("Should reject control character in path")
    except PathOutsideAllowed:
        pass


def test_path_guard_only_workspace_allowed():
    """Only /workspace and its subdirectories should be allowed."""
    guard = PathGuard("/workspace")
    # Should be able to list the workspace
    entries = guard.list_directory()
    assert "workspace" in entries["directory"]
    # Any attempt to escape should fail
    with pytest.raises(PathOutsideAllowed):
        guard.resolve("/etc/passwd")