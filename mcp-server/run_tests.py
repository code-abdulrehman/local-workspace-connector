#!/usr/bin/env python3
"""MCP Server test script."""
import json, subprocess, sys, os

TOKEN = os.environ.get("MCP_BEARER_TOKEN", "7876e5525d36b93664e29683a056d02544f422d2fe65d12254047a68a6bf78cf")
URL = "http://127.0.0.1:8000/mcp"

def mcp_call(method, params=None, id=1):
    body = {"jsonrpc": "2.0", "method": method, "id": str(id)}
    if params:
        body["params"] = params
    cmd = ["curl", "-s", "-X", "POST", "-H", "Content-Type: application/json",
           "-H", f"Authorization: Bearer {TOKEN}", URL, "-d", json.dumps(body)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return json.loads(result.stdout)

def test(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"{status}: {name} {detail}")
    return condition

print("=== MCP Server Tests ===")

# Test 1: Initialize
r = mcp_call("initialize", id="1")
ok = test("Initialize returns protocolVersion string",
          r.get("result", {}).get("protocolVersion") == "2025-11-25",
          f"got: {r.get('result', {}).get('protocolVersion')}")

# Test 2: Tools/List
r = mcp_call("tools/list", id="2")
tools = [t["function"]["name"] for t in r.get("result", {}).get("tools", [])]
required = ["list_allowed_directories", "list_directory", "read_text_file",
            "write_file", "create_directory", "move_file", "delete_file", "get_storage_status"]
ok = test("Tools/List returns all required tools",
          all(t in tools for t in required),
          f"missing: {[t for t in required if t not in tools]}")

# Test 3: List directory
r = mcp_call("tools/call", {"name": "list_directory", "arguments": {"path": "/workspace"}}, id="3")
text = r.get("result", {}).get("content", [{}])[0].get("text", "{}")
data = json.loads(text)
dirs = [e["name"] for e in data.get("entries", [])]
ok = test("List directory shows workspace contents",
          ".sandbox" in dirs and "test-folder" in dirs,
          f"dirs: {dirs}")

# Test 4: Health
import urllib.request
resp = urllib.request.urlopen("http://127.0.0.1:8000/health")
health = json.loads(resp.read())
ok = test("Health shows 5 GB limit and allowed directory",
          health["limit_bytes"] == 5368709120 and health["allowed_directory"] == "/workspace",
          f"limit={health['limit_bytes']}, files={health['files']}")

print("\n=== All tests completed ===")
sys.exit(0 if all([ok]) else 1)