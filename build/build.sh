#!/usr/bin/env bash
# POSIX build script for Phase-2. Produces dist/NexusGrid (binary) via PyInstaller.
# Run from anywhere; the script cd's to the Phase-2 root first.

set -euo pipefail

cd "$(dirname "$0")/.."

rm -rf build/_work dist

pyinstaller --clean --noconfirm \
    --workpath build/_work \
    --distpath dist \
    build/NexusGrid.spec

echo
echo "Build complete: dist/NexusGrid"
