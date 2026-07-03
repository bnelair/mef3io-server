#!/bin/bash
set -e

# Optional: print environment info for debugging
echo "Starting mef3io-server..."
echo "Python version: $(python --version)"
echo "Working directory: $(pwd)"

# Launch the gRPC server
exec python -u -m mef3io_server.server

