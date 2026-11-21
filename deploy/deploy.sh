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
# copied there - run ./prep_deploy.sh <cert-host> <remote-cert-dir> first
# to fetch it (and pull the latest code) in one step, or copy the two
# files there by hand; there's no
# self-signed fallback and no way for
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

# The last thing every run prints is one large SUCCESS or FAIL, so the
# outcome can be read at a glance instead of pieced together from
# scrollback. SUCCESS needs the final check at the bottom of this script
# to pass: queue3d running on the Pi and serving this checkout's own
# app/VERSION. A field deploy once looked clean and left the Pi on the
# previous version, which nothing here caught. Any other way out - a
# failed check below, remote_install.sh failing, ssh dropping, Ctrl-C -
# lands in on_exit and prints FAIL.
EXPECTED_VERSION="$(tr -d '[:space:]' < ../app/VERSION)"
SOURCE_DESC="$(git describe --tags --always --dirty 2>/dev/null || echo "not a git checkout")"
PREVIOUS_VERSION=""
RESULT_SHOWN=0
if [ -t 1 ]; then
  GREEN=$'\033[1;32m'; RED=$'\033[1;31m'; RESET=$'\033[0m'
else
  GREEN=""; RED=""; RESET=""
fi

show_success() {
  RESULT_SHOWN=1
  echo
  echo "$GREEN"
  cat <<'EOF'
   #####   #     #   #####    #####   #######   #####    #####
  #     #  #     #  #     #  #     #  #        #     #  #     #
  #        #     #  #        #        #        #        #
   #####   #     #  #        #        #####     #####    #####
        #  #     #  #        #        #              #        #
  #     #  #     #  #     #  #     #  #        #     #  #     #
   #####    #####    #####    #####   #######   #####    #####
EOF
  echo "$RESET"
  echo "  queue3d $EXPECTED_VERSION is running on $TARGET."
  echo "  Before: ${PREVIOUS_VERSION:-(fresh install)}   Now: $EXPECTED_VERSION   Deployed from: $SOURCE_DESC"
  if [ "$PREVIOUS_VERSION" = "$EXPECTED_VERSION" ]; then
    echo "  Note: the Pi was already on $EXPECTED_VERSION before this deploy."
  fi
  echo
}

# Arguments: one reason each.
show_fail() {
  RESULT_SHOWN=1
  echo
  echo "$RED"
  cat <<'EOF'
  #######     #     ###  #
  #          # #     #   #
  #         #   #    #   #
  #####    #     #   #   #
  #        #######   #   #
  #        #     #   #   #
  #        #     #  ###  #######
EOF
  echo "$RESET"
  local reason
  for reason in "$@"; do
    echo "  - $reason"
  done
  echo "  Target: $TARGET   Before: ${PREVIOUS_VERSION:-unknown}   Expected: $EXPECTED_VERSION   Deployed from: $SOURCE_DESC"
  echo
}

on_exit() {
  local status=$?
  if [ "$RESULT_SHOWN" -eq 0 ]; then
    show_fail "deploy.sh stopped before finishing (exit status $status) - see the messages above."
  fi
}
trap on_exit EXIT

if [ ! -d cache/wheels ] || [ -z "$(ls -A cache/wheels 2>/dev/null)" ]; then
  echo "deploy/cache/wheels is empty - run ./fetch_bundle_assets.sh on a machine with internet first." >&2
  exit 1
fi
# Not just "non-empty" - every pin in requirements.txt needs its exact
# wheel here, since the Pi's pip runs --no-index and has nowhere else to
# look. A real field upgrade to 9.2.0 got all the way through staging
# and partway into remote_install.sh before pip found a cache last
# refreshed for 8.x (idna 3.19 vs a 3.20 pin, and numpy/scipy/trimesh
# missing outright). Checked here, before anything touches the Pi.
# Names are compared normalized (lowercase, -/. -> _) since wheel
# filenames and requirements.txt don't always agree on spelling
# (Jinja2 vs jinja2, python-dotenv vs python_dotenv). Written for the
# Mac's stock bash 3.2 - no associative arrays or ${var,,}.
CACHED_WHEELS="$(for whl in cache/wheels/*.whl; do
  base="$(basename "$whl")"
  rest="${base#*-}"
  echo "$(echo "${base%%-*}" | tr 'A-Z' 'a-z' | tr -- '-.' '__')==${rest%%-*}"
done)"
MISSING_WHEELS=""
while IFS= read -r req || [ -n "$req" ]; do
  req="$(echo "${req%%#*}" | tr -d '[:space:]')"
  case "$req" in *==*) ;; *) continue ;; esac
  want="$(echo "${req%%==*}" | tr 'A-Z' 'a-z' | tr -- '-.' '__')==${req#*==}"
  echo "$CACHED_WHEELS" | grep -qxF "$want" || MISSING_WHEELS="$MISSING_WHEELS $req"
done < ../app/requirements.txt
if [ -n "$MISSING_WHEELS" ]; then
  echo "deploy/cache/wheels doesn't match app/requirements.txt - missing:" >&2
  for req in $MISSING_WHEELS; do echo "  $req" >&2; done
  echo "Run ./prep_deploy.sh (or ./fetch_bundle_assets.sh) on a machine with internet first." >&2
  exit 1
fi
if ! ls cache/OrcaSlicer-aarch64-*.AppImage >/dev/null 2>&1; then
  echo "No cached OrcaSlicer AppImage - run ./fetch_bundle_assets.sh first." >&2
  exit 1
fi
if [ ! -d cache/debs ] || [ -z "$(ls -A cache/debs 2>/dev/null)" ]; then
  echo "deploy/cache/debs is empty - run ./fetch_bundle_assets.sh on a machine with internet first." >&2
  exit 1
fi
if [ ! -f cache/debs-orcaslicer/Packages ] || [ ! -f cache/debs-orcaslicer/roots.txt ]; then
  echo "deploy/cache/debs-orcaslicer is missing or incomplete (OrcaSlicer's host libraries) -" >&2
  echo "run ./prep_deploy.sh (or ./fetch_bundle_assets.sh) on a machine with internet first." >&2
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
# Also reads the version installed now (empty on a fresh install), for
# the before/after line in the final report.
PREVIOUS_VERSION="$(ssh "$TARGET" "mkdir -p $STAGING_DIR && { cat /opt/queue3d/app/VERSION 2>/dev/null || true; }" | tr -d '[:space:]')"

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
echo "  dnsmasq + dependencies (.deb) ..."
rsync -az "${RSYNC_PROGRESS[@]}" cache/debs "$TARGET:$STAGING_DIR/"
echo "  OrcaSlicer host libraries (.deb, ~190MB) ..."
# --delete: resolve_debs.py prunes superseded .debs from the cache, and
# a stale one left behind on the Pi side would just be dead weight.
rsync -az "${RSYNC_PROGRESS[@]}" --delete cache/debs-orcaslicer "$TARGET:$STAGING_DIR/"
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

# Final check, independent of everything remote_install.sh reported: ask
# the running app which version it is. Every page's footer reads
# "queue3d v<VERSION>" from the VERSION file loaded at startup, so this
# is what the restarted process is actually serving, not just what was
# copied to disk.
echo "Final check: confirming $TARGET is serving queue3d $EXPECTED_VERSION ..."
read -r -d '' REMOTE_CHECK <<'EOF' || true
echo "state=$(systemctl is-active queue3d)"
echo "version=$(curl -sk --max-time 15 https://127.0.0.1/login | grep -o '<footer>queue3d v[^<]*' | head -1 | sed 's/^<footer>queue3d v//')"
EOF
FINAL_CHECK="$(ssh "$TARGET" "$REMOTE_CHECK" || true)"
RUNNING_STATE="$(echo "$FINAL_CHECK" | sed -n 's/^state=//p')"
RUNNING_VERSION="$(echo "$FINAL_CHECK" | sed -n 's/^version=//p' | tr -d '[:space:]')"
FAILURES=()
if [ -z "$FINAL_CHECK" ]; then
  FAILURES+=("Couldn't run the final check on $TARGET over ssh.")
else
  [ "$RUNNING_STATE" = "active" ] || FAILURES+=("The queue3d service is '${RUNNING_STATE:-unknown}', not 'active'.")
  if [ -z "$RUNNING_VERSION" ]; then
    FAILURES+=("https://127.0.0.1/login on the Pi didn't answer with a queue3d version.")
  elif [ "$RUNNING_VERSION" != "$EXPECTED_VERSION" ]; then
    FAILURES+=("The Pi is serving queue3d $RUNNING_VERSION, but this checkout is $EXPECTED_VERSION.")
  fi
fi
if [ "${#FAILURES[@]}" -gt 0 ]; then
  show_fail "${FAILURES[@]}"
  exit 1
fi
show_success
