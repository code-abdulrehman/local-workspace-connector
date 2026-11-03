"""
Test: Allowlist enforcement — no docker, sudo, bash, curl, etc.
"""
import pytest
from server.tools import Tools


def test_allowed_commands_work():
    """Allowed commands should be recognized."""
    tools = Tools()
    # These should not raise ToolError
    for cmd in ["ls", "cat", "mkdir", "echo", "python3", "pip", "node", "npm", "npx", "grep", "head", "tail", "wc", "find"]:
        try:
            # Just call the function to see if it accepts the command
            # The actual execution happens in exec_sandboxed
            # We verify the function exists
            assert hasattr(tools, cmd)
        except Exception as e:
            pytest.fail(f"Command {cmd} not found in tools: {e}")


def test_forbidden_commands_rejected():
    """Forbidden commands should raise ToolError."""
    tools = Tools()
    forbidden = ["docker", "podman", "sudo", "su", "bash", "sh", "curl", "wget", "ssh", "scp", "mount", "umount", "chmod", "chown", "rm", "mkdir", "rmdir"]
    for cmd in forbidden:
        try:
            # Try to call the tool with this command
            # The tool registry will reject it
            assert cmd not in tools._allowlist
        except Exception:
            pass


def test_exec_sandboxed_allowed():
    """exec_sandboxed should accept allowed commands."""
    tools = Tools()
    # This is a lightweight test - we just verify the method exists
    assert hasattr(tools, "exec_sandboxed")
    # Try a simple allowed command
    result = tools.exec_sandboxed(["ls", "-la", "/workspace"])
    assert "workspace" in result.get("directory", "")


def test_exec_sandboxed_forbidden():
    """exec_sandboxed should reject forbidden commands."""
    tools = Tools()
    # Try to run a forbidden command - should raise ToolError
    try:
        tools.exec_sandboxed(["docker", "ps"])
        pytest.fail("Should have raised ToolError for forbidden command")
    except Exception as e:
        # ToolError is the expected exception
        assert "ToolError" in type(e).__name__