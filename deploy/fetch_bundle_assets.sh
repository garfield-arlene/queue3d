#!/bin/bash
# Run this on any machine WITH internet access (this dev machine, or your
# Mac) whenever requirements.txt or the pinned OrcaSlicer version changes -
# NOT on the Pi itself, which has none. Populates deploy/cache/ with
# everything deploy.sh needs to install/upgrade the app on the Pi without
# the Pi ever touching the network: prebuilt wheels for its exact
# architecture and Python version (not this machine's own - pip's own
# cross-platform download support fetches the *target's* wheels, verified
# working for every dependency this app has, no source builds needed), and
# the matching aarch64 OrcaSlicer AppImage.
#
# deploy/cache/ is gitignored (large binaries, nothing to diff meaningfully)
# but persists between runs - re-running this only re-fetches what actually
# changed.
set -euo pipefail
cd "$(dirname "$0")"

# Raspberry Pi OS Lite 64-bit (Debian 12 "Bookworm") ships Python 3.11 by
# default - change PY_VERSION/PY_ABI together if the real Pi ends up with a
# different version (check with `python3 --version` on the Pi itself once
# it's imaged, before assuming this still matches).
PY_VERSION=311
PY_ABI=cp311
ORCASLICER_VERSION=2.4.2

mkdir -p cache/wheels

echo "Fetching pip wheels for linux aarch64 / Python $PY_VERSION (not this machine's own architecture)..."
../app/.venv/bin/pip download \
  --platform manylinux2014_aarch64 \
  --platform manylinux_2_17_aarch64 \
  --platform manylinux_2_24_aarch64 \
  --platform manylinux_2_28_aarch64 \
  --platform manylinux_2_31_aarch64 \
  --python-version "$PY_VERSION" \
  --implementation cp \
  --abi "$PY_ABI" \
  --only-binary=:all: \
  --no-deps \
  -r ../app/requirements.txt \
  -d cache/wheels

ORCA_URL="https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v${ORCASLICER_VERSION}/OrcaSlicer_Linux_AppImage_Ubuntu2404_aarch64_V${ORCASLICER_VERSION}.AppImage"
ORCA_DEST="cache/OrcaSlicer-aarch64-v${ORCASLICER_VERSION}.AppImage"
if [ -f "$ORCA_DEST" ]; then
  echo "OrcaSlicer aarch64 v$ORCASLICER_VERSION already cached, skipping (delete $ORCA_DEST to re-fetch)."
else
  echo "Fetching OrcaSlicer aarch64 v$ORCASLICER_VERSION (~135MB)..."
  curl -sL -o "$ORCA_DEST" "$ORCA_URL"
  chmod +x "$ORCA_DEST"
fi

echo "Done. deploy/cache/ is ready for deploy.sh."
