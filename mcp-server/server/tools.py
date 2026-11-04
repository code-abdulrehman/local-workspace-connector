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
import hashlib
import shutil
import logging
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone

from .quota import (
    MAX_CONFIGURABLE_WORKSPACE_LIMIT_GB,
    QuotaManager,
    StorageLimitExceeded,
    set_workspace_limit_gb,
)
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
        self.pg = PathGuard(allowed=workspace, aliases=[self.sandbox_workspace])
        self.quota = QuotaManager(workspace=workspace)
        self.job_timeout_seconds = job_timeout_seconds
        self.job_dir = os.path.join(self.workspace, ".sandbox", "jobs")
        self._permission_requests = {}
        self._services = {}
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
            "limit_bytes": self.quota.max_workspace_bytes,
            "used_bytes": snap.used_bytes,
            "free_bytes": snap.free_bytes,
            "percent_used": round(snap.used_bytes / self.quota.max_workspace_bytes * 100, 2),
            "file_count": snap.file_count,
            "allowed_directory": "/workspace",
        }

    # -- exec tools ----------------------------------------------------------

    def sandbox_request_permission(self, params):
        """Create a short-lived user approval request; never grants access itself."""
        scope = params.get("scope")
        reason = str(params.get("reason", "")).strip()
        if scope not in {
            "network", "service:postgres", "service:mongodb",
            "runtime:base", "runtime:elixir", "runtime:python", "storage:increase",
        }:
            raise ValueError("unsupported permission scope")
        if not reason or len(reason) > 500:
            raise ValueError("reason must be between 1 and 500 characters")
        requested_limit_gb = None
        if scope == "storage:increase":
            try:
                requested_limit_gb = float(params.get("limit_gb"))
            except (TypeError, ValueError):
                raise ValueError("limit_gb is required for storage:increase")
            current_limit_gb = self.quota.max_workspace_bytes / (1024 ** 3)
            if not current_limit_gb < requested_limit_gb <= MAX_CONFIGURABLE_WORKSPACE_LIMIT_GB:
                raise ValueError(
                    f"requested limit_gb must be greater than the current {current_limit_gb:g} GB and at most {MAX_CONFIGURABLE_WORKSPACE_LIMIT_GB} GB"
                )
        request_id = "p_" + secrets.token_hex(8)
        with self._lock:
            self._permission_requests[request_id] = {
                "scope": scope,
                "reason": reason,
                "created": time.time(),
                "expires": time.time() + 600,
                "status": "pending",
                "limit_gb": requested_limit_gb,
            }
        return {
            "request_id": request_id,
            "scope": scope,
            "reason": reason,
            "status": "pending_user_approval",
            "expires_in_seconds": 600,
            "user_prompt": (
                f"Approve increasing the workspace quota to {requested_limit_gb:g} GB? Reason: {reason}"
                if scope == "storage:increase"
                else f"Approve {scope} for this isolated sandbox task? Reason: {reason}"
            ),
            "approval_phrase": f"approve:{request_id}",
            "instruction": "Show the request to the user and wait for an explicit yes before calling sandbox_approve_permission.",
        }

    def sandbox_capabilities(self, _params=None):
        return {
            "runtimes": ["node", "python", "elixir", "base"],
            "temporary_services": ["postgres", "mongodb"],
            "workspace_limit_gb": self.quota.max_workspace_bytes / (1024 ** 3),
            "project_isolation": "Pass project_path to mount only that project into command containers.",
            "network": "Disabled by default; package downloads require a one-use network permission.",
            "approval_flow": ["sandbox_request_permission", "show the request to the user and wait", "sandbox_approve_permission"],
            "service_data": "Temporary in-memory storage; removed on stop or expiry.",
        }

    def sandbox_approve_permission(self, params):
        """Activate a one-use grant only after the connector agent gets user approval."""
        request_id = params.get("request_id")
        phrase = params.get("user_confirmation", "")
        approved = params.get("approved") is True
        with self._lock:
            request = self._permission_requests.get(request_id)
            if not request or request["status"] != "pending":
                raise ValueError("permission request is unknown or already used")
            if request["expires"] <= time.time():
                request["status"] = "expired"
                raise ValueError("permission request expired; request approval again")
            if phrase != f"approve:{request_id}":
                raise ValueError("user_confirmation must exactly match the approval phrase returned in the request")
            request["status"] = "approved" if approved else "denied"
            request["grant"] = secrets.token_urlsafe(24) if approved else None
        return {
            "request_id": request_id,
            "status": request["status"],
            "permission_id": request.get("grant"),
            "scope": request["scope"],
            "limit_gb": request.get("limit_gb"),
            "uses": 1 if approved else 0,
        }

    def sandbox_set_storage_limit(self, params):
        request = self._consume_permission(params.get("permission_id"), "storage:increase")
        requested_limit_gb = request.get("limit_gb")
        if requested_limit_gb is None:
            raise ValueError("approved storage request has no limit")
        set_workspace_limit_gb(requested_limit_gb)
        return {
            "limit_gb": requested_limit_gb,
            "limit_bytes": self.quota.max_workspace_bytes,
            "persistence": "current server process; set MCP_WORKSPACE_LIMIT_GB in .env to retain after restart",
        }

    def _consume_permission(self, permission_id, scope):
        if not isinstance(permission_id, str):
            raise ValueError(f"permission required: request and obtain user approval for {scope}")
        with self._lock:
            for request in self._permission_requests.values():
                if request.get("grant") == permission_id and request["scope"] == scope:
                    request["grant"] = None
                    return request
        raise ValueError(f"valid one-use approval for {scope} is required")

    def _project_mount(self, project_path):
        resolved = self.pg.resolve(project_path or self.workspace)
        if not os.path.isdir(resolved):
            raise ValueError("project_path must identify an existing directory inside /workspace")
        relative = os.path.relpath(resolved, self.workspace)
        host_path = self.sandbox_workspace if relative == "." else os.path.join(self.sandbox_workspace, relative)
        return resolved, os.path.realpath(host_path)

    def _runtime_storage(self, project_path):
        project_key = hashlib.sha256(project_path.encode("utf-8")).hexdigest()[:16]
        container_path = self.pg.validate_write(os.path.join(self.workspace, ".sandbox", "runtimes", project_key))
        relative = os.path.relpath(container_path, self.workspace)
        host_path = self.sandbox_workspace if relative == "." else os.path.join(self.sandbox_workspace, relative)
        # Create the runtime storage directory on the container-side workspace
        # bind mount (/workspace, which is writable). The host-side path is only
        # consumed later as the docker bind source for the exec container.
        container_dir = self.workspace if relative == "." else os.path.join(self.workspace, relative)
        os.makedirs(container_dir, exist_ok=True)
        return os.path.realpath(host_path)

    def sandbox_reset_runtime(self, params):
        if params.get("confirm") is not True:
            raise ValueError("confirm=true is required to reset the project's runtime packages")
        project_path, _ = self._project_mount(params.get("project_path"))
        project_key = hashlib.sha256(project_path.encode("utf-8")).hexdigest()[:16]
        reset_dir = os.path.join(self.workspace, ".sandbox", "runtimes", project_key)
        shutil.rmtree(reset_dir, ignore_errors=True)
        return {"project_path": project_path, "status": "runtime_packages_removed"}

    def sandbox_start_service(self, params):
        service = params.get("service")
        if service not in {"postgres", "mongodb"}:
            raise ValueError("service must be postgres or mongodb")
        project_path, project_host_path = self._project_mount(params.get("project_path"))
        ttl_minutes = params.get("ttl_minutes", 60)
        if not isinstance(ttl_minutes, int) or isinstance(ttl_minutes, bool) or not 1 <= ttl_minutes <= 240:
            raise ValueError("ttl_minutes must be an integer from 1 to 240")
        self._consume_permission(params.get("permission_id"), f"service:{service}")

        service_id = "s_" + secrets.token_hex(6)
        network_name = "mcp-svc-" + secrets.token_hex(6)
        username = "app"
        password = secrets.token_urlsafe(24)
        database = "app"
        image = "postgres:17-alpine" if service == "postgres" else "mongo:8"
        client = None
        try:
            import docker
            client = docker.from_env()
            client.networks.create(network_name, driver="bridge", internal=True)
            environment = (
                {"POSTGRES_DB": database, "POSTGRES_PASSWORD": password, "POSTGRES_USER": username}
                if service == "postgres"
                else {"MONGO_INITDB_ROOT_USERNAME": username, "MONGO_INITDB_ROOT_PASSWORD": password}
            )
            data_path = "/var/lib/postgresql/data" if service == "postgres" else "/data/db"
            container = client.containers.run(
                image,
                name=network_name,
                detach=True,
                auto_remove=False,
                network=network_name,
                hostname="db",
                environment=environment,
                read_only=True,
                tmpfs={data_path: "rw,nosuid,size=1g", "/tmp": "rw,nosuid,size=64m"},
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                mem_limit="1g",
                nano_cpus=1_000_000_000,
                pids_limit=100,
            )
            client.close()
        except Exception as error:
            if client:
                try:
                    client.networks.get(network_name).remove()
                except Exception:
                    pass
            raise ValueError(f"could not start isolated {service} service: {error}")

        service_info = {
            "service_id": service_id,
            "service": service,
            "network": network_name,
            "project_path": project_path,
            "project_host_path": project_host_path,
            "container_id": container.id,
            "expires": time.time() + ttl_minutes * 60,
        }
        with self._lock:
            self._services[service_id] = service_info
        cleanup = threading.Timer(ttl_minutes * 60, self._stop_service, args=(service_id,))
        cleanup.daemon = True
        cleanup.start()
        uri = (
            f"postgresql://{username}:{password}@{network_name}:5432/{database}"
            if service == "postgres"
            else f"mongodb://{username}:{password}@{network_name}:27017/admin?authSource=admin"
        )
        return {
            "service_id": service_id,
            "service": service,
            "status": "running",
            "connection_uri": uri,
            "network_isolated": True,
            "data_lifetime": "temporary; removed when the service stops or expires",
            "expires_in_minutes": ttl_minutes,
        }

    def _stop_service(self, service_id):
        with self._lock:
            service = self._services.pop(service_id, None)
        if not service:
            return {"service_id": service_id, "status": "unknown"}
        try:
            import docker
            client = docker.from_env()
            try:
                client.containers.get(service["container_id"]).remove(force=True, v=True)
            except Exception:
                pass
            try:
                client.networks.get(service["network"]).remove()
            except Exception:
                pass
            client.close()
        except Exception:
            logger.exception("failed to clean up temporary database service %s", service_id)
        return {"service_id": service_id, "status": "stopped"}

    def sandbox_stop_service(self, params):
        return self._stop_service(params.get("service_id"))

    @staticmethod
    def _command_needs_network(cmd_args):
        command = cmd_args[0]
        args = [str(arg).lower() for arg in cmd_args[1:]]
        if command in {"pip", "pip3"}:
            return any(arg in {"install", "download", "wheel"} for arg in args)
        if command in {"npm", "pnpm"}:
            return any(arg in {"install", "i", "add", "update", "upgrade", "exec", "dlx"} for arg in args)
        if command == "npx":
            return True
        if command == "mix":
            return "deps.get" in args or "archive.install" in args
        return False

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

        runtime = params.get("runtime", "auto")
        if runtime not in {"auto", "node", "python", "elixir", "base"}:
            raise ValueError("runtime must be auto, node, python, elixir, or base")
        project_path, project_host_path = self._project_mount(params.get("project_path"))
        inferred_runtime = (
            "node" if first in {"node", "npm", "npx", "pnpm"}
            else "python" if first in {"python3", "pip"}
            else "elixir" if first in {"elixir", "mix"}
            else "base"
        )
        if runtime != "auto" and runtime != inferred_runtime:
            raise ValueError(f"command '{first}' is not available in the {runtime} runtime")
        runtime = inferred_runtime if runtime == "auto" else runtime

        service_network = None
        service_id = params.get("service_id")
        if service_id:
            with self._lock:
                service_info = self._services.get(service_id)
            if not service_info or service_info["expires"] <= time.time():
                raise ValueError("service_id is unknown or expired")
            if service_info["project_path"] != project_path:
                raise ValueError("database service and command must use the same project_path")
            service_network = service_info["network"]

        needs_network = self._command_needs_network(cmd_args) or params.get("network_access") is True
        network_allowed = False
        permission_id = params.get("network_permission_id")
        if needs_network or permission_id:
            self._consume_permission(permission_id, "network")
            network_allowed = True
        if service_network and network_allowed:
            raise ValueError("run dependency downloads separately from database-connected commands")
        runtime_images = {
            "base": "alpine:3.19",
            "elixir": "elixir:1.18-slim",
            "node": "mcp-sandbox-node:22",
            "python": "python:3.12-slim",
        }
        image = runtime_images[runtime]
        try:
            import docker
            image_client = docker.from_env()
            try:
                image_client.images.get(image)
            except docker.errors.ImageNotFound:
                if runtime == "node":
                    raise ValueError("Node sandbox image is missing; rebuild it with mcp-server/run.sh")
                self._consume_permission(params.get("runtime_permission_id"), f"runtime:{runtime}")
            finally:
                image_client.close()
        except ValueError:
            raise
        except Exception as error:
            raise ValueError(f"cannot check sandbox runtime image: {error}")
        runtime_host_path = self._runtime_storage(project_path)

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
            "runtime": runtime,
            "image": image,
            "project_path": project_path,
            "project_host_path": project_host_path,
            "runtime_host_path": runtime_host_path,
            "network_allowed": network_allowed,
            "service_network": service_network,
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
        runtime_images = {
            "base": "alpine:3.19",
            "elixir": "elixir:1.18-slim",
            "node": "mcp-sandbox-node:22",
            "python": "python:3.12-slim",
        }
        image = runtime_images[job["runtime"]]

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
                environment={
                    "COREPACK_HOME": "/opt/runtime/corepack",
                    "HOME": "/opt/runtime/home",
                    "HEX_HOME": "/opt/runtime/hex",
                    "MIX_HOME": "/opt/runtime/mix",
                    "NPM_CONFIG_PREFIX": "/opt/runtime/npm",
                    "PNPM_HOME": "/opt/runtime/pnpm",
                    "PYTHONUSERBASE": "/opt/runtime/python",
                    "TMPDIR": "/opt/runtime/tmp",
                },
                volumes={
                    job["project_host_path"]: {"bind": "/workspace", "mode": "rw"},
                    job["runtime_host_path"]: {"bind": "/opt/runtime", "mode": "rw"},
                },
                network=job["service_network"],
                network_disabled=not job["network_allowed"] and not job["service_network"],
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
        {"function": {"name": "sandbox_capabilities", "description": "Discover available sandbox runtimes, temporary services, isolation, and user approval rules.", "parameters": {"type": "object", "properties": {}, "required": []}}},
        {"function": {"name": "sandbox_request_permission", "description": "Request short-lived user approval for internet access, a runtime image, a temporary database, or a workspace quota increase. Present the returned user_prompt and wait for an explicit yes before approving.", "parameters": {"type": "object", "properties": {"scope": {"type": "string", "enum": ["network", "service:postgres", "service:mongodb", "runtime:base", "runtime:elixir", "runtime:python", "storage:increase"]}, "reason": {"type": "string"}, "limit_gb": {"type": "number"}}, "required": ["scope", "reason"]}}},
        {"function": {"name": "sandbox_approve_permission", "description": "Record the user's explicit decision for a pending sandbox permission request. Call only after the user answers; use the approval phrase shown to them.", "parameters": {"type": "object", "properties": {"request_id": {"type": "string"}, "approved": {"type": "boolean"}, "user_confirmation": {"type": "string"}}, "required": ["request_id", "approved", "user_confirmation"]}}},
        {"function": {"name": "sandbox_set_storage_limit", "description": "Apply the exact workspace storage quota increase from a one-use, user-approved storage:increase permission. The increase lasts until the server restarts.", "parameters": {"type": "object", "properties": {"permission_id": {"type": "string"}}, "required": ["permission_id"]}}},
        {"function": {"name": "sandbox_start_service", "description": "Start a temporary, project-isolated PostgreSQL or MongoDB service. Requires a one-use user-approved permission_id from sandbox_approve_permission. Data is ephemeral and the service expires automatically.", "parameters": {"type": "object", "properties": {"service": {"type": "string", "enum": ["postgres", "mongodb"]}, "project_path": {"type": "string"}, "permission_id": {"type": "string"}, "ttl_minutes": {"type": "integer"}}, "required": ["service", "project_path", "permission_id"]}}},
        {"function": {"name": "sandbox_stop_service", "description": "Stop and remove a temporary database service and its isolated network.", "parameters": {"type": "object", "properties": {"service_id": {"type": "string"}}, "required": ["service_id"]}}},
        {"function": {"name": "sandbox_reset_runtime", "description": "Remove persisted per-project package-manager caches and user-installed packages. Requires confirm=true.", "parameters": {"type": "object", "properties": {"project_path": {"type": "string"}, "confirm": {"type": "boolean"}}, "required": ["project_path", "confirm"]}}},
        {"function": {"name": "list_allowed_directories", "description": "Returns exactly one entry: /workspace.", "parameters": {"type": "object", "properties": {}, "required": []}}},
        {"function": {"name": "list_directory", "description": "List files/folders inside /workspace only.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}}, "required": ["path"]}}},
        {"function": {"name": "read_text_file", "description": "Read a text file inside /workspace only. Max 1 MB per call.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}}, "required": ["path"]}}},
        {"function": {"name": "write_file", "description": "Create or overwrite a file inside /workspace only, after quota check.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "mode": {"type": "string"}, "confirm": {"type": "boolean"}}, "required": ["path", "content"]}}},
        {"function": {"name": "edit_file", "description": "Replace an exact text block inside a file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}}, "required": ["path", "old_string", "new_string"]}}},
        {"function": {"name": "create_directory", "description": "Create a folder inside /workspace only.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
        {"function": {"name": "move_file", "description": "Move/rename inside /workspace only.", "parameters": {"type": "object", "properties": {"source": {"type": "string"}, "destination": {"type": "string"}}, "required": ["source", "destination"]}}},
        {"function": {"name": "delete_file", "description": "Delete a single file inside /workspace. Requires confirm=true.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "confirm": {"type": "boolean"}}, "required": ["path", "confirm"]}}},
        {"function": {"name": "get_storage_status", "description": "Return workspace usage stats.", "parameters": {"type": "object", "properties": {}, "required": []}}},
        {"function": {"name": "exec_sandboxed", "description": "Run one command in an ephemeral Node, Python, Elixir, or base runtime. Project user packages persist in isolated storage; use project_path to mount only that project. Internet installs require a prior user-approved network_permission_id. Use service_id to join that project's isolated database network.", "parameters": {"type": "object", "properties": {"command": {"type": "array", "items": {"type": "string"}}, "job_id": {"type": "string"}, "timeout_minutes": {"type": "integer"}, "runtime": {"type": "string", "enum": ["auto", "node", "python", "elixir", "base"]}, "project_path": {"type": "string"}, "network_permission_id": {"type": "string"}, "network_access": {"type": "boolean"}, "service_id": {"type": "string"}}, "required": ["command"]}}},
        {"function": {"name": "exec_status", "description": "Check status of a background exec job.", "parameters": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]}}},
        {"function": {"name": "read_exec_log", "description": "Read paginated log of an exec job.", "parameters": {"type": "object", "properties": {"job_id": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["job_id"]}}},
    ]


def uuid_local():
    import uuid
    return uuid.uuid4().hex
