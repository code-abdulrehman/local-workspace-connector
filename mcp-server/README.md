# Secure MCP Sandbox (Single-Folder Version)

A hardened MCP (Model Context Protocol) server that gives an AI agent (e.g., Muse) access to **exactly one** folder on the local host, with full sandbox isolation and a **5 GB hard storage limit**.

## Architecture

```
AI Client (Muse)
    -> HTTPS (ngrok)
        -> MCP Server (this repo)
            -> Filesystem: /workspace (only folder exposed)
            -> Exec: ephemeral Docker sandbox (2GB RAM, 2 CPUs, 5GB disk)
            -> Storage guard: physical 5 GB limit + code-level quota checks
```

### Layers of Defense

| Layer | What It Stops | Implementation |
|-------|---------------|----------------|
| **1. Path Validation** | `../`, absolute paths, symlink escapes | `PathGuard` resolves all paths to real absolute paths and ensures they stay inside `/workspace` |
| **2. Symlink Resolution** | Symlinks pointing outside workspace | `PathGuard` resolves symlinks and rejects any target outside the allowed directory |
| **3. Docker Sandbox** | Host filesystem access, host network, host PID namespace | Each exec runs in an ephemeral Docker container with `--read-only`, `--tmpfs /tmp`, `--memory 2g`, `--pids-limit 100`, `--network none` |
| **4. Hard 5 GB Limit** | Disk fill attacks, quota bypass | Physical 5 GB disk image + code-level pre-write checks |
| **5. Allowlist + No Privileges** | Command injection, privilege escalation | Only allowed commands (`ls`, `cat`, `mkdir`, `rmdir`, `echo`, `python3`, `pip`, `node`, `npm`, `npx`, `grep`, `head`, `tail`, `wc`, `find`) are permitted; no `docker`, `sudo`, `bash`, `curl`, etc. |

## Quick Start

### 1. Prepare the Host

```bash
# Create the project folder (ONLY this folder will ever be exposed)
mkdir -p /home/user/projects/my-app

# Create the 5 GB disk image (Linux)
# This is the ONLY way to get the hard 5 GB limit.
# On Linux with Docker Desktop / macOS, use a named volume with size=5G.
# On Windows, create a 5 GB loopback image.

# Copy the example config (do not commit .env with real token)
cp mcp-server/.env.example mcp-server/.env
# Generate a strong token:
# openssl rand -hex 32
# Paste the hex output into MCP_BEARER_TOKEN in .env
```

### 2. Build and Start the Server

```bash
# Build the Docker image
cd mcp-server
docker compose build

# Start the server (port 8000 by default)
cd mcp-server
./run.sh
```

### 3. Expose via ngrok

The `run.sh` script automatically starts ngrok and returns the public HTTPS URL.

### 4. Connect in Muse

1. Create a custom connector in Muse pointing to the ngrok URL
2. Authenticate with the Bearer token (found in `.env`)
3. Run `list-tools` to see the allowed commands
4. Use only the allowed tools (e.g., `ls`, `cat`, `mkdir`)

## Files

| File | Purpose |
|------|----------|
| `mcp-server/server/main.py` | FastAPI MCP server with HTTP endpoint, auth, and tool registry |
| `mcp-server/server/quota.py` | Storage quota enforcement (5 GB hard limit, pre-write checks) |
| `mcp-server/server/path_guard.py` | Path validation (Layer 1 & 2) |
| `mcp-server/server/exec_sandbox.py` | Ephemeral Docker sandbox execution (Layer 3) |
| `mcp-server/server/tools.py` | MCP tool implementations (list, read, write, exec, etc.) |
| `mcp-server/Dockerfile` | Builds the sandbox container (non-root, read-only, limited resources) |
| `mcp-server/docker-compose.yml` | Orchestrates the MCP server with volume mounts |
| `mcp-server/config/sandbox.yaml` | Human-configurable settings (workspace, allowlist, limits) |
| `mcp-server/.env.example` | Environment template (replace placeholders) |
| `mcp-server/.gitignore` | Ignores secrets, workspace images, logs |
| `mcp-server/run.sh` | One-click start/stop with ngrok exposure |
| `mcp-server/tests/` | Automated tests for all security layers |

## Security Guarantees

- **No host disk access**: The workspace is mounted inside a 5 GB disk image. Writing beyond 5 GB fails with `ENOSPC` inside the container only.
- **No host network**: The container has `--network none` by default; no outbound connections.
- **No privileged execution**: Container runs as UID 1000, with `--cap-drop ALL` and `--read-only` root filesystem.
- **No shell access**: All commands run via Docker sandbox; no `bash -c` or `sh` allowed.
- **No limit change**: The `MAX_WORKSPACE_BYTES` constant is hardcoded; no MCP tool can modify it.
- **Path isolation**: `PathGuard` ensures every operation stays inside `/workspace`.

## Daily Use

```bash
# Start server (if not already running)
./run.sh

# Check storage status
curl http://127.0.0.1:8000/health

# List allowed directories
curl -H "Authorization: Bearer <TOKEN>" http://127.0.0.1:8000/mcp/list_allowed_directories

# Run a command (e.g., list workspace contents)
curl -H "Authorization: Bearer <TOKEN>" http://127.0.0.1:8000/mcp/list_directory

# Execute a sandboxed command (e.g., create a file)
curl -H "Authorization: Bearer <TOKEN>" http://127.0.0.1:8000/mcp/exec_sandboxed?command=["mkdir","test.txt"]
```

## Backup & Recovery

- The 5 GB workspace is the **only** place the agent can write.
- Back up the workspace folder from the host regularly.
- If the container is full, stop it and delete/archive files from the host.
- The Docker volume can be recreated from the original image.

## Risks & Mitigations

- **Allowlisted script runner**: Even with sandboxing, a script inside the 5 GB workspace can still delete files inside it. This is by design — the workspace is the sandbox boundary.
- **Long-running jobs**: Job timeout is 30 minutes; the container is killed after that.
- **ngrok URL changes**: The ngrok URL changes on restart. Update the client accordingly.
- **Host disk not protected**: The 5 GB limit is enforced at the container level (disk image) and at the code level (quota checks). If the container is full, writes fail gracefully with `STORAGE_LIMIT_EXCEEDED`.

## License

MIT