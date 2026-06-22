#!/usr/bin/env bash
# POSIX build script for Phase-2. Produces dist/NexusGrid (binary) via PyInstaller.
# Run from anywhere; the script cd's to the Phase-2 root first.

set -euo pipefail

cd "$(dirname "$0")/.."

rm -rf build/_work dist

# Build the React UI bundle (webui/dist/bundle.js) before packaging — the spec
# bundles it as a data file, same as build.bat does on Windows.
echo "Building webui bundle..."
( cd webui && npm install --no-audit --no-fund --loglevel=error && npm run build )

pyinstaller --clean --noconfirm \
    --workpath build/_work \
    --distpath dist \
    build/NexusGrid.spec

echo
echo "Build complete: dist/NexusGrid"
