#!/bin/bash
# Run this on your Mac before ./deploy.sh, whenever you're about to deploy
# or the TLS cert is due for renewal (every ~90 days - see deploy/README.md's
# "TLS certificate" section). Does the two things you'd otherwise do by
# hand each time: pulls the latest code, and fetches a fresh copy of the
# certificate from wherever certbot actually issues it. deploy.sh itself
# already handles the other half - copying cache/tls/*.pem onto the Pi and
# installing them there on every run - so after this, ./deploy.sh <host> is
# the whole rest of the process.
#
# Needs: SSH access to the machine that runs certbot for this deployment's
# domain (a home server, not this Mac and not the Pi - see deploy/README.md
# for which one, and why it can't be either of those) - your own key added
# to its authorized_keys, the same way it already is for the Pi, connecting
# as an account that can already read both files directly. No sudo, no
# elevation, no fallback of any kind - per the user, whose account is set
# up with exactly the access this needs already: this script trusts that
# and just copies the files.
#
# Only two things this needs to know, and only two arguments it takes -
# the cert host, and the one directory on it holding both fullchain.pem
# and privkey.pem together (certbot's own standard layout, and per the
# user, the actual layout here too). Deliberately neither is hardcoded
# anywhere below, even as a default - both are only ever passed in at
# the command line, per the user, so neither ends up committed to this
# (public) repo.
#
# Usage: ./prep_deploy.sh <cert-host> <remote-cert-dir>
# Example: ./prep_deploy.sh certadmin@your-home-server /etc/letsencrypt/live/your-domain
# <cert-host> works the same as ./deploy.sh's own <host> argument - a
# bare hostname, user@host, or a ~/.ssh/config Host alias all work,
# since this is passed straight through to ssh/scp exactly as typed.
set -euo pipefail
cd "$(dirname "$0")"

if [ $# -ne 2 ]; then
  echo "Usage: $0 <cert-host> <remote-cert-dir>" >&2
  echo "Example: $0 certadmin@your-home-server /path/to/cert/dir" >&2
  exit 1
fi
CERT_HOST="$1"
REMOTE_CERT_DIR="$2"

# This script lives in deploy/, but git itself only cares that we're
# somewhere inside the repo - one level up either way. Checked explicitly
# (not just letting `git pull` fail with its own generic error) so a
# clone that somehow lost its .git directory, or this script accidentally
# copied somewhere else entirely, says so plainly rather than failing on
# some less obvious later step instead.
cd ..
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "$(pwd) isn't a git repository - can't git pull. Is this a real clone of queue3d?" >&2
  exit 1
fi

# Fail loudly rather than pull over uncommitted local changes - this is a
# deploy machine, not a place real work-in-progress should be sitting
# uncommitted anyway, so this should never actually trigger in normal use;
# if it does, something unexpected is going on and silently merging over
# it would be the wrong call.
if [ -n "$(git status --porcelain)" ]; then
  echo "This checkout has uncommitted changes - refusing to git pull over them." >&2
  echo "Commit, stash, or discard them first, then re-run this script." >&2
  exit 1
fi

echo "Pulling the latest code ($(git branch --show-current))..."
# --ff-only, not a plain pull: this machine is only ever meant to deploy
# what's already been pushed elsewhere, never to hold its own local
# commits - a real divergence here means something unexpected happened
# (e.g. a commit made directly on this checkout by mistake), and creating
# a surprise merge commit to paper over that is worse than just stopping
# and saying so.
git pull --ff-only

cd deploy
mkdir -p cache/tls

echo "Fetching the TLS certificate from $CERT_HOST:$REMOTE_CERT_DIR ..."
# Plain scp, nothing else - no sudo fallback, no elevation, no retry.
# Trusts the account behind $CERT_HOST already has read access to both
# files directly, per the user.
scp "$CERT_HOST:$REMOTE_CERT_DIR/fullchain.pem" cache/tls/fullchain.pem
scp "$CERT_HOST:$REMOTE_CERT_DIR/privkey.pem" cache/tls/privkey.pem
chmod 600 cache/tls/privkey.pem

# Confirms the two files actually belong together, the same check used
# when these were first wired in by hand - a fetch that silently grabbed
# a stale/mismatched pair (e.g. mid-renewal on the source machine, or the
# wrong path typed above) would otherwise only surface much later, as a
# broken HTTPS setup on the Pi after ./deploy.sh has already run.
cert_modulus="$(openssl x509 -in cache/tls/fullchain.pem -noout -modulus | openssl md5)"
key_modulus="$(openssl rsa -in cache/tls/privkey.pem -noout -modulus 2>/dev/null | openssl md5)"
if [ "$cert_modulus" != "$key_modulus" ]; then
  echo "cache/tls/fullchain.pem and privkey.pem don't match each other -" >&2
  echo "something went wrong in the fetch above. Not safe to deploy with this pair." >&2
  exit 1
fi

echo "Certificate and key match. Valid dates:"
openssl x509 -in cache/tls/fullchain.pem -noout -dates

echo
echo "Done. deploy/cache/tls/ is ready - run ./deploy.sh <host> next."
