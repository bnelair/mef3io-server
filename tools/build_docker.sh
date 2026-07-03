#!/bin/bash
# Build the Docker image locally and optionally push it to the GitHub Container
# Registry (GHCR). In normal operation, publishing is handled automatically by
# .github/workflows/docker-publish.yml on each release/tag; this script is for
# local builds and manual pushes.
#
# Usage:
#   ./tools/build_docker.sh [TAG] [--push]
#   TAG defaults to "local".
#
# Pushing requires a one-time login with a Personal Access Token that has the
# write:packages scope:
#   echo "$GHCR_PAT" | docker login ghcr.io -u <github-username> --password-stdin
set -e

IMAGE="ghcr.io/bnelair/mef3io-server"
TAG="${1:-local}"

docker build --pull -f Dockerfile -t "$IMAGE:$TAG" .

if [ "$2" = "--push" ]; then
    docker push "$IMAGE:$TAG"
fi
