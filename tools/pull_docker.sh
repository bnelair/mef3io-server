#!/bin/bash
# Download (pull) the appropriate brainmaze-mef3-server image from the GitHub
# Container Registry (GHCR). The GHCR package is public, so NO login is required.
#
# Usage:
#   ./tools/pull_docker.sh [TAG]
#   TAG defaults to "latest" (e.g. "1.0.1", "1.0", "latest").
set -e

IMAGE="ghcr.io/bnelair/brainmaze-mef3-server"
TAG="${1:-latest}"

echo "Pulling ${IMAGE}:${TAG} ..."
docker pull "${IMAGE}:${TAG}"
echo "Done. Image available locally as ${IMAGE}:${TAG}"
