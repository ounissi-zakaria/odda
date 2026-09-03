#!/usr/bin/env bash
# Run the e2e test suite inside a Docker container.
# Always rebuilds the image (Docker layer cache makes this fast when nothing changed).
#
# Usage: ./scripts/test-e2e.sh [-j N] [TEST...]
#   -j N     Run N pytest-xdist workers (default: 4). Each worker runs its
#            test modules sequentially with per-test tmp dirs, MCP sessions,
#            Chrome instances, and auto-picked fixture ports, so parallel
#            workers never collide. 4 is the measured sweet spot: Chrome is
#            CPU/memory heavy, so parallelism beyond ~4 stops helping on an
#            8-core box.
#   TEST     One or more test files to run instead of the full suite.
#            Each resolves to a file under tests/e2e/: a full path
#            (tests/e2e/test_01_browser.py), a bare filename
#            (test_01_browser.py), or a number prefix (01 -> test_01_browser.py).
#            With no TEST args, runs every module.
#            Typical agent workflow: run the one file you changed first
#            (./scripts/test-e2e.sh 01), then the full suite to confirm
#            nothing else broke.
set -euo pipefail

cd "$(dirname "$0")/.."

IMAGE_TAG="odda-test:latest"

docker build -f tests/e2e/Dockerfile -t "$IMAGE_TAG" .

JOBS=4

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

PYTEST_ARGS=(tests/e2e -n "$JOBS" -q)
if [ "$#" -gt 0 ]; then
  SELECTED=()
  for arg in "$@"; do
    # Accept full paths, bare filenames, or number prefixes (01 ->
    # test_01_browser.py): try the exact file, then test_<arg>*.py.
    arg="${arg#tests/e2e/}"
    matches=""
    [ -f "tests/e2e/$arg" ] && matches="tests/e2e/$arg"
    if [ -z "$matches" ]; then
      matches=$(ls tests/e2e/test_"$arg"*.py 2>/dev/null | sort -u) || true
    fi
    if [ -z "$matches" ]; then
      echo "error: no test file matches '$arg' in tests/e2e/" >&2
      exit 1
    fi
    SELECTED+=($matches)
  done
  PYTEST_ARGS=("${SELECTED[@]}")
fi

docker run --rm "$IMAGE_TAG" pytest "${PYTEST_ARGS[@]}"