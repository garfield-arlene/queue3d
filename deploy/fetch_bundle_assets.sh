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

# dnsmasq (arm64) plus its full recursive dependency closure, for
# remote_install.sh's own offline `apt-get install ./*.deb` - real
# incident, not a hypothetical precaution: an earlier version of that
# script apt-get installed dnsmasq live, on the Pi itself, which failed
# outright the moment this actually ran on the real, permanently-offline
# deployment network (see remote_install.sh's own comment on this list
# for the fuller account). Every *other* OS package this project
# installs (nginx, python3-venv) genuinely only needs internet once,
# during a Pi's very first setup while it's still on a network with
# internet - dnsmasq was added long after that phase already happened
# for the real Pi this deploys to, so that assumption was simply wrong
# for it specifically. Any *future* new OS package dependency needs the
# same treatment as this, not the nginx/python3-venv one - stage it
# here, install from the stage in remote_install.sh, never apt-get
# install a new package live on the Pi.
#
# Resolved once (not re-derived by this script) via a throwaway
# debian:trixie container, in two parts: `apt-cache depends dnsmasq`
# for dnsmasq's own *direct* Depends (netbase, dnsmasq-base,
# runit-helper - deliberately not dnsmasq's Recommends, matching
# remote_install.sh's own --no-install-recommends; a first pass without
# that flag pulled in a real but unnecessary dbus/libapparmor1/
# libexpat1/dns-root-data/adduser cluster that turned out to come
# entirely from Recommends, not anything dnsmasq or its Depends
# actually require - confirmed by checking netbase/runit-helper's own
# `apt-cache depends` output, which shows neither has any dependency of
# its own at all), then `dpkg --add-architecture arm64 && apt-get
# update && apt-get download dnsmasq netbase runit-helper $(apt-cache
# depends --recurse --no-recommends --no-suggests --no-conflicts
# --no-breaks --no-replaces --no-enhances dnsmasq-base:arm64 | grep
# '^\w' | grep ':arm64$' | sort -u)` for the rest - dnsmasq/netbase/
# runit-helper are all Architecture: all (identical file regardless of
# arch), dnsmasq-base is the real arm64 binary, every other entry is a
# transitive runtime dependency of dnsmasq-base specifically (filtered
# to :arm64 - the same recursive walk over the bare "dnsmasq" target
# pulls in a parallel amd64 chain too, which would be the wrong
# architecture to ship). Verified complete by actually installing this
# exact set with a real, blocked-network `apt-get install
# --no-install-recommends ./*.deb` in a throwaway container and
# confirming dnsmasq ends up genuinely "install ok installed" with its
# systemd unit present on disk - not just that dependency resolution
# looked plausible. Re-derive this list the same way if dnsmasq/the
# Pi's Debian release ever changes - these exact versions won't stay
# current forever, the same caveat PY_VERSION/PY_ABI/ORCASLICER_VERSION
# above already carry.
DEB_MIRROR="http://deb.debian.org/debian"
DNSMASQ_DEBS=(
  pool/main/a/acl/libacl1_2.3.2-2%2bb1_arm64.deb
  pool/main/a/attr/libattr1_2.5.2-3_arm64.deb
  pool/main/a/audit/libaudit1_4.0.2-2%2bdeb13u1_arm64.deb
  pool/main/b/base-passwd/base-passwd_3.6.7_arm64.deb
  pool/main/b/bzip2/libbz2-1.0_1.0.8-6_arm64.deb
  pool/main/c/cdebconf/cdebconf_0.280_arm64.deb
  pool/main/c/cdebconf/libdebconfclient0_0.280_arm64.deb
  pool/main/d/db5.3/libdb5.3t64_5.3.28%2bdfsg2-9_arm64.deb
  pool/main/d/dbus/libdbus-1-3_1.16.2-2_arm64.deb
  pool/main/d/dh-runit/runit-helper_2.16.4_all.deb
  pool/main/d/dnsmasq/dnsmasq_2.91-1%2bdeb13u2_all.deb
  pool/main/d/dnsmasq/dnsmasq-base_2.91-1%2bdeb13u2_arm64.deb
  pool/main/g/gcc-14/gcc-14-base_14.2.0-19_arm64.deb
  pool/main/g/gcc-14/libgcc-s1_14.2.0-19_arm64.deb
  pool/main/g/glibc/libc6_2.41-12%2bdeb13u4_arm64.deb
  pool/main/g/gmp/libgmp10_6.3.0%2bdfsg-3_arm64.deb
  pool/main/i/iptables/libxtables12_1.8.11-2_arm64.deb
  pool/main/j/jansson/libjansson4_2.14-2%2bb3_arm64.deb
  pool/main/libb/libbsd/libbsd0_0.12.2-2_arm64.deb
  pool/main/libc/libcap2/libcap2_2.75-10%2bdeb13u1%2bb3_arm64.deb
  pool/main/libc/libcap-ng/libcap-ng0_0.8.5-4%2bb1_arm64.deb
  pool/main/libd/libdebian-installer/libdebian-installer4_0.125_arm64.deb
  pool/main/libi/libidn2/libidn2-0_2.3.8-2_arm64.deb
  pool/main/libm/libmd/libmd0_1.1.0-2%2bb1_arm64.deb
  pool/main/libm/libmnl/libmnl0_1.0.5-3_arm64.deb
  pool/main/libn/libnetfilter-conntrack/libnetfilter-conntrack3_1.1.0-1_arm64.deb
  pool/main/libn/libnfnetlink/libnfnetlink0_1.0.2-3_arm64.deb
  pool/main/libn/libnftnl/libnftnl11_1.2.9-1_arm64.deb
  pool/main/libs/libselinux/libselinux1_3.8.1-1_arm64.deb
  pool/main/libs/libsemanage/libsemanage2_3.8.1-1_arm64.deb
  pool/main/libs/libsepol/libsepol2_3.8.1-1_arm64.deb
  pool/main/libt/libtextwrap/libtextwrap1_0.1-17%2bb1_arm64.deb
  pool/main/libu/libunistring/libunistring5_1.3-2_arm64.deb
  pool/main/libx/libxcrypt/libcrypt1_4.4.38-1_arm64.deb
  pool/main/n/ncurses/libtinfo6_6.5%2b20250216-2_arm64.deb
  pool/main/n/netbase/netbase_6.5_all.deb
  pool/main/n/nettle/libhogweed6t64_3.10.1-1_arm64.deb
  pool/main/n/nettle/libnettle8t64_3.10.1-1_arm64.deb
  pool/main/n/newt/libnewt0.52_0.52.25-1_arm64.deb
  pool/main/n/nftables/libnftables1_1.1.3-1_arm64.deb
  pool/main/p/pam/libpam0g_1.7.0-5_arm64.deb
  pool/main/p/pam/libpam-modules_1.7.0-5_arm64.deb
  pool/main/p/pam/libpam-modules-bin_1.7.0-5_arm64.deb
  pool/main/p/pcre2/libpcre2-8-0_10.46-1%7edeb13u2_arm64.deb
  pool/main/r/readline/libreadline8t64_8.2-6_arm64.deb
  pool/main/s/shadow/passwd_4.17.4-2_arm64.deb
  pool/main/s/slang2/libslang2_2.3.3-5%2bb2_arm64.deb
  pool/main/s/systemd/libsystemd0_257.13-1%7edeb13u1_arm64.deb
)
mkdir -p cache/debs
echo "Fetching dnsmasq (arm64) + its ${#DNSMASQ_DEBS[@]} dependencies for offline install on the Pi..."
for path in "${DNSMASQ_DEBS[@]}"; do
  fname="$(python3 -c "import sys, urllib.parse; print(urllib.parse.unquote(sys.argv[1]))" "$(basename "$path")")"
  dest="cache/debs/$fname"
  if [ -f "$dest" ]; then
    continue
  fi
  curl -sL -o "$dest" "$DEB_MIRROR/$path"
done

echo "Done. deploy/cache/ is ready for deploy.sh."
