# Secure MCP Sandbox (Single-Folder Version)

A hardened, client-independent MCP (Model Context Protocol) server that gives connected agents access to one configured workspace, with isolated command and temporary service containers.

## Architecture

```
Any MCP-compatible client
    -> HTTPS (ngrok)
        -> MCP Server (this repo)
            -> Filesystem: /workspace (only folder exposed)
            -> Exec: ephemeral Docker runtime (2GB RAM, 2 CPUs)
            -> Services: temporary project-isolated PostgreSQL or MongoDB
            -> Storage guard: configurable workspace quota (5 GB default)
```

### Layers of Defense

| Layer | What It Stops | Implementation |
|-------|---------------|----------------|
| **1. Path Validation** | `../`, absolute paths, symlink escapes | `PathGuard` resolves all paths to real absolute paths and ensures they stay inside `/workspace` |
| **2. Symlink Resolution** | Symlinks pointing outside workspace | `PathGuard` resolves symlinks and rejects any target outside the allowed directory |
| **3. Docker Sandbox** | Host filesystem access, host network, host PID namespace | Runtime containers use `--read-only`, dropped capabilities, `--memory 2g`, and `--pids-limit 100`; only the selected project and its runtime cache are mounted |
| **4. Workspace Quota** | MCP file operations exceeding configured usage | `.env` sets the 5 GB default; increases require user approval |
| **5. Allowlist + No Privileges** | Command injection, privilege escalation | Only allowed commands (`ls`, `cat`, `mkdir`, `rmdir`, `echo`, `python3`, `pip`, `node`, `npm`, `npx`, `grep`, `head`, `tail`, `wc`, `find`) are permitted; no `docker`, `sudo`, `bash`, `curl`, etc. |

## Quick Start

### 1. Prepare the Host

```bash
# Create the project folder (ONLY this folder will ever be exposed)
mkdir -p /home/user/projects/my-app

# MCP file operations use the configured quota. Direct container writes share
# the host filesystem quota unless the host provides a separate disk limit.

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

### 4. Connect an MCP client

1. Create a custom connector pointing to the ngrok URL
2. Authenticate with the Bearer token (found in `.env`)
3. Run `list-tools` to see the allowed commands
4. Use only the allowed tools (e.g., `ls`, `cat`, `mkdir`)

## Files

| File | Purpose |
|------|----------|
| `mcp-server/server/main.py` | FastAPI MCP server with HTTP endpoint, auth, and tool registry |
| `mcp-server/server/quota.py` | Configurable storage quota enforcement |
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

- **Workspace boundary**: The configured workspace is the only host folder mounted into command containers. MCP file operations enforce its configured quota.
- **Network access**: Commands have no network by default. Package downloads require one-use user approval.
- **Permission requests**: Network access, temporary database services, and quota increases require a request shown to the user and explicit approval.
- **Temporary services**: PostgreSQL and MongoDB run on private per-service networks with in-memory data and automatic expiry.
- **Runtime packages**: Per-project user packages persist in ignored `.sandbox/runtimes` storage and can be removed with `sandbox_reset_runtime`.
- **Approval gates**: Internet access and temporary database creation require one-use user approval. Clients should show each request and wait for an explicit response.
- **Temporary services**: PostgreSQL and MongoDB run on private per-service Docker networks, with temporary storage and automatic expiry.
- **Runtime packages**: Per-project Python, Node, and Elixir user packages persist in the ignored `.sandbox/runtimes` directory and can be removed with `sandbox_reset_runtime`.
- **No privileged execution**: Container runs as UID 1000, with `--cap-drop ALL` and `--read-only` root filesystem.
- **No shell access**: All commands run via Docker sandbox; no `bash -c` or `sh` allowed.
- **Quota changes**: `MCP_WORKSPACE_LIMIT_GB` sets the startup quota (5 GB default). An approved increase applies until restart; set the environment variable to retain it.
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

- The configured workspace is the only host folder exposed to the agent.
- Back up the workspace folder from the host regularly.
- If the container is full, stop it and delete/archive files from the host.
- The Docker volume can be recreated from the original image.

## Risks & Mitigations

- **Allowlisted script runner**: Even with sandboxing, a script inside the 5 GB workspace can still delete files inside it. This is by design — the workspace is the sandbox boundary.
- **Long-running jobs**: Job timeout is 30 minutes; the container is killed after that.
- **ngrok URL changes**: The ngrok URL changes on restart. Update the client accordingly.
- **Physical quota**: The configurable limit protects MCP file operations. Docker bind mounts do not impose a physical filesystem quota on direct command writes.

## License

MIT
