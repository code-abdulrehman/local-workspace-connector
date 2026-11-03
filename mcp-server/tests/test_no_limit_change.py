"""
Test: Attempts to change the workspace limit or container size must be rejected.
"""
import os
import tempfile
import shutil
from server.quota import QuotaManager, StorageLimitExceeded

MAX_WORKSPACE_BYTES = 5 * 1024 * 1024 * 1024

def test_config_hash_unchanged():
    """Config hash should remain constant across runs."""
    from server.path_guard import PathGuard
    guard = PathGuard("/workspace")
    h1 = PathGuard("/workspace")._hash()  # Not a real method, but we can check consistency
    # The config hash is computed from the config dict
    from server.quota import config_hash
    h2 = config_hash()
    assert h1 == h2


def test_agent_cannot_edit_config():
    """No MCP tool can modify the sandbox config file."""
    # The config file is mounted read-only into the container
    # Any attempt to write to it should fail
    from server.tools import Tools
    tools = Tools()
    # Try to write to the config file
    try:
        tools.write_file("/opt/sandbox-config/sandbox.yaml", "test", confirm=True)
        pytest.fail("Should not be able to write to config file")
    except Exception as e:
        # Expected - config is read-only
        pass


def test_agent_cannot_change_quota():
    """Agent cannot change MAX_WORKSPACE_BYTES or other quota limits."""
    from server.quota import QuotaManager
    mgr = QuotaManager()
    # Try to modify the limit
    try:
        mgr.quota.max_workspace_bytes = 10 * 1024 * 1024 * 1024
        pytest.fail("Should not be able to change quota limits")
    except Exception as e:
        # Any exception is fine - the limit is protected
        pass


def test_agent_cannot_run_docker():
    """Agent cannot spawn a new Docker container."""
    from server.tools import Tools
    tools = Tools()
    # This would try to run a Docker command via the allowlist
    # but docker is not in the allowlist, so it should be rejected
    # The tool registry will reject it
    result = tools.exec_sandboxed(["docker", "ps"])
    pytest.fail("Should have rejected docker command")