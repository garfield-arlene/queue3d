#!/bin/bash
# Run from your own machine (the Mac laptop, or here) - installs queue3d
# on the Pi if it isn't there yet, or upgrades it in place if it is. Same
# command either way; every step on the Pi side (remote_install.sh) is
# idempotent, so there's no separate "first time" mode to remember.
#
# Needs: SSH access to the Pi with sudo privileges (your own account -
# see deploy/README.md for the "only I can log in" SSH setup this assumes,
# which this script doesn't itself configure). Needs deploy/cache/
# populated first - run fetch_bundle_assets.sh on a machine WITH internet
# before this, since the Pi itself never touches the network.
#
# Usage: ./deploy.sh pi@queue3d.local
set -euo pipefail
cd "$(dirname "$0")"

if [ $# -ne 1 ]; then
  echo "Usage: $0 <user@host>" >&2
  echo "Example: $0 pi@queue3d.local" >&2
  exit 1
fi
TARGET="$1"
# /tmp, not /opt - this is only ever temporary staging (deleted at the end
# of this script), and critically, every step that populates it (mkdir,
# rsync below) runs as your plain SSH user, before sudo ever comes into
# it - /opt normally requires root to write to at all, which would fail
# here with nothing yet to escalate privilege. Only remote_install.sh
# itself (invoked via sudo further down) needs root, to write the real,
# permanent install to /opt/queue3d.
STAGING_DIR=/tmp/queue3d-deploy-staging

if [ ! -d cache/wheels ] || [ -z "$(ls -A cache/wheels 2>/dev/null)" ]; then
  echo "deploy/cache/wheels is empty - run ./fetch_bundle_assets.sh on a machine with internet first." >&2
  exit 1
fi
if ! ls cache/OrcaSlicer-aarch64-*.AppImage >/dev/null 2>&1; then
  echo "No cached OrcaSlicer AppImage - run ./fetch_bundle_assets.sh first." >&2
  exit 1
fi

echo "Staging files on $TARGET:$STAGING_DIR ..."
ssh "$TARGET" "mkdir -p $STAGING_DIR"

# The real application source - excludes match remote_install.sh's own
# (data/.venv/__pycache__/tools are either regenerated on the Pi or never
# meant to be overwritten by a deploy at all).
rsync -az --delete \
  --exclude 'data' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  ../app "$TARGET:$STAGING_DIR/"
rsync -az --delete \
  --exclude 'tools' \
  --exclude '__pycache__' \
  ../slicing "$TARGET:$STAGING_DIR/"

# The offline-install assets (wheels + OrcaSlicer) and the two scripts
# that actually apply everything on the Pi side.
rsync -az cache/wheels "$TARGET:$STAGING_DIR/"
rsync -az cache/OrcaSlicer-aarch64-*.AppImage "$TARGET:$STAGING_DIR/"
rsync -az queue3d.service "$TARGET:$STAGING_DIR/"
rsync -az nginx-queue3d.conf "$TARGET:$STAGING_DIR/"
rsync -az remote_install.sh "$TARGET:$STAGING_DIR/"

echo "Running the install/upgrade on $TARGET (needs your sudo password there)..."
ssh -t "$TARGET" "sudo bash $STAGING_DIR/remote_install.sh"

echo "Done. Cleaning up the staging directory..."
ssh "$TARGET" "rm -rf $STAGING_DIR"
