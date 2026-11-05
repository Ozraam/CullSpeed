#!/usr/bin/env bash
# Build a standalone app with PyInstaller: dist/Tamis.app on macOS,
# dist/Tamis/ elsewhere. Run from an environment with `pip install -e ".[build]"`.
set -euo pipefail
cd "$(dirname "$0")/.."

icon_args=()
if [[ "$(uname)" == "Darwin" ]]; then
    python scripts/make_icns.py build/Tamis.icns
    icon_args=(--icon build/Tamis.icns)
fi

python -m PyInstaller \
    --name Tamis \
    --windowed \
    --noconfirm \
    --clean \
    --osx-bundle-identifier io.github.ozraam.tamis \
    --collect-binaries rawpy \
    --add-data tamis/resources:tamis/resources \
    ${icon_args[@]+"${icon_args[@]}"} \
    Tamis.py
