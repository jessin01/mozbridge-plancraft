#!/usr/bin/env bash
# Build the plancraft sdist + wheel and verify the artifact with twine check.
#
# Does NOT upload anywhere -- publishing to a package index is a separate,
# deliberate step (not part of this script).
#
# Usage:
#   scripts/build.sh              # build into ./dist
#   scripts/build.sh /tmp/out     # build into a custom output dir

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${1:-"$ROOT_DIR/dist"}"

cd "$ROOT_DIR"

PYTHON="${PYTHON:-python3}"

echo "==> Building sdist + wheel into: $OUT_DIR"
rm -rf "$OUT_DIR"
"$PYTHON" -m build --outdir "$OUT_DIR"

echo "==> twine check"
"$PYTHON" -m twine check "$OUT_DIR"/*

echo "==> Built artifacts:"
ls -la "$OUT_DIR"
