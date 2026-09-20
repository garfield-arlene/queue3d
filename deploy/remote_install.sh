#!/bin/bash
# Runs ON the Pi (invoked by deploy.sh over SSH, via sudo - never run this
# by hand unless you're deliberately re-running just the install step).
# Everything here is idempotent: safe to run on a bare filesystem (fresh
# install) or a hundred times against an already-running service (upgrade)
# - the whole point, since the same script has to cover both per the
# deployment requirement ("install if not already; upgrade if already
# installed"). Never touches the network - every package comes from the
# wheels already rsynced alongside this script.
set -euo pipefail

APP_DIR=/opt/queue3d
STAGING_DIR=/tmp/queue3d-deploy-staging  # must match deploy.sh's own STAGING_DIR

if [ "$(id -u)" -ne 0 ]; then
  echo "Must run as root (deploy.sh invokes this via sudo)." >&2
  exit 1
fi

# A dedicated, unprivileged, no-login system account for the service
# itself - never the account you actually log in with. Created once;
# harmless to "recreate" on every run since useradd just no-ops if it
# already exists.
if ! id -u queue3d >/dev/null 2>&1; then
  echo "Creating the queue3d system user (no login shell, no password)..."
  useradd --system --no-create-home --shell /usr/sbin/nologin queue3d
fi

echo "Syncing staged files into $APP_DIR..."
mkdir -p "$APP_DIR/app" "$APP_DIR/slicing"
# Two separate calls, not one rsync given both source dirs at once -
# exclude-pattern anchoring gets ambiguous across multiple sources in a
# single call, and getting this wrong (deleting app/data, say) would be a
# real, serious problem, not just cosmetic. --delete never removes
# something excluded on the source side unless --delete-excluded is
# passed (not passed here) - data/ at the destination (the live database
# and every job's files) survives every deploy untouched, deliberately.
rsync -a --delete --exclude 'data' --exclude '.venv' --exclude '__pycache__' \
  "$STAGING_DIR/app/" "$APP_DIR/app/"
rsync -a --delete --exclude 'tools' --exclude '__pycache__' \
  "$STAGING_DIR/slicing/" "$APP_DIR/slicing/"

mkdir -p "$APP_DIR/app/data"

if [ ! -d "$APP_DIR/app/.venv" ]; then
  echo "Creating the virtualenv (first install)..."
  python3 -m venv "$APP_DIR/app/.venv"
fi

echo "Installing/upgrading Python dependencies from the bundled wheels only (no network)..."
"$APP_DIR/app/.venv/bin/pip" install --no-index --find-links="$STAGING_DIR/wheels" -r "$APP_DIR/app/requirements.txt"

echo "Extracting OrcaSlicer..."
ORCA_APPIMAGE=$(ls "$STAGING_DIR"/OrcaSlicer-aarch64-*.AppImage 2>/dev/null | head -1)
if [ -z "$ORCA_APPIMAGE" ]; then
  echo "No OrcaSlicer AppImage found in $STAGING_DIR - skipping (leaving whatever's already extracted, if anything)." >&2
else
  mkdir -p "$APP_DIR/slicing/tools"
  rm -rf "$APP_DIR/slicing/tools/squashfs-root"
  ( cd "$APP_DIR/slicing/tools" && "$ORCA_APPIMAGE" --appimage-extract >/dev/null )
fi

echo "Setting ownership..."
chown -R queue3d:queue3d "$APP_DIR"

echo "Installing the systemd unit..."
cp "$STAGING_DIR/queue3d.service" /etc/systemd/system/queue3d.service
systemctl daemon-reload
systemctl enable queue3d

echo "Restarting the service..."
systemctl restart queue3d
sleep 2
systemctl --no-pager status queue3d
