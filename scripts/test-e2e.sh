#!/usr/bin/env bash
# Run the e2e test suite inside a Docker container.
# Always rebuilds the image (Docker layer cache makes this fast when nothing changed).
#
# Usage: ./scripts/test-e2e.sh [-j N]
#   -j N   Run N test documents in parallel (default: 1, sequential).
#          Use -j 0 for unlimited parallelism (all docs at once).
#          Each `scrut test <file>` runs in its own $PWD with its own
#          odda server and auto-picked fixture port.

set -euo pipefail

cd "$(dirname "$0")/.."

IMAGE_TAG="odda-test:latest"

docker build -f tests/e2e/Dockerfile -t "$IMAGE_TAG" .

JOBS=1
while getopts "j:" opt; do
  case "$opt" in
    j) JOBS="$OPTARG" ;;
    \?) exit 1 ;;
  esac
done
shift $((OPTIND-1))

if ! [[ "$JOBS" =~ ^[0-9]+$ ]]; then
  echo "error: -j requires a non-negative integer, got '$JOBS'" >&2
  exit 1
fi

docker run --rm "$IMAGE_TAG" bash -c "
  ls tests/e2e/scrut/[0-9]*.md | xargs -n1 -P $JOBS scrut test
"