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
ssh pi@<hostname-or-ip>.local
```

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

### 3. Install OS-level prerequisites (needs internet - the one and only
### time this Pi should ever need it)

```bash
sudo apt update
sudo apt install -y rsync python3-venv
```

`openssh-server` is normally already present/enabled on a Pi Imager
image with SSH turned on; `python3` itself ships with Raspberry Pi OS
Lite by default. Nothing else here needs installing from the internet -
every *application* dependency (Python packages, OrcaSlicer) is fetched
on your own machine instead and bundled through `deploy.sh` below,
deliberately, so this is the only step this whole process ever asks the
Pi itself to reach the internet for.

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
./deploy.sh pi@<hostname-or-ip>.local
```

Syncs the app, bundled wheels, and OrcaSlicer to the Pi, then runs
`remote_install.sh` there via `sudo` - creates the `queue3d` system
user and venv if this is the first run, installs/upgrades Python
dependencies from the bundled wheels only (no network), extracts
OrcaSlicer, installs and enables the systemd unit, and (re)starts the
service. The exact same command is both "install" and "upgrade" - every
step is idempotent, so there's no separate first-time mode to remember
(see `remote_install.sh`'s own comments for why each step is safe to
re-run).

### 6. Confirm it's actually running

```bash
ssh pi@<hostname-or-ip>.local sudo systemctl status queue3d
curl http://<hostname-or-ip>.local:8000/login
```

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

## What's NOT handled here yet

- **Backups and draft-expiry cron jobs** (`app/backup.py`,
  `app/cleanup_drafts.py`) still need their own cron entries installed
  on the Pi - not wired into `remote_install.sh` yet. See each script's
  own module docstring for the exact crontab line.
- **A fixed IP or mDNS hostname** so `<hostname>.local` actually
  resolves reliably on the deployment network - still a README.md
  to-do item, not yet set up.
- **Printer pairing** (`QUEUE3D_PRINTER_HOST`/`QUEUE3D_PRINTER_PORT`
  env vars, if the Pi's network setup needs them different from the
  app's own defaults) - not yet wired into the systemd unit as an
  `EnvironmentFile`; add one there if the real deployment needs
  non-default values.
