#!/usr/bin/env bash
# Build a standalone app with PyInstaller: dist/CullSpeed.app on macOS,
# dist/CullSpeed/ elsewhere. Run from an environment with `pip install -e ".[build]"`.
set -euo pipefail
cd "$(dirname "$0")/.."

icon_args=()
if [[ "$(uname)" == "Darwin" ]]; then
    python scripts/make_icns.py build/CullSpeed.icns
    icon_args=(--icon build/CullSpeed.icns)
fi

python -m PyInstaller \
    --name CullSpeed \
    --windowed \
    --noconfirm \
    --clean \
    --osx-bundle-identifier io.github.ozraam.cullspeed \
    --collect-binaries rawpy \
    --add-data cullspeed/resources:cullspeed/resources \
    ${icon_args[@]+"${icon_args[@]}"} \
    CullSpeed.py
