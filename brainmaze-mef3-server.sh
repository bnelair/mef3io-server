#!/bin/bash
set -e

# Optional: print environment info for debugging
echo "Starting brainmaze-mef3-server..."
echo "Python version: $(python --version)"
echo "Working directory: $(pwd)"

# Launch the gRPC server
exec python -u -m brainmaze_mef3_server.server

