#!/bin/bash
# mcp-server/stop.sh - Stop the MCP server, ngrok, and containers

set -e

echo "=== STOPPING MCP SERVER ==="

# Kill ngrok if running
NGROK_PIDS=$(pgrep -f "ngrok http 8000" || true)
if [ -n "$NGROK_PIDS" ]; then
    echo "Killing ngrok processes: $NGROK_PIDS"
    kill $NGROK_PIDS 2>/dev/null || true
fi

# Stop Docker containers
echo "Stopping Docker containers..."
docker compose down 2>/dev/null || true

# Also stop any mcp-sandbox container by name
docker stop mcp-sandbox 2>/dev/null || true
docker rm mcp-sandbox 2>/dev/null || true

echo "========================================"
echo "✓ MCP server stopped"
echo "========================================"