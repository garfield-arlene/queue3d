#!/usr/bin/env python3
"""Resolve and download the full .deb dependency closure for a list of
Debian packages, for offline install on the Pi. Run by
fetch_bundle_assets.sh (on a machine WITH internet - the Mac, or the
dev machine), never on the Pi itself.

Exists for the OrcaSlicer AppImage's host libraries (GTK3, WebKitGTK
4.1, GStreamer, the OpenGL/Mesa stack - see fetch_bundle_assets.sh for
the list and how it was derived): a closure of about 300
packages, far too many to hand-maintain as a fixed URL list the way
dnsmasq's 48 .debs are, and one that shifts every time Debian or
Raspberry Pi publish an update.

Resolves against the same archives a stock Raspberry Pi OS (trixie,
arm64) install is configured with - Debian's trixie, trixie-updates and
trixie-security, plus archive.raspberrypi.com's own trixie - picking the
highest version of each package across all of them, the same choice
apt itself makes. Debian alone isn't enough: Raspberry Pi's archive
ships its own newer builds of Mesa and GTK, and a closure resolved only
against Debian's would collide with those on the real Pi.

The whole closure is downloaded, base libraries (libc6, ...) included,
whether or not the Pi already has them - <dest-dir> also gets a
Packages/Release index, making it a small local apt repository, and a
roots.txt naming the packages asked for.
remote_install.sh points apt at only that repository and lets apt itself
work out the minimal change: install what's missing, upgrade an
installed package only when something being installed actually needs a
newer version, and leave everything else on the Pi alone. Handing apt
every .deb by filename instead would force-upgrade each one, dragging
version-locked siblings (systemd alongside libsystemd0, say) into a
conflict there's no network to resolve.

Stdlib only, so it runs on the Mac's own python3 with nothing installed.

Usage: python3 resolve_debs.py [--arch arm64] <dest-dir> <package> [<package> ...]
"""

import email.utils
import gzip
import hashlib
import lzma
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ARCH = "arm64"  # the Pi's; --arch exists only for testing on another machine
# (base URL, suite, component) - matches Raspberry Pi OS trixie's own
# /etc/apt/sources.list.d entries.
ARCHIVES = [
    ("http://deb.debian.org/debian", "trixie", "main"),
    ("http://deb.debian.org/debian", "trixie-updates", "main"),
    ("http://security.debian.org/debian-security", "trixie-security", "main"),
    ("http://archive.raspberrypi.com/debian", "trixie", "main"),
]


def _fetch_index(base, suite, component):
    """Packages index for one archive, decompressed. Not every archive
    publishes every compression (archive.raspberrypi.com has no .xz), so
    try each in turn. trixie-updates' index can legitimately be empty."""
    stem = f"{base}/dists/{suite}/{component}/binary-{ARCH}/Packages"
    for ext, decompress in ((".xz", lzma.decompress), (".gz", gzip.decompress)):
        try:
            with urllib.request.urlopen(stem + ext, timeout=60) as resp:  # nosec B310 - fixed archive URLs above
                return decompress(resp.read()).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise
    raise RuntimeError(f"No Packages index found at {stem}(.xz|.gz)")


def _parse_index(text, base):
    for stanza in text.split("\n\n"):
        fields = {"_raw": stanza.strip("\n")}
        key = None
        for line in stanza.splitlines():
            if line.startswith((" ", "\t")):
                continue  # continuation (long Description) - never needed here
            key, _, value = line.partition(":")
            fields[key] = value.strip()
        if "Package" in fields and "Filename" in fields:
            fields["_base"] = base
            yield fields


# ---- dpkg version comparison (deb-version(7)) ----

def _order(c):
    if c == "~":
        return -1
    if c.isdigit():
        return 0
    if c.isalpha():
        return ord(c)
    return ord(c) + 256


def _compare_part(a, b):
    """dpkg's verrevcmp: alternating non-digit/digit runs, where ~ sorts
    before everything (even the end of the string)."""
    i = j = 0
    while i < len(a) or j < len(b):
        first_diff = 0
        while (i < len(a) and not a[i].isdigit()) or (j < len(b) and not b[j].isdigit()):
            ac = _order(a[i]) if i < len(a) else 0
            bc = _order(b[j]) if j < len(b) else 0
            if ac != bc:
                return ac - bc
            i += 1
            j += 1
        while i < len(a) and a[i] == "0":
            i += 1
        while j < len(b) and b[j] == "0":
            j += 1
        while i < len(a) and a[i].isdigit() and j < len(b) and b[j].isdigit():
            if not first_diff:
                first_diff = ord(a[i]) - ord(b[j])
            i += 1
            j += 1
        if i < len(a) and a[i].isdigit():
            return 1
        if j < len(b) and b[j].isdigit():
            return -1
        if first_diff:
            return first_diff
    return 0


def _split_version(v):
    epoch, _, rest = v.rpartition(":") if ":" in v else ("0", "", v)
    upstream, _, revision = rest.rpartition("-") if "-" in rest else (rest, "", "")
    return int(epoch or 0), upstream, revision


def compare_versions(a, b):
    ea, ua, ra = _split_version(a)
    eb, ub, rb = _split_version(b)
    if ea != eb:
        return ea - eb
    return _compare_part(ua, ub) or _compare_part(ra, rb)


def _satisfies(version, op, wanted):
    c = compare_versions(version, wanted)
    return {"<<": c < 0, "<=": c <= 0, "=": c == 0, ">=": c >= 0, ">>": c > 0}[op]


# ---- resolution ----

_DEP_RE = re.compile(r"^([a-z0-9][a-z0-9+.-]*)(?::[a-z0-9-]+)?\s*(?:\(\s*(<<|<=|=|>=|>>)\s*([^)\s]+)\s*\))?")


def _parse_deps(value):
    """"a (>= 1), b | c" -> [[("a", ">=", "1")], [("b", None, None), ("c", None, None)]]"""
    groups = []
    for group in value.split(","):
        alts = []
        for alt in group.split("|"):
            m = _DEP_RE.match(alt.strip())
            if m:
                alts.append(m.groups())
        if alts:
            groups.append(alts)
    return groups


def resolve(roots, packages, providers):
    selected = {}
    problems = []
    todo = list(roots)
    while todo:
        name = todo.pop()
        if name in selected:
            continue
        pkg = packages.get(name)
        if pkg is None:
            if name in providers:
                todo.append(providers[name])
            else:
                problems.append(f"no package or provider for {name}")
            continue
        selected[name] = pkg
        for field in ("Pre-Depends", "Depends"):
            for alts in _parse_deps(pkg.get(field, "")):
                # Prefer an alternative that's already part of the
                # closure; else the first one that exists at all - apt's
                # own first-alternative rule.
                chosen = next((a for a in alts if a[0] in selected), None)
                if chosen is None:
                    chosen = next((a for a in alts if a[0] in packages or a[0] in providers), alts[0])
                todo.append(chosen[0])
    # Version constraints, checked once the whole set is known.
    for name, pkg in selected.items():
        for field in ("Pre-Depends", "Depends"):
            for alts in _parse_deps(pkg.get(field, "")):
                if not any(a[0] in selected for a in alts):
                    continue
                ok = any(
                    a[0] in selected and (a[1] is None or _satisfies(selected[a[0]]["Version"], a[1], a[2]))
                    for a in alts
                )
                if not ok:
                    want = " | ".join(f"{a[0]} ({a[1]} {a[2]})" if a[1] else a[0] for a in alts)
                    have = ", ".join(f"{a[0]} {selected[a[0]]['Version']}" for a in alts if a[0] in selected)
                    problems.append(f"{name} {pkg['Version']} needs {want}, but the closure has {have}")
    return selected, problems


def _write_repo_index(dest, wanted):
    """Packages + Release, so apt can use <dest> as a flat repository
    (`deb [trusted=yes] file:<dest> ./`). Each stanza is the archive's
    own, verbatim, with only Filename pointed at the local copy."""
    stanzas = []
    for filename, pkg in sorted(wanted.items()):
        stanzas.append(re.sub(r"^Filename: .*$", f"Filename: ./{filename}", pkg["_raw"], flags=re.M))
    index = ("\n\n".join(stanzas) + "\n").encode()
    (dest / "Packages").write_bytes(index)
    (dest / "Release").write_text(
        "Origin: queue3d\nLabel: queue3d\n"
        f"Date: {email.utils.formatdate(usegmt=True)}\n"
        f"Architectures: {ARCH} all\n"
        "SHA256:\n"
        f" {hashlib.sha256(index).hexdigest()} {len(index)} Packages\n"
    )


def main():
    global ARCH
    args = sys.argv[1:]
    if args[:1] == ["--arch"]:
        ARCH = args[1]
        args = args[2:]
    if len(args) < 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        sys.exit(1)
    dest = Path(args[0])
    roots = args[1:]

    packages = {}
    providers = {}
    for base, suite, component in ARCHIVES:
        print(f"  Reading {base} {suite}/{component} ({ARCH})...")
        for fields in _parse_index(_fetch_index(base, suite, component), base):
            name = fields["Package"]
            if name not in packages or compare_versions(fields["Version"], packages[name]["Version"]) > 0:
                packages[name] = fields
            for prov in _parse_deps(fields.get("Provides", "")):
                providers.setdefault(prov[0][0], name)

    selected, problems = resolve(roots, packages, providers)
    if problems:
        print("Couldn't resolve a consistent set of packages:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        sys.exit(1)

    dest.mkdir(parents=True, exist_ok=True)
    wanted = {}
    for name, pkg in selected.items():
        filename = Path(pkg["Filename"]).name
        wanted[filename] = pkg
    # Anything left over from an earlier resolution goes - two versions of
    # the same package handed to one apt-get install is an error.
    for stale in dest.glob("*.deb"):
        if stale.name not in wanted:
            stale.unlink()

    total = sum(int(p["Size"]) for p in wanted.values())
    print(f"  {len(wanted)} packages, {total / 1e6:.0f} MB")
    fetched = 0
    for filename, pkg in sorted(wanted.items()):
        path = dest / filename
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == pkg["SHA256"]:
            continue
        url = f"{pkg['_base']}/{pkg['Filename']}"
        with urllib.request.urlopen(url, timeout=120) as resp:  # nosec B310 - URL built from the fixed archives above
            data = resp.read()
        if hashlib.sha256(data).hexdigest() != pkg["SHA256"]:
            raise RuntimeError(f"SHA256 mismatch for {url}")
        path.write_bytes(data)
        fetched += 1
    _write_repo_index(dest, wanted)
    # What remote_install.sh actually asks apt to install - the closure
    # above is only what apt may draw on to satisfy these.
    (dest / "roots.txt").write_text("\n".join(roots) + "\n")
    print(f"  Downloaded {fetched}, {len(wanted) - fetched} already cached.")


if __name__ == "__main__":
    main()
