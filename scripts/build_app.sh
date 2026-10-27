#!/usr/bin/env bash
# Build a standalone app with PyInstaller: dist/CullSpeed.app on macOS,
# dist/CullSpeed/ elsewhere. Run from an environment with `pip install -e ".[build]"`.
set -euo pipefail
cd "$(dirname "$0")/.."

python -m PyInstaller \
    --name CullSpeed \
    --windowed \
    --noconfirm \
    --clean \
    --osx-bundle-identifier io.github.ozraam.cullspeed \
    --collect-binaries rawpy \
    CullSpeed.py
