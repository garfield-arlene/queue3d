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

# Deliberately NOT this project's own app/.venv - this script has to run
# on whatever machine you actually have internet on when you need it
# (this dev machine, or a fresh clone on a Mac with nothing else set up
# yet), not only a machine that happens to already have that venv built.
# pip's cross-platform --platform/--python-version/--abi flags below work
# from any reasonably modern pip3 - the *target* wheels fetched are for
# the Pi's architecture and Python version, never this machine's own, so
# there's no real requirement on which Python actually runs pip itself.
if command -v python3 >/dev/null 2>&1; then
  PIP="python3 -m pip"
elif command -v pip3 >/dev/null 2>&1; then
  PIP="pip3"
else
  echo "No python3/pip3 found on this machine - install Python 3 first" >&2
  echo "(on a Mac: https://www.python.org/downloads/macos/ or 'brew install python3')." >&2
  exit 1
fi

# The real target Pi (confirmed directly via `python3 --version` over SSH,
# not assumed): Raspberry Pi OS Lite 64-bit is now based on Debian 13
# "Trixie", shipping Python 3.13.5 - Raspberry Pi OS moved past the
# Debian 12 "Bookworm" (Python 3.11) this was first built against between
# when that assumption was made and when the real Pi was actually imaged.
# Change PY_VERSION/PY_ABI together if a future re-image ends up on yet
# another version - re-check with `python3 --version` on the Pi itself
# rather than assuming this still matches.
PY_VERSION=313
PY_ABI=cp313
ORCASLICER_VERSION=2.4.2

mkdir -p cache/wheels

echo "Fetching pip wheels for linux aarch64 / Python $PY_VERSION (not this machine's own architecture)..."
$PIP download \
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
