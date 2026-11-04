"""
MCP tool definitions and implementations.
Tools:
  list_allowed_directories  - return /workspace only
  list_directory            - list workspace contents
  read_text_file            - read up to 1 MB, with pagination
  write_file                - create or overwrite after quota check
  edit_file                 - replace text block after quota check
  create_directory          - create dir in workspace
  move_file                 - rename/move inside workspace
  delete_file               - single file delete (requires confirm=true)
  get_storage_status        - limit/used/free/percent/filecount
  exec_sandboxed            - spawn allowlisted Docker container in background
  exec_status               - poll background job status
  read_exec_log             - paginated read of job log file
"""

import os
import json
import shutil
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timezone

from .quota import QuotaManager, StorageLimitExceeded, MAX_WORKSPACE_BYTES
from .path_guard import PathGuard, PathOutsideAllowed, PathTooLong
from .exec_sandbox import ExecSandbox

logger = logging.getLogger("tools")

TOOL_ERROR = "ToolError"

class ToolError(Exception):
    pass


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
def _log(tool: str, resolved_path: str | None, action: str, success: bool):
    ts = datetime.now(timezone.utc).isoformat()
    entry = json.dumps({
        "timestamp": ts,
        "tool": tool,
        "action": action,
        "resolved_path": resolved_path,
        "success": success,
    })
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_DIR + "/tools.log", "a") as f:
            f.write(entry + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Thin wrapper class so main.py can do getattr(exec_sandbox, name)
# ---------------------------------------------------------------------------
class ToolRegistry(ExecSandbox):
    """Tool implementations that share path_guard and quota_mgr from main."""

    def __init__(self, workspace: str):
        super().__init__()
        self._workspace = workspace
        self.path_guard = PathGuard(allowed=workspace)
        self.quota = QuotaManager(workspace=workspace)


# We subclass ExecSandbox but override methods directly instead, so that
# the exec logic lives here too.
class Tools(ExecSandbox):
    def __init__(self, workspace: str = "/workspace", job_timeout_seconds: int = 30 * 60, allowed_commands=None):
        super().__init__(allowed_commands=allowed_commands)
        self.workspace = os.path.realpath(workspace)
        self.sandbox_workspace = os.path.realpath(
            os.environ.get("SANDBOX_WORKSPACE_HOST_PATH", self.workspace)
        )
        self.pg = PathGuard(allowed=workspace)
        self.quota = QuotaManager(workspace=workspace)
        self.job_timeout_seconds = job_timeout_seconds
        self.job_dir = os.path.join(self.workspace, ".sandbox", "jobs")
        os.makedirs(self.job_dir, exist_ok=True)

    # -- error helpers ------------------------------------------------------
    def __getattr__(self, name):
        return lambda *a, **kw: TOOL_ERROR("tool not found: %s" % name)

    # ======================================================================
    # Tools — each returns a dict suitable for MCP JSON-RPC result
    # ======================================================================

    def list_allowed_directories(self, _params=None):
        """Return exactly one entry: /workspace."""
        out = {"allowed_directory": "/workspace"}
        _log("list_allowed_directories", None, "list", True)
        return out

    def list_directory(self, params):
        d = params.get("path") or self.workspace
        resolved = self.pg.resolve(d)
        if not os.path.isdir(resolved):
            raise ValueError(f"'{resolved}' is not a directory")
        items = []
        try:
            entries = sorted(os.listdir(resolved), key=lambda x: (not os.path.isdir(os.path.join(resolved, x)), x))
            for e in entries:
                full = os.path.join(resolved, e)
                items.append({
                    "name": e,
                    "type": "dir" if os.path.isdir(full) else "file",
                    "size": os.path.getsize(full) if os.path.isfile(full) else 0,
                })
        except Exception as e:
            raise ValueError(f"failed to list directory: {e}")
        _log("list_directory", resolved, "list", True)
        return {"directory": resolved, "entries": items}

    def read_text_file(self, params):
        full = self.pg.validate_read(params["path"])
        if not os.path.isfile(full):
            raise ValueError(f"'{full}' is not a regular file")
        # Reject binary by trying to decode
        try:
            with open(full, "r", errors="strict") as f:
                raw = f.read()
            raw.encode("utf-8").decode("utf-8")  # validate it's text
        except UnicodeDecodeError:
            raise ValueError("binary file not supported")
        except Exception as e:
            raise ValueError(f"cannot read file: {e}")
        size = len(raw.encode("utf-8"))
        if size > 1024 * 1024:
            raise ValueError("file exceeds 1 MB read limit; use head/tail pagination")
        _log("read_text_file", full, "read", True)
        return {"path": full, "content": raw, "size": size}

    def write_file(self, params):
        target = self.pg.validate_write(params["path"])
        if os.path.exists(target) and os.path.islink(target):
            raise ValueError("cannot overwrite symlink outside workspace")
        mode = params.get("mode", "w")
        content = params.get("content", "")
        size = len(content.encode("utf-8"))
        self.quota.check_file_too_big(size)
        self.quota.check_write(target, existing_size=0 if not os.path.exists(target) else os.path.getsize(target))
        with open(target, mode) as f:
            f.write(content)
        if not self.quota.verify_after_write(target):
            # rollback on failure
            shutil.copymode(target, params.get("__original_mode", target))
            os.remove(target)
            raise ValueError("quota verification failed after write")
        _log("write_file", target, "write", True)
        return {"path": target, "size": size}

    def edit_file(self, params):
        target = self.pg.validate_write(params["path"])
        if not os.path.isfile(target):
            raise ValueError(f"'{target}' is not a file")
        old = params.get("old_string", "")
        new = params.get("new_string", "")
        if not old:
            raise ValueError("old_string required for edit")
        with open(target, "r") as f:
            text = f.read()
        if old not in text:
            raise ValueError(f"search string '{old}' not found in {target}")
        new_text = text.replace(old, new, 1)
        size_delta = len(new.encode("utf-8")) - len(old.encode("utf-8"))
        self.quota.check_write(target, existing_size=len(text.encode("utf-8")))
        with open(target, "w") as f:
            f.write(new_text)
        if not self.quota.verify_after_write(target):
            raise ValueError("quota verification failed after edit")
        _log("edit_file", target, "edit", True)
        return {"path": target, "matches": text.count(old)}

    def create_directory(self, params):
        target = self.pg.validate_write(params["path"])
        if os.path.exists(target) and os.path.isdir(target):
            return {"created": False, "path": target}
        os.makedirs(target, exist_ok=True)
        _log("create_directory", target, "mkdir", True)
        return {"created": True, "path": target}

    def move_file(self, params):
        src, dst = self.pg.validate_move(params["source"], params["destination"])
        # prevent replacing an existing destination accidentally
        dst_parent = os.path.dirname(dst) or "."
        if os.path.lexists(dst):
            raise ValueError(f"destination '{dst}' already exists")
        shutil.move(src, dst)
        _log("move_file", f"{src}->{dst}", "move", True)
        return {"moved_from": src, "moved_to": dst}

    def delete_file(self, params):
        confirm = params.get("confirm", False)
        path = params.get("path")
        resolved = self.pg.resolve(path)
        if not confirm:
            raise ValueError("confirm=true required to delete")
        rp = self.pg.validate_delete(path)
        if not confirm:
            raise ValueError("confirm=true required to delete")
        os.remove(rp)
        _log("delete_file", rp, "delete", True)
        return {"deleted": rp}

    def get_storage_status(self, _params=None):
        snap = self.quota.snapshot()
        return {
            "limit_bytes": MAX_WORKSPACE_BYTES,
            "used_bytes": snap.used_bytes,
            "free_bytes": snap.free_bytes,
            "percent_used": round(snap.used_bytes / MAX_WORKSPACE_BYTES * 100, 2),
            "file_count": snap.file_count,
            "allowed_directory": "/workspace",
        }

    # -- exec tools ----------------------------------------------------------

    def exec_sandboxed(self, params):
        cmd_args = params.get("command") or params.get("args") or []
        job_id = params.get("job_id")
        if isinstance(cmd_args, str):
            cmd_args = [cmd_args]
        try:
            job_timeout = int(params.get("timeout_minutes", self.job_timeout_seconds // 60))
        except (TypeError, ValueError):
            raise ValueError("timeout_minutes must be a positive integer")
        max_timeout_minutes = max(1, self.job_timeout_seconds // 60)
        if job_timeout < 1:
            raise ValueError("timeout_minutes must be a positive integer")
        job_timeout = min(job_timeout, max_timeout_minutes)

        # validate no shell metacharacters anywhere
        self._validate_no_shell_metachars(cmd_args)

        # validate first command against allowlist
        first = cmd_args[0] if cmd_args else ""
        if first not in self.allowed_commands:
            _log("exec_sandboxed", first, "exec_reject", False)
            raise ValueError(f"command '{first}' is not in the allowlist")

        if job_id:
            return self.exec_status({"job_id": job_id})

        job_id = "j_" + uuid.uuid4().hex[:12]
        job = {
            "job_id": job_id,
            "started": time.time(),
            "timeout": job_timeout * 60,
            "finished": False,
            "exit_code": None,
            "stdout_tail": "",
            "stderr_tail": "",
            "storage_usage": None,
            "killed": False,
        }
        with self._lock:
            self._exec_registry[job_id] = job
        thread = threading.Thread(
            target=self._spawn_exec_sync,
            args=(job_id, cmd_args, job_timeout),
            daemon=True,
        )
        thread.start()
        return {"job_id": job_id, "status": "running"}

    def exec_status(self, params):
        job_id = params.get("job_id")
        if not isinstance(job_id, str) or not re.fullmatch(r"j_[0-9a-f]{12}", job_id):
            return {"status": "unknown", "message": f"unknown job_id {job_id}"}
        with self._lock:
            info = self._exec_registry.get(job_id)
            if not info:
                return {"status": "unknown", "message": f"unknown job_id {job_id}"}
            remaining = info["started"] + info["timeout"] - time.time()
            if remaining <= 0 and not info.get("finished"):
                info["killed"] = True
            return {
                "job_id": job_id,
                "status": "running" if not info.get("finished") else ("succeeded" if info.get("exit_code") == 0 else "failed"),
                "exit_code": info.get("exit_code"),
                "stdout_tail": info.get("stdout_tail", b""),
                "stderr_tail": info.get("stderr_tail", b""),
                "storage_usage": info.get("storage_usage", {}),
                "killed": info.get("killed", False),
            }

    def read_exec_log(self, params):
        job_id = params.get("job_id")
        if not isinstance(job_id, str) or not re.fullmatch(r"j_[0-9a-f]{12}", job_id):
            return {"job_id": job_id, "logs": "", "finished": False}
        start = int(params.get("offset", 0))
        limit = int(params.get("limit", 1024))
        if start < 0 or limit < 0:
            raise ValueError("offset and limit must be non-negative")
        job_path = os.path.join(self.job_dir, job_id + ".log")
        if not os.path.isfile(job_path):
            return {"job_id": job_id, "logs": "", "finished": False}
        try:
            with open(job_path, "rb") as f:
                f.seek(start)
                data = f.read(limit)
            return {"job_id": job_id, "logs": data.decode("utf-8", errors="replace"), "finished": os.path.isfile(os.path.join(self.job_dir, job_id + ".done"))}
        except Exception as e:
            return {"job_id": job_id, "logs": "", "error": str(e)}

    # -- internal -----------------------------------------------------------

    def _validate_no_shell_metachars(self, args: list):
        metachars = set(";|&$()`<>\\!*?[](){}\n\r\t")
        for a in args:
            if isinstance(a, str):
                for ch in a:
                    if ch in metachars:
                        raise ValueError(f"shell metacharacter in argument: '{a}'")
            elif isinstance(a, list):
                for sub in a:
                    self._validate_no_shell_metachars([sub])

    def _spawn_exec_sync(self, job_id, cmd_args, timeout_minutes):
        """Run an allowlisted command in a constrained ephemeral container."""
        job = self._exec_registry[job_id]
        log_path = os.path.join(self.job_dir, job_id + ".log")
        done_path = os.path.join(self.job_dir, job_id + ".done")
        binary = cmd_args[0]
        if binary in {"node", "npm", "npx", "pnpm"}:
            image = "muse-sandbox-node:22"
        elif binary in {"python3", "pip"}:
            image = "python:3.11-alpine"
        else:
            image = "alpine:3.19"

        os.makedirs(self.job_dir, exist_ok=True)
        try:
            import docker
            client = docker.from_env()
            container = client.containers.run(
                image,
                cmd_args,
                detach=True,
                auto_remove=False,
                working_dir="/workspace",
                user="1000:1000",
                volumes={self.sandbox_workspace: {"bind": "/workspace", "mode": "rw"}},
                network_disabled=binary != "pnpm",
                read_only=True,
                tmpfs={"/tmp": "rw,noexec,nosuid,size=256m"},
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                mem_limit="2g",
                nano_cpus=2_000_000_000,
                pids_limit=100,
            )
            try:
                result = container.wait(timeout=max(1, min(timeout_minutes * 60, self.job_timeout_seconds)))
                exit_code = result.get("StatusCode", 1)
            except Exception:
                container.kill()
                exit_code = 124
                job["killed"] = True
            finally:
                with open(log_path, "wb") as log_file:
                    log_file.write(container.logs(stdout=True, stderr=True))
                container.remove(force=True)
            client.close()
        except Exception as error:
            exit_code = 1
            with open(log_path, "a", encoding="utf-8") as log_file:
                log_file.write(f"\n{type(error).__name__}: {error}\n")
        finally:
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as log_file:
                    job["stdout_tail"] = log_file.read()[-4096:]
            except OSError:
                pass
            job["exit_code"] = exit_code
            job["finished"] = True
            try:
                snap = self.quota.snapshot()
                job["storage_usage"] = {"used_bytes": snap.used_bytes, "file_count": snap.file_count}
            except Exception:
                pass
            with open(done_path, "w", encoding="utf-8") as marker:
                marker.write("DONE\n")

    def _poll_or_get_last(self, job_id):
        """Poll the registry for last known status."""
        info = getattr(self._exec_registry, "_jobs", {}).get(job_id)
        if not info:
            # try reading marker
            job_path = os.path.join(JOB_DIR, job_id + ".done")
            if os.path.isfile(job_path):
                with open(job_path, "rb") as f:
                    return {"job_id": job_id, "status": "succeeded", "exit_code": 0, "stdout": f.read().decode()}
            return {"job_id": job_id, "status": "running", "note": "still running"}
        return self._pack_job(info)

    # -- registry ----------------------------------------------------------------

    def _pack_job(self, info):
        return {
            "job_id": info.get("job_id"),
            "status": "succeeded" if info.get("exit_code") == 0 else "failed",
            "exit_code": info.get("exit_code"),
            "stdout": info.get("stdout_tail", ""),
            "storage_usage": info.get("storage_usage", {}),
        }


def build_tool_definitions():
    return [
        {"function": {"name": "list_allowed_directories", "description": "Returns exactly one entry: /workspace.", "parameters": {"type": "object", "properties": {}, "required": []}}},
        {"function": {"name": "list_directory", "description": "List files/folders inside /workspace only.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}}, "required": ["path"]}}},
        {"function": {"name": "read_text_file", "description": "Read a text file inside /workspace only. Max 1 MB per call.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}}, "required": ["path"]}}},
        {"function": {"name": "write_file", "description": "Create or overwrite a file inside /workspace only, after quota check.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "mode": {"type": "string"}, "confirm": {"type": "boolean"}}, "required": ["path", "content"]}}},
        {"function": {"name": "edit_file", "description": "Replace an exact text block inside a file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}}, "required": ["path", "old_string", "new_string"]}}},
        {"function": {"name": "create_directory", "description": "Create a folder inside /workspace only.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
        {"function": {"name": "move_file", "description": "Move/rename inside /workspace only.", "parameters": {"type": "object", "properties": {"source": {"type": "string"}, "destination": {"type": "string"}}, "required": ["source", "destination"]}}},
        {"function": {"name": "delete_file", "description": "Delete a single file inside /workspace. Requires confirm=true.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "confirm": {"type": "boolean"}}, "required": ["path", "confirm"]}}},
        {"function": {"name": "get_storage_status", "description": "Return workspace usage stats.", "parameters": {"type": "object", "properties": {}, "required": []}}},
        {"function": {"name": "exec_sandboxed", "description": "Spawn a sandbox container for a limited command.", "parameters": {"type": "object", "properties": {"command": {"type": "array", "items": {"type": "string"}}, "job_id": {"type": "string"}, "timeout_minutes": {"type": "integer"}}, "required": ["command"]}}},
        {"function": {"name": "exec_status", "description": "Check status of a background exec job.", "parameters": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]}}},
        {"function": {"name": "read_exec_log", "description": "Read paginated log of an exec job.", "parameters": {"type": "object", "properties": {"job_id": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["job_id"]}}},
    ]


def uuid_local():
    import uuid
    return uuid.uuid4().hex
