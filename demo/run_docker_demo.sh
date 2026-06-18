#!/bin/bash
# Demo: build and run the MEF3 gRPC server in Docker.
#
# IMPORTANT: the server reads host files through a bind mount at /host_root.
# When running in Docker it rewrites any absolute path you request to
# /host_root/<that path>, so the host filesystem MUST be mounted there.
# We mount it read-only (-v /:/host_root:ro) because the server only reads data.
# Then pass the client the normal absolute host path (e.g. /data/x.mefd); the
# server resolves it to /host_root/data/x.mefd inside the container.
set -e

docker build -t brainmaze-mef3-server:demo .

docker run --name brainmaze-mef3-demo \
  -p 127.0.0.1:50051:50051 \
  -v /:/host_root:ro \
  brainmaze-mef3-server:demo

# add -d to run in the background
