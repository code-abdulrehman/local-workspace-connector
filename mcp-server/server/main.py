"""
MCP Server with hard 5 GB workspace limit and strict sandbox isolation.
Streamable HTTP transport with Bearer auth, path guards, quota checks, and
sandboxed command execution via ephemeral Docker containers.
"""

import os
import sys
import json
import asyncio
import hashlib
import yaml
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from .quota import QuotaManager, MAX_WORKSPACE_BYTES, StorageLimitExceeded
from .path_guard import PathGuard, PathOutsideAllowed, PathTooLong
from .tools import Tools, build_tool_definitions, ToolError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CONFIG_PATH = os.environ.get("SANDBOX_CONFIG_PATH", "/opt/sandbox-config/sandbox.yaml")
CONFIG = {
    "workspace": "/workspace",
    "allowlist": [
        "ls", "cat", "mkdir", "rmdir", "echo",
        "python3", "pip",
        "node", "npm", "npx",
        "grep", "head", "tail", "wc", "find",
    ],
    "max_single_file_mb": 500,
    "max_file_count": 20000,
    "job_timeout_minutes": 30,
    "max_read_bytes": 1024 * 1024,  # 1 MB,
}

try:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            loaded = yaml.safe_load(f) or {}
            CONFIG.update(loaded)
except Exception:
    pass

WORKSPACE_DIR = CONFIG["workspace"]
LOG_DIR = os.path.join(WORKSPACE_DIR, ".sandbox", "logs")
JOB_DIR = os.path.join(WORKSPACE_DIR, ".sandbox", "jobs")
MAX_SINGLE_FILE_MB = CONFIG["max_single_file_mb"]
MAX_FILE_COUNT = CONFIG["max_file_count"]
JOB_TIMEOUT_SECONDS = CONFIG["job_timeout_minutes"] * 60
ALLOWED_COMMANDS = list(CONFIG["allowlist"])
MAX_READ_BYTES = CONFIG["max_read_bytes"]

TOKEN = os.environ.get("MCP_BEARER_TOKEN")
PORT = int(os.environ.get("MCP_PORT", 8000))

def ensure_dirs():
    for d in [LOG_DIR, JOB_DIR, os.path.join(LOG_DIR, ".."), os.path.join(JOB_DIR, "..")]:
        os.makedirs(d, exist_ok=True)

def config_hash():
    h = hashlib.sha256()
    h.update(json.dumps(CONFIG).encode())
    return h.hexdigest()

def build_tool_registry():
    registry = {}
    t = Tools(
        workspace=WORKSPACE_DIR,
        job_timeout_seconds=JOB_TIMEOUT_SECONDS,
        allowed_commands=ALLOWED_COMMANDS,
    )
    tool_defs = build_tool_definitions()
    for tool_def in tool_defs:
        func_name = tool_def["function"]["name"]
        registry[func_name] = getattr(t, func_name, None)
    return registry

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
quota_mgr = QuotaManager()
path_guard = PathGuard(allowed=WORKSPACE_DIR)
tool_registry = build_tool_registry()

# ---------------------------------------------------------------------------
# Application setup
# ---------------------------------------------------------------------------
app = FastAPI(title="Secure MCP Sandbox", version="1.0.0")

@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    if not os.environ.get("MCP_BEARER_TOKEN"):
        raise RuntimeError("MCP_BEARER_TOKEN must be set in environment")
    yield

app = FastAPI(title="Secure MCP Sandbox", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path == "/mcp":
        authorization = request.headers.get("Authorization", "")
        token = os.environ.get("MCP_BEARER_TOKEN", "")
        if not authorization.startswith("Bearer " + token) or authorization != f"Bearer {token}":
            return JSONResponse(
                status_code=401,
                content={"error": "unauthorized", "message": "Valid Bearer token required"},
            )
    response = await call_next(request)
    return response

# ---------------------------------------------------------------------------
# MCP Streamable HTTP endpoint
# ---------------------------------------------------------------------------
@app.post("/mcp")
async def mcp_post(request: Request):
    body = await request.body()
    try:
        message = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": "invalid_json"})
    
    if isinstance(message, dict) and "jsonrpc" in str(message) and "method" in str(message):
        result = _handle_mcp_message(message, request.headers.get("mcp-session-id", ""))
        return JSONResponse(content=result)
    return JSONResponse(content={"error": "not_an_mcp_message"})

# ---------------------------------------------------------------------------
# Message dispatch
# ---------------------------------------------------------------------------
def _handle_mcp_message(msg, session_id=""):
    if not isinstance(msg, dict):
        return {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Invalid request"}}
    
    # Initialize / notifications / listings
    if msg.get("jsonrpc") == "2.0" and "method" in msg:
        method = msg["method"]
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "secure-mcp-sandbox", "version": "1.0.0"},
                    "sessionId": session_id,
                },
            }
        if method == "notifications/initialized":
            return {"jsonrpc": "2.0", "result": None, "id": msg.get("id")}
        if method == "tools/list":
            defs = build_tool_definitions()
            return {"jsonrpc": "2.0", "result": {"tools": defs}, "id": msg.get("id")}
        # Handle tool calls (can be either direct tool name or tools/call format)
        if "params" in msg and isinstance(msg.get("params"), dict):
            params = msg.get("params", {})
            # Check for standard tools/call format: {"name": "ls", "arguments": {...}}
            if "name" in params and "arguments" in params:
                tool_name = params["name"]
                args = params["arguments"]
                if tool_name not in tool_registry:
                    return {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32602, "message": f"Tool '{tool_name}' not found"}}
                try:
                    tool = tool_registry[tool_name]
                    result = tool(args)
                    return {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": json.dumps(result)}], "isError": False}, "id": msg.get("id")}
                except ToolError as e:
                    return {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32603, "message": str(e)}}
                except Exception as e:
                    return {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32603, "message": str(e)}}
        return {"jsonrpc": "2.0", "result": None, "id": msg.get("id")}
    
    if "id" not in msg:
        return {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Missing id"}}
    
    tool_name = msg.get("params", {}).get("tool")
    params = msg.get("params", {})
    
    if tool_name is None:
        return {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32602, "message": "Missing tool name"}}
    
    if tool_name not in tool_registry:
        return {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32602, "message": f"Tool '{tool_name}' not found"}}
    
    tool = tool_registry[tool_name]
    try:
        result = tool(params)
        return {"jsonrpc": "2.0", "result": result}
    except ToolError as e:
        return {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(e)}}
    except Exception as e:
        return {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(e)}}

# ---------------------------------------------------------------------------
# Health check (no auth, no tool calls — purely informational)
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    snap = quota_mgr.snapshot()
    return {
        "status": "ok",
        "limit_bytes": MAX_WORKSPACE_BYTES,
        "used_bytes": snap.used_bytes,
        "free_bytes": snap.free_bytes,
        "files": snap.file_count,
        "allowed_directory": "/workspace",
        "config_hash": config_hash(),
    }

if __name__ == "__main__":
    print(f"MCP Sandbox server starting on port {os.environ.get('MCP_PORT', 8000)}, workspace={WORKSPACE_DIR}, config_hash={config_hash()}", flush=True)
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get('MCP_PORT', 8000)))
