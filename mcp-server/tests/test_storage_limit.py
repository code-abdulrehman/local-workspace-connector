"""
Test: 5 GB hard limit enforcement.
"""
import os
import tempfile
import shutil
import time
from server.quota import QuotaManager, StorageLimitExceeded

MAX_WORKSPACE_BYTES = 5 * 1024 * 1024 * 1024

def test_quota_check_before_write():
    """QuotaManager should reject writes that would exceed the limit."""
    mgr = QuotaManager("/workspace")
    # Start with small usage
    mgr._walk_usage()  # walk
    # Add some dummy files to use up some space
    for i in range(10):
        with open(f"/workspace/test_{i}.txt", "w") as f:
            f.write("x" * 100)
    # Now try to write a file that would exceed limit
    # We need to know current usage
    snap = mgr.snapshot()
    used = snap.used_bytes
    print(f"Current usage: {used} bytes")
    # Try to write a 6 GB file - should fail
    try:
        mgr.check_write("/workspace/big.bin", existing_size=0)
        pytest.fail("Should have raised StorageLimitExceeded")
    except StorageLimitExceeded as e:
        assert e.used_bytes > used
        assert e.needed > 0
        print(f"Correctly rejected: {e}")


def test_quota_check_single_file_limit():
    """Single file should not exceed 500 MB."""
    mgr = QuotaManager("/workspace")
    # Create a 600 MB file
    large_path = "/workspace/big.bin"
    with open(large_path, "wb") as f:
        f.write(b"\x00" * (600 * 1024 * 1024))
    # Check that it was rejected
    try:
        mgr.check_file_too_big(600 * 1024 * 1024)
        pytest.fail("Should have raised StorageLimitExceeded for single file")
    except StorageLimitExceeded:
        pass
    # Clean up
    os.remove(large_path)


def test_quota_verification_after_write():
    """After a successful write, quota should still be within limits."""
    mgr = QuotaManager("/workspace")
    # Start with minimal usage
    for i in range(5):
        with open(f"/workspace/test_{i}.txt", "w") as f:
            f.write("a" * 100)
    snap = mgr.snapshot()
    used = snap.used_bytes
    print(f"Before write: {used} bytes")
    # Write a small file (50 KB)
    small_path = "/workspace/small.bin"
    with open(small_path, "wb") as f:
        f.write(b"\x00" * 51200)
    mgr.check_write(small_path)
    snap2 = mgr.snapshot()
    used2 = snap2.used_bytes
    print(f"After write: {used2} bytes")
    assert used2 <= MAX_WORKSPACE_BYTES
    os.remove(small_path)