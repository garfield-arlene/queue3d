# Deploying queue3d to the Raspberry Pi

Everything here assumes the target from project memory: a Raspberry Pi
4B running Raspberry Pi OS Lite, 64-bit (required, not a preference -
the vendored OrcaSlicer build only ships aarch64 Linux binaries). Initial
setup happens here, at home, with internet - the whole point of this
directory is that **after** that one-time setup, the Pi never needs
internet access again, matching the actual school deployment (an
isolated "island" LAN with zero internet access - see project memory
`queue3d-deployment-network`).

## Why this shape

Two things about the deployment target drove every decision here:

1. **"I don't want anyone to login to the Pi except me."** No shared
   root login, no password authentication, no second human account -
   just your own key-authenticated account with sudo, plus a dedicated
   `queue3d` system account (created by `remote_install.sh`) that runs
   the actual service and can never itself be used to log in at all (no
   shell, no password).
2. **Zero internet at the deployment site.** Every Python dependency and
   the OrcaSlicer binary the app needs get fetched *here*, ahead of
   time, and bundled into the deploy - the Pi's own `pip`/`apt` never
   run against the real internet. Ongoing updates go over the local
   network from your own machine (SSH + rsync), the same access you
   already need for systemd/service management - not a new surface.

## One-time initial setup (do this here, with internet, before the Pi
## ever reaches its actual deployment location)

### 1. First boot and SSH access

If you used Raspberry Pi Imager's advanced options (the gear icon /
Ctrl+Shift+X before writing) to set a hostname, enable SSH with your own
public key, and configure WiFi, the Pi should come up reachable over SSH
on first boot with no keyboard/monitor needed at all. If you didn't set
those options during flashing, the simplest fix is to re-flash with them
set - much less fiddly than configuring a headless Pi after the fact.

Verify you can reach it:

```bash
ssh <your-user>@<hostname-or-ip>.local
```

Worth setting up a `~/.ssh/config` entry on your own machine at this
point too, rather than typing `user@host` every time from here on:

```
Host q3d
    HostName q3d.local
    User <your-user>
    IdentityFile ~/.ssh/<your-key>
```

Every command below (including `deploy.sh <target>`) just takes
whatever you pass as the SSH target verbatim - with this in place, that
means every one of them can just be `q3d`, with the actual user and key
resolved from the config file instead of typed out each time.

### 2. Lock SSH down to just your own key

On the Pi (`/etc/ssh/sshd_config`, or a drop-in under
`/etc/ssh/sshd_config.d/`):

```
PasswordAuthentication no
PermitRootLogin no
```

Then `sudo systemctl restart ssh`. Confirm you can still log in with
your key in a **second** terminal before closing the first - if
something's wrong with the key setup, you want a still-open session to
fix it from, not a locked-out Pi.

If the image's default account still has a password set, either disable
it (`sudo passwd -l <username>`) or delete the account entirely once
your own key-only account is confirmed working - the requirement is "no
password auth works for anyone," not just "you have a key."

### 3. Install `rsync` on the Pi (needs internet)

```bash
sudo apt update
sudo apt install -y rsync
```

`openssh-server` is normally already present/enabled on a Pi Imager
image with SSH turned on; `python3` itself ships with Raspberry Pi OS
Lite by default. `rsync` has to be a manual, separate step because
`deploy.sh` needs it already present on the Pi just to sync files there
at all in step 5 below - nothing later in the process can bootstrap it.

`nginx` (the reverse proxy that makes the app reachable on a plain
`https://<host>/` with no port number - see step 6), `openssl` (which
generates the self-signed TLS cert nginx serves, since there's no CA
reachable at the deployment site to get a real one from), and
`python3-venv` are all installed automatically by `remote_install.sh`
itself in step 5, on demand, over whatever internet the Pi has at the
time - no separate manual step needed for those. That only works right
now, during this initial setup at home; once deployed to a genuinely
offline site, `remote_install.sh` will fail clearly if it ever needs one
of these and can't reach the internet to get it (it never needs to, in
practice, once they're already installed here first).

Nothing else here needs installing from the internet by hand - every
*application* dependency (Python packages, OrcaSlicer) is fetched on
your own machine instead and bundled through `deploy.sh` below,
deliberately, since the real deployment site never has internet for
those at all.

### 4. Build the deploy bundle (on this machine, or your Mac - wherever
### you actually have internet)

```bash
cd deploy
./fetch_bundle_assets.sh
```

Populates `deploy/cache/` with aarch64 Python wheels for every
dependency in `app/requirements.txt` and the matching aarch64 OrcaSlicer
AppImage - both verified for real while building this, not assumed: a
real cross-platform `pip download` targeting linux aarch64 + Python
3.13 (confirmed via `python3 --version` on the real Pi - Raspberry Pi OS
Lite had moved to a Debian 13 "Trixie" base, Python 3.13.5, by the time
it was actually imaged, one major version past the Debian 12/Python
3.11 this was first built against; re-verified cleanly against 3.13
rather than assumed to still work) resolved every single dependency
with a genuine prebuilt wheel (no source builds needed, ~13MB total),
and the aarch64 OrcaSlicer AppImage was downloaded and extracted
cleanly (confirmed `ARM aarch64` ELF, correct `AppRun`/`bin`/`lib`
layout matching the x86_64 build). Neither could be executed to confirm
full runtime behavior from this x86_64
machine - that still needs the real Pi. Re-run this whenever
`requirements.txt` or the pinned OrcaSlicer version changes; otherwise
the cache persists between
deploys and there's nothing to re-fetch.

### 5. Deploy

```bash
./deploy.sh <hostname-or-ip>.local
```

(or `./deploy.sh q3d`, or whatever `Host` alias you gave it in
`~/.ssh/config` per step 1 - `deploy.sh` just passes this straight
through to `ssh`/`rsync`, so anything they'd accept works here too.)

Syncs the app, bundled wheels, and OrcaSlicer to the Pi, then runs
`remote_install.sh` there via `sudo` - creates the `queue3d` system
user and venv if this is the first run, installs/upgrades Python
dependencies from the bundled wheels only (no network), extracts
OrcaSlicer, installs and enables the systemd unit, generates a
self-signed TLS cert (first run only - see below), configures nginx as
an https reverse proxy in front of it, and (re)starts both services.
The exact same command is both "install" and "upgrade" - every step is
idempotent, so there's no separate first-time mode to remember (see
`remote_install.sh`'s own comments for why each step is safe to
re-run).

The app itself (`queue3d.service`) only ever listens on `127.0.0.1:8000`
- never directly reachable from the network at all. `nginx` is the
actual public-facing listener, terminating TLS with a self-signed cert
(generated once on the Pi itself and left alone on every later deploy,
so it doesn't force everyone to re-click-through a "not trusted"
warning on every upgrade) and proxying to the app (see
`nginx-queue3d.conf`) - so everyone on the deployment network reaches
this at `https://<host>/`, no port number to remember, and the app
itself has one less thing directly exposed to the network regardless.
Plain port 80 just redirects to https, for anyone who types a bare
`http://` URL.

Since there's no CA reachable at the deployment site (zero internet, by
design), the cert is self-signed - every browser will show a "not
secure" / "not trusted" warning the first time it visits, on every
device, with no way around that short of manually installing the cert
as trusted on each one. That's inherent to a self-signed cert on an
otherwise-offline network, not a bug here. Firefox's version of this
warning has an obvious "Accept the Risk and Continue" button; Chrome
buries the same option one level deeper - click **Advanced**, then
**Proceed to `<host>` (unsafe)** underneath it. Confirmed working this
way on an ordinary, unmanaged Chrome install.

One real caveat worth confirming before this becomes the primary way
students/staff reach the app: on a **managed** Chrome install (e.g.
school-issued Chromebooks under a Google Workspace for Education admin
console), that "Proceed anyway" option can be disabled entirely by
district policy - if so, there is no client-side click-through at all,
on any page, ever, and the fix has to happen elsewhere (a real
CA-signed cert for a domain you actually own, with local DNS set up to
resolve it on the island network - a bigger lift, worth revisiting only
if this turns out to actually be the situation on the real deployment
devices).

**If Chrome (specifically, and only Chrome) shows `ERR_ADDRESS_UNREACHABLE`
instead of the cert warning above** - this isn't a queue3d, nginx, or
cert problem at all, even though it looks like one. On a Mac, it's
almost always macOS's own **Local Network privacy permission**
(Apple menu -> System Settings -> Privacy & Security -> Local Network):
apps have to be individually granted permission to connect to devices
on your local subnet, tied specifically to resolving/connecting via
mDNS (`.local` names) - and unlike Firefox, Chrome doesn't reliably
prompt for it. Diagnosed for real, not guessed: a `chrome://net-export/`
capture showed DNS resolution succeeding correctly
(`q3d.local` -> `192.168.1.x`), then the actual TCP connect immediately
failing with `os_error 65` (`EHOSTUNREACH`) - the kernel itself refusing
the connection for that app, not a network or server problem. Fix:
find Chrome in that Local Network list (it may appear more than once -
harmless, both are Chrome-related) and enable it, fully quit and
relaunch Chrome, and retry. If toggling it doesn't visibly take effect,
`tccutil reset LocalNetwork` from Terminal resets it for every app and
forces a fresh permission prompt next time each one tries. This is a
macOS-specific gatekeeper with no ChromeOS equivalent (Chrome effectively
*is* the OS on a Chromebook, not a sandboxed app requesting permission
from a layer above it), so it should not recur on the real deployment
devices.

### 6. Confirm it's actually running

```bash
ssh <hostname-or-ip>.local sudo systemctl status queue3d nginx
curl -k https://<hostname-or-ip>.local/login
```

(`-k` skips certificate validation - expected here, since the cert is
self-signed and there's no CA for curl to check it against either.) In
a real browser, click through the "not secure" warning once per device
- that's expected, not a sign anything is actually wrong.

Only now, once this all checks out, move the Pi to its real deployment
location and connect it to the isolated LAN instead of your home
network. From this point on, every future update is just steps 4-5
again, run from wherever your laptop happens to be *on the same LAN as
the Pi* - the Pi itself never needs internet again.

## Ongoing updates

Once deployed, upgrading to a newer version of the app is exactly step
4 (if `requirements.txt`/OrcaSlicer changed) followed by step 5 - `git
pull` (or however you get the newer code onto your laptop) on your own
machine, then `./deploy.sh <target>` again from the `deploy/` directory.
Nothing on the Pi side changes about this process whether it's the
first deploy or the fiftieth.

## If an upgrade goes wrong: rolling back

Per the user, asked directly before ever relying on this for a real
upgrade: every upgrade (not a fresh install - there's nothing to back up
yet on the first one) automatically backs up the previous code and
database to `/opt/queue3d-backups/<timestamp>/` *before* touching
anything, and checks that the service actually comes back up and
responds afterward. If it doesn't, `remote_install.sh` stops and tells
you plainly rather than leaving you to discover it later - it
deliberately does **not** try to roll back automatically (an unattended
automatic restore has its own real failure modes; the backup exists so
a person can make that call with the actual situation in front of them).

To actually roll back by hand, pick the backup you want (they're named
by UTC timestamp, most recent last) and, on the Pi:

```bash
sudo systemctl stop queue3d
BACKUP=/opt/queue3d-backups/<the-timestamp-you-want>
sudo rsync -a --delete "$BACKUP/app/" /opt/queue3d/app/ --exclude data --exclude .venv
sudo rsync -a --delete "$BACKUP/slicing/" /opt/queue3d/slicing/ --exclude tools
sudo cp "$BACKUP/db/queue3d.db" /opt/queue3d/app/data/queue3d.db
sudo chown -R queue3d:queue3d /opt/queue3d
sudo systemctl start queue3d
sudo systemctl status queue3d
```

Note this restores the **database** to exactly how it was right before
that upgrade too, not just the code - any jobs submitted/changed between
that backup and now would be lost. That's an inherent tradeoff of
restoring a consistent snapshot, not a bug: the alternative (roll back
code but keep the newer database) risks the old code not understanding
data the new code already wrote. The last 5 pre-upgrade backups are kept
automatically (oldest pruned first) so this option stays available
without accumulating unboundedly on the Pi's limited storage.

## What's NOT handled here yet

- **The regular, ongoing disaster-recovery backup and draft-expiry cron
  jobs** (`app/backup.py`, `app/cleanup_drafts.py`) - a different thing
  from the pre-upgrade snapshots above: this is the daily backup to the
  two rotating external USB drives, protecting against real data loss
  (a failed SD card, say), not just a bad upgrade. Still needs its own
  cron entries installed on the Pi - not wired into `remote_install.sh`
  yet. See each script's own module docstring for the exact crontab line.
- **A fixed IP or mDNS hostname** so `<hostname>.local` actually
  resolves reliably on the deployment network - still a README.md
  to-do item, not yet set up.
- **Printer pairing** (`QUEUE3D_PRINTER_HOST`/`QUEUE3D_PRINTER_PORT`
  env vars, if the Pi's network setup needs them different from the
  app's own defaults) - not yet wired into the systemd unit as an
  `EnvironmentFile`; add one there if the real deployment needs
  non-default values.
