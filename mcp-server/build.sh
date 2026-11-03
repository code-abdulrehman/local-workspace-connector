#!/bin/bash
# mcp-server/build.sh - Build the Docker image directly

set -e

echo "=== BUILDING MCP SERVER DOCKER IMAGE ==="

if ! docker info > /dev/null 2>&1; then
    echo "ERROR: Docker is not running."
    echo "Start Docker first: sudo systemctl start docker"
    exit 1
fi

# Create workspace directories if they don't exist
mkdir -p workspace/.sandbox/logs
mkdir -p workspace/.sandbox/jobs

# Generate .env file if it doesn't exist
if [ ! -f .env ]; then
    echo "Generating .env file with placeholders..."
    cp .env.example .env
fi

echo "Building Docker image..."
docker build -t mcp-sandbox:latest .

echo "========================================"
echo "✓ Docker image built: mcp-sandbox:latest"
echo "========================================"
echo "Now run: ./run.sh"
echo "========================================"