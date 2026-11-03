#!/bin/bash
# mcp-server/run.sh - One-click start for MCP server with ngrok exposure

set -e

echo "=== MCP SANDBOX SERVER SETUP ==="

# Check if Docker is running
if ! docker info > /dev/null 2>&1; then
    echo "ERROR: Docker is not running."
    echo "Start Docker first (sudo systemctl start docker), then run ./run.sh again."
    exit 1
fi

# Create workspace directories if they don't exist
mkdir -p workspace/.sandbox/logs
mkdir -p workspace/.sandbox/jobs

# Generate .env file if it doesn't exist
if [ ! -f .env ]; then
    echo "Generating .env file with placeholders..."
    cp .env.example .env
    echo "Please edit .env and set your MCP_BEARER_TOKEN"
    exit 1
fi

# Start Docker compose
echo "Starting MCP server..."
docker compose up -d

# Wait for server to start
echo "Waiting for server to start (5 seconds)..."
sleep 5

# Get server URL
SERVER_URL="http://127.0.0.1:8000"

# Check if server is running
if curl -s "$SERVER_URL/health" > /dev/null; then
    echo "✓ MCP server is running"
else
    echo "✗ Failed to start MCP server. Check logs with 'docker compose logs -f'"
    exit 1
fi

# Start ngrok (use downloaded version if old version fails)
NGROK_BIN="/home/abdul/Downloads/ngrok"
if [ -x "$NGROK_BIN" ]; then
    echo "Starting ngrok (new version)..."
    $NGROK_BIN http 8000 > /dev/null 2>&1 &
else
    echo "Starting ngrok..."
    ngrok http 8000 > /dev/null 2>&1 &
fi
NGROK_PID=$!

# Wait for ngrok to start
echo "Waiting for ngrok to initialize (10 seconds)..."
sleep 10

# Get ngrok URL
NGROK_URL=$(curl -s http://127.0.0.1:4040/api/tunnels 2>/dev/null | grep -o '"public_url":"https://[^"]*' | head -1 | sed 's/"public_url":"//')

if [ -z "$NGROK_URL" ]; then
    echo "✗ Failed to get ngrok URL. Check ngrok logs."
    kill $NGROK_PID
    exit 1
fi

echo "========================================"
echo "✓ MCP SERVER IS RUNNING"
echo "✓ ngrok URL: $NGROK_URL"
echo "========================================"
echo "Connect your AI agent (Muse) to:"
echo "  1. Use the ngrok URL above as the connector endpoint"
echo "  2. Set Authorization: Bearer [MCP_BEARER_TOKEN] in your .env"
echo "  3. Connect and use only the allowed tools: ls, cat, mkdir, rmdir, echo, python3, pip, node, npm, npx, grep, head, tail, wc, find"
echo ""
echo "To stop: run ./stop.sh"
echo "========================================"

# Keep running so ngrok stays alive
trap "kill $NGROK_PID" EXIT
wait