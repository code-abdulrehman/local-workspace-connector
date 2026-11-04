"""
ExecSandbox — implements the exec_sandboxed / exec_status / read_exec_log tools.
Each exec spawns ONE fresh short-lived Docker container.
"""

import os
import subprocess
import threading
import time
import json
import logging
import uuid

logger = logging.getLogger("exec_sandbox")

ALLOWED_COMMANDS = [
    "ls", "cat", "mkdir", "rmdir", "echo",
    "python3", "pip", "elixir", "mix",
    "node", "npm", "npx",
    "grep", "head", "tail", "wc", "find",
]

METACHARS = set(";|&$()`<>\\!*?[](){}\n\r\t")


class ShellMetacharRejected(Exception):
    pass


class CommandNotAllowed(Exception):
    pass


class ExecSandbox:
    """Base class that other tool modules extend."""

    def __init__(self, allowed_commands=None, network_profile="none", workspace="/workspace", auth_token=None, registry=None):
        # Registry of background jobs
        self._exec_registry = registry or {}
        self._lock = threading.Lock()
        self.allowed_commands = allowed_commands or ALLOWED_COMMANDS

    # -- shell metachar check -------------------------------------------------
    def _validate_no_shell_metachars(self, args):
        for a in args:
            if isinstance(a, str):
                for ch in a:
                    if ch in METACHARS:
                        raise ShellMetacharRejected(f"shell metacharacter rejected: '{a}'")
            elif isinstance(a, list):
                for sub in a:
                    self._validate_no_shell_metachars([sub])

    def _validate_command(self, cmd_args):
        """Validate command against allowlist."""
        if not isinstance(cmd_args, list) or len(cmd_args) == 0:
            raise CommandNotAllowed("command must be a non-empty list")
        first = cmd_args[0]
        if first not in ALLOWED_COMMANDS:
            raise CommandNotAllowed(f"command '{first}' is not in allowlist")

    # -- background job registry --------------------------------------------

    def _register_job(self, job_id, started, timeout):
        with self._lock:
            self._exec_registry[job_id] = {
                "job_id": job_id,
                "started": started,
                "timeout": timeout,
                "exit_code": None,
                "stdout_tail": "",
                "stderr_tail": "",
                "finished": False,
                "killed": False,
                "storage_usage": None,
            }

    def _finalize_job(self, job_id, exit_code, stdout_tail, storage_usage, killed=False):
        with self._lock:
            info = self._exec_registry.get(job_id)
            if info:
                info["exit_code"] = exit_code
                info["stdout_tail"] = stdout_tail[-512:] if stdout_tail else ""
                info["finished"] = True
                info["killed"] = killed
                info["storage_usage"] = storage_usage

    # -- actual Docker exec spawn ------------------------------------------

    def _spawn_docker_container(self, cmd_args, timeout_seconds):
        """Run one command in an ephemeral Docker container with hard limits."""
        job_id = "j_" + uuid.uuid4().hex[:8]
        job_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".sandbox", "jobs", job_id + ".log")
        job_log_path = os.path.normpath(job_log_path)
        done_marker = os.path.join(os.path.dirname(job_log_path), job_id + ".done")
        os.makedirs(os.path.dirname(job_log_path), exist_ok=True)

        full_cmd = [
            "docker", "run", "--rm",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--read-only",
            "--tmpfs", "/tmp:size=256m,noexec",
            "--memory", "2g", "--memory-swap", "2g", "--cpus", "2",
            "--pids-limit", "100",
            "--network", "none",
            "--user", "1000",
            "alpine:3.19",  # minimal base image
        ] + cmd_args

        proc = subprocess.Popen(
            full_cmd,
            cwd="/workspace",
            stdin=subprocess.PIPE,
            stdout=open(job_log_path, "wb"),
            stderr=subprocess.STDOUT,
            text=True,
        )

        # kill after timeout
        timer = threading.Timer(timeout_seconds, lambda: proc.kill())
        timer.daemon = True
        timer.start()

        try:
            proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        finally:
            timer.cancel()

        exit_code = proc.returncode
        # mark done
        with open(done_marker, "w") as _f:
            _f.write("DONE\n")

        # collect stdout tail
        stdout_tail = ""
        try:
            with open(job_log_path, "r") as f:
                lines = f.readlines()
                stdout_tail = "".join(lines[-10:]) if lines else ""
        except OSError:
            pass

        # collect storage usage
        storage_usage = None
        try:
            snap = self._snapshot_storage()
            storage_usage = {"used_bytes": snap.used_bytes, "file_count": snap.file_count}
        except Exception:
            pass

        return job_id, exit_code, stdout_tail

    def _execute(self, cmd_args, timeout_minutes=30):
        """Public entry point used by tools."""
        self._validate_no_shell_metachars(cmd_args)
        self._validate_command(cmd_args)
        timeout_seconds = timeout_minutes * 60
        job_id, rc, stdout = self._spawn_docker_container(cmd_args, timeout_seconds)
        snap = self._snapshot_storage()
        self._finalize_job(job_id, rc, stdout, snap, killed=False)
        status = "succeeded" if rc == 0 else "failed"
        return {
            "job_id": job_id,
            "status": status,
            "exit_code": rc,
            "stdout": stdout,
            "storage_usage": snap,
        }

    def _snapshot_storage(self):
        """Snapshot workspace usage for reporting."""
        total = 0
        files = 0
        for dp, dns, fns in os.walk("/workspace", followlinks=False):
            dns[:] = [d for d in dns if not os.path.islink(os.path.join(dp, d))]
            for fn in fns:
                fp = os.path.join(dp, fn)
                if os.path.islink(fp):
                    continue
                try:
                    total += os.path.getsize(fp)
                    files += 1
                except OSError:
                    pass
        return {"used_bytes": total, "file_count": files}

    # -- public API -----------------------------------------------------------
    def spawn_background(self, cmd_args, timeout_minutes=30):
        """Start a background exec job."""
        self._validate_no_shell_metachars(cmd_args)
        self._validate_command(cmd_args)
        timeout_seconds = min(timeout_minutes * 60, 1800)
        thread = threading.Thread(target=self._execute_timed, args=(cmd_args, timeout_seconds), daemon=True)
        thread.start()
        # Wait briefly for job registration, then return "running"
        for _ in range(50):
            with self._lock:
                ids = list(self._exec_registry.keys())
            if any(i.startswith("j_") for i in ids):
                break
            time.sleep(0.1)
        job_id = list(self._exec_registry.keys())[-1].replace("j_", "")
        return {"job_id": job_id, "status": "running"}

    def get_job_status(self, job_id):
        with self._lock:
            info = self._exec_registry.get(job_id)
            if not info:
                return {"status": "unknown"}
            return {
                "job_id": info["job_id"],
                "status": "succeeded" if info["exit_code"] == 0 else "failed",
                "exit_code": info.get("exit_code"),
                "storage_usage": info.get("storage_usage"),
                "killed": info.get("killed", False),
            }

    def read_job_log(self, job_id, offset=0, limit=1024):
        path = os.path.join(".sandbox", "jobs", job_id + ".log")
        if not os.path.isfile(path):
            return {"job_id": job_id, "logs": "", "finished": False}
        try:
            with open(path, "rb") as f:
                data = f.read(limit)
            pos = max(0, offset)
            end = pos + limit
            remaining = data[pos:end]
            return {"job_id": job_id, "logs": remaining.decode("utf-8", errors="replace"), "finished": os.path.isfile(os.path.join(".sandbox", "jobs", job_id + ".done"))}
        except Exception:
            return {"job_id": job_id, "logs": "", "error": "read error"}

    # -- internal -----------------------------------------------------------
    def _execute_timed(self, cmd_args, timeout_seconds):
        """Run command with timeout in Docker container."""
        job_id, rc, stdout = self._spawn_docker_container(cmd_args, timeout_seconds)
        return {
            "job_id": job_id,
            "status": "succeeded" if rc == 0 else "failed",
            "exit_code": rc,
            "stdout": stdout,
            "storage_usage": self._snapshot_storage(),
        }

    def _pack_job(self, info):
        return {
            "job_id": info.get("job_id"),
            "status": "succeeded" if info.get("exit_code") == 0 else "failed",
            "exit_code": info.get("exit_code"),
            "stdout": info.get("stdout_tail", ""),
            "storage_usage": info.get("storage_usage", {}),
        }
