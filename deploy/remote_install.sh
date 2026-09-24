#!/bin/bash
# Runs ON the Pi (invoked by deploy.sh over SSH, via sudo - never run this
# by hand unless you're deliberately re-running just the install step).
# Everything here is idempotent: safe to run on a bare filesystem (fresh
# install) or a hundred times against an already-running service (upgrade)
# - the whole point, since the same script has to cover both per the
# deployment requirement ("install if not already; upgrade if already
# installed"). Every *application* dependency (Python packages,
# OrcaSlicer) always comes from what's already been bundled alongside this
# script, never the network - the real deployment site never has internet
# at all. A couple of plain OS packages (nginx, openssl) are the one
# exception: those get apt-installed here too, on demand, since they only
# ever need internet once (during initial setup) - see below.
set -euo pipefail

# Pinned explicitly rather than trusting whatever sudo/ssh -t happens to
# inherit - useradd/nginx both live under /usr/sbin, and a real run hit
# "useradd: command not found" despite the binary being present on disk,
# almost certainly because that directory wasn't on PATH in that
# particular invocation. This removes the whole class of problem outright
# rather than depending on the caller's environment being right.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

APP_DIR=/opt/queue3d
STAGING_DIR=/tmp/queue3d-deploy-staging  # must match deploy.sh's own STAGING_DIR
DATA_MOUNT=/mnt/queue3d-data  # must match queue3d.service's QUEUE3D_DATA_DIR
# Exported for the whole script, not just queue3d.service's own copy of
# this same setting - the pre-upgrade backup step below runs a plain
# `python3 -c` invoking backup.py's own backup_database() directly, and
# needs this in its environment too. Safe against code from before this
# variable existed at all too: an old db.py simply ignores env vars it
# doesn't know about and resolves its hardcoded local path exactly like
# it always did - this only takes effect once the rsync further down has
# actually deployed the new, env-var-aware db.py.
export QUEUE3D_DATA_DIR="$DATA_MOUNT"

if [ "$(id -u)" -ne 0 ]; then
  echo "Must run as root (deploy.sh invokes this via sudo)." >&2
  exit 1
fi

# The live database + scratch/queue/archive live on a dedicated external
# drive (see queue3d.service's own RequiresMountsFor/QUEUE3D_DATA_DIR),
# not the SD card - this script itself writes there directly too (the
# one-time migration below), so it needs the same guard db.py enforces at
# app startup: refuse to proceed against an unmounted path rather than
# silently treating an ordinary empty directory on local disk as if it
# were the real drive.
if ! mountpoint -q "$DATA_MOUNT"; then
  echo "$DATA_MOUNT is not currently a mounted filesystem - refusing to" >&2
  echo "continue. Check the live-data USB drive is plugged in and set up" >&2
  echo "in /etc/fstab (see deploy/README.md's storage setup section)." >&2
  exit 1
fi

# nginx/openssl/python3-venv are plain OS packages, not application
# dependencies - unlike the Python packages and OrcaSlicer (which must
# stay bundled forever, since the real deployment site never has
# internet at all), these only ever need internet once, right now,
# during this initial setup phase - so it's fine, and more genuinely
# "remote", for this script to just apt-get them itself rather than
# making you SSH in separately first. Every later run at the real
# (offline) deployment site is a no-op here, since by then they're
# already installed - this never reaches the network on an ordinary
# upgrade. (rsync isn't included here even though it's the same kind of
# package - deploy.sh's own rsync calls, from your machine to this Pi,
# have to succeed just to get this script onto the Pi at all, so rsync
# genuinely has to already be present beforehand; nothing later in this
# script can bootstrap it.)
MISSING_PKGS=""
command -v nginx >/dev/null 2>&1 || MISSING_PKGS="$MISSING_PKGS nginx"
command -v openssl >/dev/null 2>&1 || MISSING_PKGS="$MISSING_PKGS openssl"
dpkg -s python3-venv >/dev/null 2>&1 || MISSING_PKGS="$MISSING_PKGS python3-venv"
if [ -n "$MISSING_PKGS" ]; then
  echo "Installing missing OS packages ($MISSING_PKGS) - needs internet..."
  if ! (DEBIAN_FRONTEND=noninteractive apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y $MISSING_PKGS); then
    echo "" >&2
    echo "Could not install:$MISSING_PKGS - most likely no internet is" >&2
    echo "reachable from the Pi right now. This only needs to succeed once," >&2
    echo "so if this is a genuinely offline deployment, install these" >&2
    echo "manually the next time the Pi has internet:" >&2
    echo "  sudo apt install -y$MISSING_PKGS" >&2
    exit 1
  fi
fi

# A dedicated, unprivileged, no-login system account for the service
# itself - never the account you actually log in with. Created once;
# harmless to "recreate" on every run since useradd just no-ops if it
# already exists.
if ! id -u queue3d >/dev/null 2>&1; then
  echo "Creating the queue3d system user (no login shell, no password)..."
  useradd --system --no-create-home --shell /usr/sbin/nologin queue3d
fi

# Per the user, asked directly before ever relying on this for a real
# upgrade rather than after a bad one: "does the upgrade process trigger
# a backup in case it fails? I don't want to leave it in an unstable
# state." It didn't, until now - the rsync --delete calls just below this
# would otherwise overwrite the previous working code with no way back,
# and a bad upgrade could leave a broken service with nothing to restore
# from. Skipped entirely on a genuine first install (nothing exists yet
# to back up) - only runs when $APP_DIR/app is already a real install.
# Checks both possible database locations: the old local path (still
# where it lives on the very first deploy that introduces the external
# data drive - see the migration step below) and the new mount (every
# deploy after that one).
BACKUP_ROOT=/opt/queue3d-backups
if [ -d "$APP_DIR/app" ] && { [ -f "$APP_DIR/app/data/queue3d.db" ] || [ -f "$DATA_MOUNT/queue3d.db" ]; }; then
  BACKUP_DIR="$BACKUP_ROOT/$(date -u +%Y%m%dT%H%M%SZ)"
  echo "Existing install found - backing up code and database to $BACKUP_DIR before upgrading..."
  mkdir -p "$BACKUP_DIR"
  # Code: a plain snapshot, never touched again unless actually restoring
  # from it - not the same rsync --delete the real sync below uses. Skips
  # data/ (backed up properly, below, not just file-copied) and .venv/
  # (fully regenerated from the bundled wheels either way, nothing
  # meaningful to preserve there).
  cp -a "$APP_DIR/app" "$BACKUP_DIR/app"
  rm -rf "$BACKUP_DIR/app/data" "$BACKUP_DIR/app/.venv"
  cp -a "$APP_DIR/slicing" "$BACKUP_DIR/slicing"
  rm -rf "$BACKUP_DIR/slicing/tools"
  # Database: the exact same safe, consistent online-backup-API copy
  # backup.py's own backup_database() uses (not a raw file copy, which
  # could grab a half-written page) - run via the OLD venv, still fully
  # intact at this point since nothing has touched it yet.
  if "$APP_DIR/app/.venv/bin/python3" -c "
import sys; sys.path.insert(0, '$APP_DIR/app')
from pathlib import Path
from backup import backup_database
backup_database(Path('$BACKUP_DIR/db'))
"; then
    echo "Pre-upgrade backup complete."
  else
    echo "WARNING: pre-upgrade database backup failed - continuing anyway, but there is no database rollback point for this particular upgrade." >&2
  fi
  # Keep the 5 most recent pre-upgrade backups, not an unbounded pile on
  # a Pi's limited SD card - oldest deleted first.
  ls -1dt "$BACKUP_ROOT"/*/ 2>/dev/null | tail -n +6 | xargs -r rm -rf
else
  echo "No existing install found - nothing to back up (this is a fresh install)."
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

# One-time migration of the live data from local disk to the external
# drive, per the user's original storage plan (see project memory
# queue3d-app-progress: microSD boots the OS, one flash drive holds the
# live data, the other two rotate as backups) - never actually wired up
# until now. Only triggers the first time this runs after $DATA_MOUNT
# exists but has no database of its own yet (idempotent - a no-op on
# every later run) and there's actually something on local disk to bring
# over (nothing to migrate on a genuinely fresh install, which never had
# a local database in the first place).
if [ -f "$APP_DIR/app/data/queue3d.db" ] && [ ! -f "$DATA_MOUNT/queue3d.db" ]; then
  echo "Migrating existing live data from local disk to $DATA_MOUNT (one-time)..."
  cp -a "$APP_DIR/app/data/." "$DATA_MOUNT/"
  chown -R queue3d:queue3d "$DATA_MOUNT"
fi

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
# Defensive, not just for the migration step above - keeps ownership
# correct even on a run where nothing needed migrating (idempotent, same
# as everything else here).
chown -R queue3d:queue3d "$DATA_MOUNT"

echo "Installing the systemd unit..."
cp "$STAGING_DIR/queue3d.service" /etc/systemd/system/queue3d.service
systemctl daemon-reload
systemctl enable queue3d

# Daily backup (backup.py) and draft-expiry cleanup (cleanup_drafts.py),
# now actually wired in rather than left as "run manually to test, or
# wire into cron for real deployment" in each script's own docstring -
# systemd timers instead of plain cron, for the same systemctl/
# journalctl visibility everything else here already has. Installing the
# .service units too (not just the .timer units) since the timer
# activates them by name.
echo "Installing the backup/cleanup timers..."
cp "$STAGING_DIR/queue3d-backup.service" /etc/systemd/system/queue3d-backup.service
cp "$STAGING_DIR/queue3d-backup.timer" /etc/systemd/system/queue3d-backup.timer
cp "$STAGING_DIR/queue3d-cleanup.service" /etc/systemd/system/queue3d-cleanup.service
cp "$STAGING_DIR/queue3d-cleanup.timer" /etc/systemd/system/queue3d-cleanup.timer
systemctl daemon-reload
systemctl enable --now queue3d-backup.timer queue3d-cleanup.timer

# Self-signed TLS cert for nginx's https listener. Per the user, plain
# http wasn't good enough even on an isolated LAN - but there's no CA
# reachable at the deployment site to get a real one from (zero internet,
# by design, same reason nothing here ever runs apt/pip against the real
# internet), so self-signed is the only option at all. Generated once, on
# the Pi itself, and left alone on every later run - regenerating it on
# every upgrade would invalidate the cert everyone already clicked
# "trust" on, forcing that warning again for no reason.
SSL_DIR=/etc/nginx/ssl
if [ ! -f "$SSL_DIR/queue3d.crt" ] || [ ! -f "$SSL_DIR/queue3d.key" ]; then
  echo "Generating a self-signed TLS certificate (first run only)..."
  mkdir -p "$SSL_DIR"
  HOST_NAME="$(hostname -f 2>/dev/null || hostname)"
  openssl req -x509 -nodes -newkey rsa:2048 \
    -keyout "$SSL_DIR/queue3d.key" -out "$SSL_DIR/queue3d.crt" \
    -days 3650 \
    -subj "/CN=$HOST_NAME" \
    -addext "subjectAltName=DNS:$HOST_NAME,DNS:localhost,IP:127.0.0.1"
  chmod 600 "$SSL_DIR/queue3d.key"
else
  echo "Existing self-signed TLS certificate found - leaving it in place."
fi

echo "Configuring nginx..."
cp "$STAGING_DIR/nginx-queue3d.conf" /etc/nginx/sites-available/queue3d
ln -sf /etc/nginx/sites-available/queue3d /etc/nginx/sites-enabled/queue3d
rm -f /etc/nginx/sites-enabled/default
# Verified before ever reloading, not after - a broken config left in
# place by `nginx -t` failing loudly here is far better than silently
# restarting into a state where nginx won't come back up at all. This
# config couldn't be syntax-checked on the machine that wrote it (no
# nginx installed there) - this is the first real check it ever gets.
nginx -t
systemctl enable nginx
systemctl restart nginx

echo "Restarting queue3d..."
systemctl restart queue3d
sleep 3

# Deliberately reports clearly rather than attempting an automatic
# rollback - an automatic rollback has its own real failure modes (what
# if the restore itself goes wrong, unattended?), and the whole backup
# above exists precisely so a person can make that call with the actual
# situation in front of them, not have a script guess at 3am. This is
# the moment that tells you whether it worked at all. Checks both the
# app directly on its own internal port (isolates whether queue3d itself
# is the problem) and through nginx on 80 (the thing anyone on the
# network actually reaches) - reporting exactly which one failed rather
# than one combined pass/fail is worth the extra few lines here.
APP_OK=0; PROXY_OK=0
systemctl is-active --quiet queue3d && curl -sf -o /dev/null http://127.0.0.1:8000/login && APP_OK=1
# -k: the cert is self-signed (there's no CA to validate against here at
# all), so curl would otherwise refuse it on principle even though it's
# exactly the cert nginx was just told to use - this check only cares
# that TLS itself terminates and the app answers behind it.
curl -sfk -o /dev/null https://127.0.0.1/login && PROXY_OK=1

if [ "$APP_OK" -eq 1 ] && [ "$PROXY_OK" -eq 1 ]; then
  echo "queue3d is up and reachable through nginx at https://<host>/."
else
  echo "" >&2
  echo "WARNING: something is not right after this deploy:" >&2
  [ "$APP_OK" -eq 1 ] || echo "  - queue3d itself is not running/responding on its internal port" >&2
  [ "$PROXY_OK" -eq 1 ] || echo "  - nginx is not proxying https to it successfully" >&2
  if [ -n "${BACKUP_DIR:-}" ]; then
    echo "This was an upgrade - the previous working version was backed up to:" >&2
    echo "  $BACKUP_DIR" >&2
    echo "See deploy/README.md's rollback section to restore it." >&2
  else
    echo "This was a fresh install, so there is no previous version to roll back to." >&2
  fi
  systemctl --no-pager status queue3d || true
  systemctl --no-pager status nginx || true
  exit 1
fi
