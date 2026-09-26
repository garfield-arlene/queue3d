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
`https://<host>/` with no port number - see step 6) and `python3-venv`
are installed automatically by `remote_install.sh` itself in step 5, on
demand, over whatever internet the Pi has at the time - no separate
manual step needed for those. That only works right now, during this
initial setup at home; once deployed to a genuinely offline site,
`remote_install.sh` will fail clearly if it ever needs one of these and
can't reach the internet to get it (it never needs to, in practice, once
they're already installed here first).

Nothing else here needs installing from the internet by hand - every
*application* dependency (Python packages, OrcaSlicer) is fetched on
your own machine instead and bundled through `deploy.sh` below,
deliberately, since the real deployment site never has internet for
those at all.

### 4. Set up the three storage drives

Per the original storage plan (three USB flash drives, already owned,
no new hardware purchase needed): the microSD card boots the OS only:
one flash drive holds the live database and every job's files, the
other two rotate as daily backup targets (see `backup.py`'s own
docstring for why day-parity rotation between two always-plugged-in
drives, rather than a manually-swapped one). Plug in all three, then
identify them:

```bash
lsblk -o NAME,SIZE,MODEL,SERIAL,MOUNTPOINT
```

If they're all the same size, it genuinely doesn't matter which
physical drive gets which role - everything below mounts by the
filesystem's own UUID, not by device letter (`sda`/`sdb`/`sdc` can
shift across reboots/reconnects depending on enumeration order, but a
UUID is embedded in the filesystem itself at format time and travels
with the physical drive regardless of which port it's plugged into).
If they differ, give the largest one the live-data role - it's the one
that only ever grows over a school year, while the two backups just
mirror that same content on rotation.

**Formatting wipes whatever's currently on each drive - confirm you
don't need that data first.** Reusing each drive's existing single
partition (already spanning the whole disk) rather than repartitioning:

```bash
sudo mkfs.ext4 -F -L q3d-data /dev/sda1        # live data - adjust device letter as identified above
sudo mkfs.ext4 -F -L q3d-backup-a /dev/sdb1    # backup A
sudo mkfs.ext4 -F -L q3d-backup-b /dev/sdc1    # backup B
```

Get each partition's UUID and add persistent mounts by UUID, not
`/dev/sdX` - the `nofail` option matters specifically here since these
are removable USB drives, not permanent internal storage: without it, a
drive that's unplugged or slow to enumerate would hang or fail the
whole boot:

```bash
sudo blkid /dev/sda1 /dev/sdb1 /dev/sdc1
sudo mkdir -p /mnt/queue3d-data /mnt/queue3d-backup-a /mnt/queue3d-backup-b
```

Add one line per drive to `/etc/fstab` (substitute the real UUIDs from
`blkid` above):

```
UUID=<data-uuid>       /mnt/queue3d-data       ext4  defaults,nofail,noatime  0  2
UUID=<backup-a-uuid>   /mnt/queue3d-backup-a   ext4  defaults,nofail,noatime  0  2
UUID=<backup-b-uuid>   /mnt/queue3d-backup-b   ext4  defaults,nofail,noatime  0  2
```

Then mount and hand ownership to the `queue3d` service account (the
same dedicated no-login user everything else runs as - without this,
the app gets permission denied trying to write its own database):

```bash
sudo mount -a
sudo chown -R queue3d:queue3d /mnt/queue3d-data /mnt/queue3d-backup-a /mnt/queue3d-backup-b
```

`queue3d.service` (step 6 below) refuses to even start if
`/mnt/queue3d-data` isn't actually mounted (`RequiresMountsFor`,
backed up by the same check inside `db.py` itself) - deliberately, so a
drive that's unplugged or not yet mounted at boot can never look like a
successful start against a silently-empty, freshly-created local
directory instead of the real data. The first `remote_install.sh` run
after this is set up also migrates any existing local database onto
this drive automatically, one time only - nothing already submitted
gets lost by moving to this setup partway through.

### 5. Build the deploy bundle (on this machine, or your Mac - wherever
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

### 6. Deploy

```bash
./deploy.sh <hostname-or-ip>.local
```

(or `./deploy.sh q3d`, or whatever `Host` alias you gave it in
`~/.ssh/config` per step 1 - `deploy.sh` just passes this straight
through to `ssh`/`rsync`, so anything they'd accept works here too.)

Syncs the app, bundled wheels, OrcaSlicer, and the TLS certificate to
the Pi, then runs `remote_install.sh` there via `sudo` - creates the
`queue3d` system user and venv if this is the first run, installs/
upgrades Python dependencies from the bundled wheels only (no network),
extracts OrcaSlicer, installs and enables the systemd unit (and the
`queue3d-backup`/`queue3d-cleanup` timers - daily backup and
draft-expiry cleanup, actually wired in and running rather than left as
a "run manually, or wire into cron" note in each script's own
docstring), installs the TLS certificate (every run - see "TLS
certificate" below), configures nginx as an https reverse proxy in
front of it, and (re)starts everything. The exact same command is both
"install" and "upgrade" - every step is idempotent, so there's no
separate first-time mode to remember (see `remote_install.sh`'s own
comments for why each step is safe to re-run). Refuses to proceed at
all if step 4's live-data drive isn't actually mounted at
`/mnt/queue3d-data`, or if the TLS certificate isn't staged - check
those first if this exits early with either message.

The app itself (`queue3d.service`) only ever listens on `127.0.0.1:8000`
- never directly reachable from the network at all. `nginx` is the
actual public-facing listener, terminating TLS and proxying to the app
(see `nginx-queue3d.conf`) - so everyone on the deployment network
reaches this at `https://q3d.home.mygarfield.us/`, no port number to
remember, and the app itself has one less thing directly exposed to the
network regardless. Plain port 80 just redirects to https, for anyone
who types a bare `http://` URL.

### TLS certificate

**This used to be a self-signed certificate, generated once on the Pi
itself.** That's what the rest of this file described for a while, with
an open caveat right here: "on a **managed** Chrome install..., that
'Proceed anyway' option can be disabled entirely by district policy - if
so,... the fix has to happen elsewhere (a real CA-signed cert for a
domain you actually own, with local DNS set up to resolve it on the
island network)." Confirmed directly on a real school-issued Chromebook
on the actual island network: exactly that - a hard certificate warning
with no "Advanced" / "Proceed anyway" option at all, on any page, ever.
No amount of regenerating a self-signed cert can ever fix this; only a
certificate from a publicly-trusted CA does, so that's what this now
uses.

**Where the certificate comes from.** This deployment has no internet
access at all, by design - it can never run its own ACME client
(certbot or similar) to request or renew a certificate itself. Issued
instead on a machine that does have internet, for a real domain the
user already controls (a subdomain of an existing personal domain,
`q3d.home.mygarfield.us`), then copied here by hand:

```bash
# On the machine that ran certbot (a home server, in this case) -
# only fullchain.pem and privkey.pem are actually needed; cert.pem and
# chain.pem are subsets already folded into fullchain.pem.
scp /etc/letsencrypt/live/<domain>/fullchain.pem  you@this-machine:deploy/cache/tls/fullchain.pem
scp /etc/letsencrypt/live/<domain>/privkey.pem    you@this-machine:deploy/cache/tls/privkey.pem
```

`deploy/cache/tls/` is gitignored, same as `deploy/cache/wheels/` and
the OrcaSlicer AppImage - a real private key must never end up in this
(public) repo. `deploy.sh` refuses to proceed at all if either file is
missing there, rather than silently falling back to anything weaker.

**Renewal.** Let's Encrypt certificates are valid 90 days. There is no
automatic renewal path here - by the time this cert is due to expire,
get a fresh one the same way (wherever it was originally issued),
overwrite the two files in `deploy/cache/tls/`, and run `./deploy.sh
<host>` again; the certificate is reinstalled on *every* run (unlike the
self-signed one this replaced, which was deliberately generated once and
left alone), so this is the entire renewal process - no separate
"just update the cert" script or flag needed.

**DNS - the other half of this, and just as necessary.** A trusted
certificate only fixes whether a connection is *trusted* once a client
already reached it - it does nothing for whether `q3d.home.mygarfield.us`
actually *resolves* to this Pi's LAN IP in the first place, and the
island network has no route to the public DNS record for that name at
all (zero internet access, the same reason this deployment can't request
its own certificate). The router on the actual deployment network turned
out to have no usable DNS service of its own to add that record to
directly, so the Pi answers it instead: `remote_install.sh` installs and
configures `dnsmasq` on every run, resolving *only*
`q3d.home.mygarfield.us` (to the Pi's own current IP, detected fresh
each time - `no-resolv`, no upstream forwarding at all, since there's
nothing else worth resolving on a network with zero internet access
anyway). `nginx-queue3d.conf`'s `server_name` is set to that same
hostname to match.

That alone doesn't make Chromebooks *use* the Pi for DNS, though -
**one manual, router-specific step is still required**: log into the
router's own admin page and set its DHCP-advertised DNS server to the
Pi's LAN IP (exactly where "DNS server" or "DNS 1" lives varies by
router - look under DHCP or LAN settings). Once that's done, every
device that renews its DHCP lease on that network starts asking the Pi
for DNS, and `q3d.home.mygarfield.us` resolves correctly for all of
them.

**This deliberately replaces relying on the Pi's own mDNS/Avahi hostname
(`<host>.local`) for the real deployment network** - either isn't
reliable there or isn't part of what a locked-down Chromebook will
resolve/trust in the first place. mDNS is still exactly how every step
in *this* file reaches the Pi during initial at-home setup (see the
`.local` examples throughout) - `remote_install.sh` never disables it on
its own, since that would break this very setup process the moment it
first ran. Turning it off is instead the last manual step, done once,
right before the Pi actually leaves for the deployment site - see
"Before the move: disable mDNS" below.

**If Chrome (specifically, and only Chrome) shows `ERR_ADDRESS_UNREACHABLE`
while testing via `.local` from your own Mac during setup** - this isn't
a queue3d, nginx, or cert problem at all, even though it looks like one.
It's almost always macOS's own **Local Network privacy permission**
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

### 7. Confirm it's actually running

```bash
ssh <hostname-or-ip>.local sudo systemctl status queue3d nginx
ssh <hostname-or-ip>.local sudo systemctl list-timers queue3d-backup.timer queue3d-cleanup.timer
curl -k https://<hostname-or-ip>.local/login
```

(`-k` is still needed here even with a real, trusted certificate now -
it's issued for `q3d.home.mygarfield.us` specifically, not for whatever
`.local` mDNS name you're actually testing through at this stage, so
curl would otherwise refuse it on a hostname mismatch. That mismatch is
expected and fine during this at-home setup step; once the real DNS
override is in place at the deployment site and everyone reaches it by
the real hostname, this stops being an issue and a plain `curl
https://q3d.home.mygarfield.us/login` with no `-k` at all would succeed
cleanly.) The `list-timers` output shows when each is next scheduled to
run (3am/4am)
- worth a manual run of each once, too, rather than waiting until 3am to
find out if either has a problem:

```bash
ssh <hostname-or-ip>.local sudo systemctl start queue3d-backup.service
ssh <hostname-or-ip>.local sudo systemctl start queue3d-cleanup.service
ssh <hostname-or-ip>.local sudo journalctl -u queue3d-backup.service -u queue3d-cleanup.service -n 20 --no-pager
```

### Before the move: disable mDNS

Everything up to here, including every `.local` example in this file,
depends on the Pi's own mDNS/Avahi hostname - that's deliberate, and
`remote_install.sh` never touches it on its own, since disabling it
automatically would break this exact setup process the moment it first
ran. Once dnsmasq (above) is confirmed working and the router's DHCP
is pointed at it, mDNS has nothing left to do on the real deployment
network - turn it off as the actual last step here, not before:

```bash
ssh <hostname-or-ip>.local sudo systemctl disable --now avahi-daemon.service avahi-daemon.socket
```

After this, `<host>.local` stops resolving *at all* on this Pi - correct,
not a bug, and exactly the point (nothing should be relying on it once
the deployment network's own DNS is doing the real job). Reach the Pi
by its plain LAN IP or, once the router's DHCP change has taken effect,
`q3d.home.mygarfield.us` from here on, including for every future
`./deploy.sh` run.

Only now, once this all checks out, move the Pi to its real deployment
location and connect it to the isolated LAN instead of your home
network. From this point on, every future update is just steps 5-6
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
sudo cp "$BACKUP/db/queue3d.db" /mnt/queue3d-data/queue3d.db
sudo chown -R queue3d:queue3d /opt/queue3d /mnt/queue3d-data
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

- **Printer pairing** (`QUEUE3D_PRINTER_HOST`/`QUEUE3D_PRINTER_PORT`
  env vars, if the Pi's network setup needs them different from the
  app's own defaults) - not yet wired into the systemd unit as an
  `EnvironmentFile`; add one there if the real deployment needs
  non-default values.
