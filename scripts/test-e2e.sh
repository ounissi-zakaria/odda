#!/usr/bin/env bash
# Run the e2e test suite inside a Docker container.
# Always rebuilds the image (Docker layer cache makes this fast when nothing changed).
#
# Usage: ./scripts/test-e2e.sh [-j N] [TEST...]
#   -j N     Run N test documents in parallel (default: 4).
#            Each `scrut test <file>` runs in its own $PWD with its own
#            odda server and auto-picked fixture port, so parallel docs
#            never collide. Use -j 1 for sequential, -j 0 for unlimited.
#            4 is the measured sweet spot: Chrome is CPU/memory heavy,
#            so parallelism beyond ~4 stops helping on an 8-core box.
#   TEST     One or more test files to run instead of the full suite.
#            Each resolves to a file under tests/e2e/scrut/: a full path
#            (tests/e2e/scrut/02-userscripts.md), a bare filename
#            (02-userscripts.md), or a prefix (02 -> 02-userscripts.md).
#            With no TEST args, runs every numbered doc in parallel.
#            Typical agent workflow: run the one file you changed first
#            (./scripts/test-e2e.sh 02), then the full suite to confirm
#            nothing else broke (./scripts/test-e2e.sh).
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
TESTS=""
if [ "$#" -gt 0 ]; then
  for arg in "$@"; do
    # Accept full paths, bare filenames, or prefixes (02 -> 02-userscripts.md).
    arg="${arg#tests/e2e/scrut/}"
    # Try exact match first, then prefix glob. The glob also covers the
    # case where the arg is a bare filename (02-userscripts.md*.md -> itself).
    matches=""
    [ -f "tests/e2e/scrut/$arg" ] && matches="tests/e2e/scrut/$arg"
    if [ -z "$matches" ]; then
      matches=$(ls tests/e2e/scrut/"$arg"*.md 2>/dev/null | sort -u) || true
    fi
    if [ -z "$matches" ]; then
      echo "error: no test file matches '$arg' in tests/e2e/scrut/" >&2
      exit 1
    fi
    TESTS="$TESTS $matches"
  done
fi

# Quote the test list safely for the inner shell; fall back to the glob
# so the default (no args) runs every numbered doc.
if [ -n "$TESTS" ]; then
  TEST_FILES="printf '%s\n' $TESTS"
else
  TEST_FILES='ls tests/e2e/scrut/[0-9]*.md'
fi

docker run --rm "$IMAGE_TAG" bash -c "
  $TEST_FILES | xargs -n1 -P $JOBS scrut test
"