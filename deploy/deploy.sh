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
# before this, since the Pi itself never touches the network. Also needs
# deploy/cache/tls/fullchain.pem and privkey.pem - a real TLS certificate,
# issued elsewhere (see deploy/README.md's "TLS certificate" section) and
# copied there by hand; there's no self-signed fallback and no way for
# this deployment to obtain one on its own.
#
# Usage: ./deploy.sh queue3d.local
# (or user@host, or a ~/.ssh/config Host alias - this is passed straight
# through to ssh/rsync, so anything they'd accept as a target works here)
set -euo pipefail
cd "$(dirname "$0")"

if [ $# -ne 1 ]; then
  echo "Usage: $0 <host>" >&2
  echo "Example: $0 queue3d.local   (or a ~/.ssh/config Host alias, or user@host)" >&2
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
# A real, publicly-trusted TLS certificate, not self-signed (see
# deploy/README.md's "TLS certificate" section for why, and how to get
# one) - this deployment has no internet access itself, so it can never
# obtain or renew one on its own; it has to be issued elsewhere and
# staged here, same pattern as the wheels/OrcaSlicer cache above.
if [ ! -f cache/tls/fullchain.pem ] || [ ! -f cache/tls/privkey.pem ]; then
  echo "deploy/cache/tls/fullchain.pem and/or privkey.pem are missing - see" >&2
  echo "deploy/README.md's 'TLS certificate' section for how to get a real" >&2
  echo "certificate and where to put it. There is no self-signed fallback." >&2
  exit 1
fi

echo "Staging files on $TARGET:$STAGING_DIR ..."
ssh "$TARGET" "mkdir -p $STAGING_DIR"

# --info=progress2 (a single running total for the whole transfer, not a
# per-file line) plus --no-i-r (computes the full file list up front, so
# that total - and its percentage - is accurate from the very first line
# instead of climbing as rsync discovers more files partway through) -
# per the user, after this step alone was visibly taking a while with no
# way to tell how far along it actually was. Needs rsync >= 3.1, which
# --info itself didn't exist before - stock macOS ships 2.6.9 (Apple
# stopped bundling anything past GPLv2, and 3.x is GPLv3) and would
# error out on an unrecognized option, confirmed directly against a real
# Mac hitting exactly that. Detected here rather than just documented,
# so this script keeps working (falling back to plain -v - at least
# each filename as it goes, if not a single running total) on whatever
# rsync happens to be on PATH, rather than requiring everyone running
# this to go install a newer one first. `brew install rsync` (or
# anything newer ahead of the stock one on PATH) gets the nicer bar.
# The `|| true` at the end is load-bearing, not decoration: under
# `set -euo pipefail` (top of this script), grep finding no match on
# whatever this particular rsync's --version banner actually looks like
# exits 1, pipefail propagates that as the whole command substitution's
# exit status, and set -e then kills the *entire deploy* right here -
# silently, no error message at all, immediately after the "Staging
# files" line above. Confirmed as a real, reproducible bug, not a
# hypothetical: a real report of the script doing exactly that (stopping
# dead right after that line, nothing after it, no error text) traced
# back to exactly this. `|| true` makes "couldn't parse a version
# number" a normal, survivable outcome - rsync_version simply ends up
# empty, and every use of it below already defaults safely to 0 via
# ${var:-0} for exactly that case.
rsync_version="$(rsync --version | head -1 | grep -oE '[0-9]+\.[0-9]+' | head -1 || true)"
rsync_major="${rsync_version%%.*}"
rsync_minor="${rsync_version#*.}"
if [ "${rsync_major:-0}" -gt 3 ] || { [ "${rsync_major:-0}" -eq 3 ] && [ "${rsync_minor:-0}" -ge 1 ]; }; then
  RSYNC_PROGRESS=(--info=progress2 --no-i-r)
else
  echo "Note: local rsync $rsync_version doesn't support --info=progress2 (needs 3.1+)" >&2
  echo "- falling back to -v for at least per-file progress. 'brew install rsync' gets the nicer bar." >&2
  RSYNC_PROGRESS=(-v)
fi

# The real application source - excludes match remote_install.sh's own
# (data/.venv/__pycache__/tools are either regenerated on the Pi or never
# meant to be overwritten by a deploy at all).
echo "  app/ ..."
rsync -az "${RSYNC_PROGRESS[@]}" --delete \
  --exclude 'data' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  ../app "$TARGET:$STAGING_DIR/"
echo "  slicing/ ..."
rsync -az "${RSYNC_PROGRESS[@]}" --delete \
  --exclude 'tools' \
  --exclude '__pycache__' \
  ../slicing "$TARGET:$STAGING_DIR/"

# The offline-install assets (wheels + OrcaSlicer) and the two scripts
# that actually apply everything on the Pi side.
echo "  wheels/ ..."
rsync -az "${RSYNC_PROGRESS[@]}" cache/wheels "$TARGET:$STAGING_DIR/"
echo "  OrcaSlicer AppImage ..."
rsync -az "${RSYNC_PROGRESS[@]}" cache/OrcaSlicer-aarch64-*.AppImage "$TARGET:$STAGING_DIR/"
echo "  TLS certificate ..."
# Already going over SSH like everything else here, so this is no less
# protected in transit than the app source itself - rsync's own -p isn't
# used to preserve the private key's 600 permissions specifically, since
# remote_install.sh re-chmods it explicitly once installed regardless.
rsync -az "${RSYNC_PROGRESS[@]}" cache/tls/fullchain.pem cache/tls/privkey.pem "$TARGET:$STAGING_DIR/"
rsync -az "${RSYNC_PROGRESS[@]}" queue3d.service "$TARGET:$STAGING_DIR/"
rsync -az "${RSYNC_PROGRESS[@]}" queue3d-backup.service queue3d-backup.timer "$TARGET:$STAGING_DIR/"
rsync -az "${RSYNC_PROGRESS[@]}" queue3d-cleanup.service queue3d-cleanup.timer "$TARGET:$STAGING_DIR/"
rsync -az "${RSYNC_PROGRESS[@]}" nginx-queue3d.conf "$TARGET:$STAGING_DIR/"
rsync -az "${RSYNC_PROGRESS[@]}" remote_install.sh "$TARGET:$STAGING_DIR/"

echo "Running the install/upgrade on $TARGET (needs your sudo password there)..."
ssh -t "$TARGET" "sudo bash $STAGING_DIR/remote_install.sh"

echo "Done. Cleaning up the staging directory..."
ssh "$TARGET" "rm -rf $STAGING_DIR"
