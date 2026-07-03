#!/bin/bash
# Pull and run the published image from the GitHub Container Registry (GHCR).
# The GHCR package is public, so NO docker login / access token is required.
set -e

IMAGE="ghcr.io/bnelair/mef3io-server:latest"

docker pull "$IMAGE"
docker run -d -p 50051:50051 --name mef3io-server "$IMAGE"
