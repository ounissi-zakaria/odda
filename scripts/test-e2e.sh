#!/usr/bin/env bash
# Run the e2e test suite inside a Docker container.
# Always rebuilds the image (Docker layer cache makes this fast when nothing changed).

set -euo pipefail

cd "$(dirname "$0")/.."

IMAGE_TAG="odda-test:latest"

docker build -f tests/e2e/Dockerfile -t "$IMAGE_TAG" .

exec docker run --rm "$IMAGE_TAG" "$@"