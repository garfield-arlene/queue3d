"""System performance metrics for the admin-only /admin/system dashboard
- see app/README.md's "System performance dashboard" section for the
full design.

Reads straight from /proc and sysfs rather than adding a psutil
dependency: everything this needs (per-core CPU times, memory, load
average, network counters, process counts, uptime) is already exposed
there in a few lines of parsing each. Every other pip dependency this
app has is either pure Python or a small, already-staged compiled
wheel (bcrypt) - a real dependency for something a few dozen lines of
/proc parsing already covers isn't worth adding wheel-staging surface
for (see deploy/fetch_bundle_assets.sh) when nothing about this needs
psutil's much larger cross-platform surface. `shutil.disk_usage` covers
disk space from the standard library alone.

A background daemon thread (start_metrics_sampler(), called once from
main.py's startup handler, same pattern as jobs.start_auto_finish_poller)
samples CPU/memory/network every SAMPLE_INTERVAL_S and keeps the last
HISTORY_LENGTH samples in memory - independent of whether anyone's
actually looking at the dashboard, so the history graphs are already
populated with real recent data the moment an admin opens the page,
not starting from blank and building up only from page-load onward.
"""

import os
import shutil
import threading
import time
from dataclasses import dataclass
from collections import deque
from pathlib import Path

from backup import backup_targets
from db import DATA_DIR

# 5 minutes of history at a 2s sample interval - long enough to see a
# real trend (a slice starting, a backup running) without the chart
# becoming unreadable clutter or the in-memory buffer growing without
# bound. Both deliberately small enough that a Pi doing real slicing
# work in the background never notices this running alongside it.
SAMPLE_INTERVAL_S = 2
HISTORY_LENGTH = 150


@dataclass
class Sample:
    at: float
    cpu_percpu: list[float]  # one entry per core, 0-100
    mem_percent: float
    swap_percent: float
    net_recv_kBps: float
    net_sent_kBps: float


_history: deque[Sample] = deque(maxlen=HISTORY_LENGTH)
_lock = threading.Lock()
_started = False

# Both None until the first real sample has something to diff against -
# CPU% and network rates are only meaningful as a delta between two
# reads (see _sample_once), so the very first sample after startup
# reports zeros rather than a nonsensical since-boot average.
_prev_cpu_times: dict[str, tuple[int, int]] | None = None
_prev_net: tuple[float, int, int] | None = None


def _read_cpu_times() -> dict[str, tuple[int, int]]:
    """{'cpu0': (idle_time, total_time), ...} - one entry per real core,
    the aggregate 'cpu' line deliberately excluded (the per-core lines
    already give an accurate overall figure by averaging them, and this
    dashboard's whole point is the per-core breakdown htop shows, not a
    single blended number). Units are USER_HZ ticks, not seconds or a
    real timestamp - meaningless on their own, only a percentage of the
    *difference* between two reads is."""
    times = {}
    with open("/proc/stat") as f:
        for line in f:
            parts = line.split()
            if not parts or parts[0] == "cpu" or not parts[0].startswith("cpu"):
                continue
            fields = [int(x) for x in parts[1:]]
            # idle + iowait (fields 3/4: user nice system idle iowait ...) -
            # both are genuinely idle time from the scheduler's own
            # perspective, iowait just idle-while-a-disk-request-is-
            # outstanding rather than idle-with-nothing-to-do.
            idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
            times[parts[0]] = (idle, sum(fields))
    return times


def _read_meminfo() -> dict[str, int]:
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, _, rest = line.partition(":")
            value = rest.strip().split()[0]  # drop the trailing " kB"
            info[key] = int(value)
    return info


def _read_loadavg() -> tuple[float, float, float, int, int]:
    """(load1, load5, load15, running_procs, total_procs) - /proc/loadavg's
    own format is "0.52 0.58 0.59 1/123 4567" (the last field, a recently
    used PID, isn't returned - not useful here)."""
    with open("/proc/loadavg") as f:
        parts = f.read().split()
    load1, load5, load15 = (float(x) for x in parts[0:3])
    running, total = (int(x) for x in parts[3].split("/"))
    return load1, load5, load15, running, total


def _read_uptime_s() -> float:
    with open("/proc/uptime") as f:
        return float(f.read().split()[0])


def _read_net_bytes() -> tuple[int, int]:
    """(bytes_received, bytes_sent) summed across every real interface -
    loopback excluded, since that's traffic this Pi is generating for
    itself, never traffic actually reaching it over the network."""
    recv = sent = 0
    with open("/proc/net/dev") as f:
        lines = f.readlines()[2:]  # two fixed header lines
    for line in lines:
        iface, _, rest = line.partition(":")
        if iface.strip() == "lo":
            continue
        fields = rest.split()
        recv += int(fields[0])
        sent += int(fields[8])
    return recv, sent


def cpu_temperature_c() -> float | None:
    """SoC temperature straight from sysfs, in millidegrees C on the real
    device - None (not an error) wherever this path doesn't exist, e.g.
    developing on a machine with no such sensor exposed this way. The
    dashboard just omits the gauge rather than failing when this is
    None, same "best-effort, never load-bearing" shape as the printer
    status banner."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read().strip()) / 1000.0
    except (OSError, ValueError):
        return None


def _sample_once() -> Sample:
    global _prev_cpu_times, _prev_net
    now = time.time()

    cpu_times = _read_cpu_times()
    cpu_percpu = []
    for label in sorted(cpu_times, key=lambda l: int(l[3:])):
        idle, total = cpu_times[label]
        pidle, ptotal = (_prev_cpu_times or {}).get(label, (idle, total))
        dtotal = total - ptotal
        didle = idle - pidle
        pct = 0.0 if dtotal <= 0 else max(0.0, min(100.0, (dtotal - didle) / dtotal * 100))
        cpu_percpu.append(pct)
    _prev_cpu_times = cpu_times

    mem = _read_meminfo()
    mem_total = mem.get("MemTotal", 0)
    # MemAvailable (kernel 3.14+) is the real "usable without swapping"
    # figure - reclaimable page cache/buffers already excluded, unlike
    # MemFree alone, which would make an otherwise-idle Pi with a large
    # page cache look far more memory-pressured than it actually is.
    mem_available = mem.get("MemAvailable", mem.get("MemFree", 0))
    mem_percent = 0.0 if mem_total <= 0 else (mem_total - mem_available) / mem_total * 100
    swap_total = mem.get("SwapTotal", 0)
    swap_free = mem.get("SwapFree", 0)
    swap_percent = 0.0 if swap_total <= 0 else (swap_total - swap_free) / swap_total * 100

    recv_bytes, sent_bytes = _read_net_bytes()
    if _prev_net:
        prev_at, prev_recv, prev_sent = _prev_net
        elapsed = max(now - prev_at, 0.001)
        recv_kBps = max(0.0, (recv_bytes - prev_recv) / elapsed / 1024)
        sent_kBps = max(0.0, (sent_bytes - prev_sent) / elapsed / 1024)
    else:
        recv_kBps = sent_kBps = 0.0
    _prev_net = (now, recv_bytes, sent_bytes)

    return Sample(
        at=now,
        cpu_percpu=cpu_percpu,
        mem_percent=mem_percent,
        swap_percent=swap_percent,
        net_recv_kBps=recv_kBps,
        net_sent_kBps=sent_kBps,
    )


def _sampler_loop():
    while True:
        try:
            sample = _sample_once()
            with _lock:
                _history.append(sample)
        except Exception:
            # Best-effort background loop, same shape as jobs.py's
            # auto-finish poller - never let one bad tick (a transient
            # /proc read racing a container/cgroup boundary, say) kill
            # the whole sampler. A gap just shows as a short flat
            # stretch in the history graphs, not a crash.
            pass
        time.sleep(SAMPLE_INTERVAL_S)


def start_metrics_sampler() -> None:
    """Starts the loop above on a daemon thread - called once, from
    main.py's startup handler. Guarded against a double start the same
    way it would matter if `--reload` or a future test harness ever
    imported this module twice into the same process."""
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_sampler_loop, daemon=True).start()


def history() -> list[Sample]:
    with _lock:
        return list(_history)


def svg_polyline_points(values: list[float], max_value: float, width: int = 600, height: int = 100) -> str:
    """"x1,y1 x2,y2 ..." for one <polyline> in admin_system.html's history
    charts - a Jinja global (see templates_env.py), not a per-route
    computation, since three charts each need this applied to several
    series (per-core CPU, memory, swap, both network directions).

    Oldest sample first, spread evenly across `width` - the line reads
    left-to-right as a timeline with the newest sample at the right
    edge, the same convention every real system monitor uses. Y is
    flipped from SVG's own coordinate space (which grows downward) so
    "more" reads as "higher up the chart," matching what every value on
    the page (a percentage, a rate) actually means to a viewer. An empty
    or single-value input still returns something renderable (a flat
    line at 0, or one lone point) rather than an empty string that
    would silently drop the whole <polyline> from the page - real for
    the first few ticks after a fresh app start, before the history
    buffer has anything in it yet."""
    if not values:
        return f"0,{height} {width},{height}"
    if max_value <= 0:
        max_value = 1.0
    step = width / max(len(values) - 1, 1)
    points = []
    for i, v in enumerate(values):
        x = i * step
        y = height - (min(max(v, 0.0), max_value) / max_value) * height
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def snapshot() -> dict:
    """Everything the dashboard's "top" section needs beyond the rolling
    history - computed fresh on every request rather than only on the
    sampler's own 2s cadence, since load average/uptime/disk/process
    counts don't need a delta between two reads the way CPU%/network
    rates do, so there's no reason to ever show them stale."""
    load1, load5, load15, running_procs, total_procs = _read_loadavg()
    has_swap = _read_meminfo().get("SwapTotal", 0) > 0
    disk_root = shutil.disk_usage("/")
    # Only a genuinely separate device is worth its own gauge - in dev
    # (no QUEUE3D_DATA_DIR set), DATA_DIR sits on the same filesystem as
    # root, and showing the identical numbers twice would just be
    # visual clutter, not real information.
    disk_data = None
    try:
        if DATA_DIR.stat().st_dev != os.stat("/").st_dev:
            disk_data = shutil.disk_usage(DATA_DIR)
    except OSError:
        pass
    return {
        "load1": load1,
        "load5": load5,
        "load15": load15,
        "running_procs": running_procs,
        "total_procs": total_procs,
        "uptime_s": _read_uptime_s(),
        "cpu_count": len(_read_cpu_times()),
        "has_swap": has_swap,
        "temperature_c": cpu_temperature_c(),
        "disk_root": disk_root,
        "disk_data": disk_data,
        "data_dir": str(DATA_DIR),
    }


def disk_mounts() -> list[dict]:
    """All four mountpoints worth watching on a real deployment - the OS
    drive, the app's own data drive, and both rotating backup drives
    (see backup.backup_targets) - always all four, unlike snapshot()'s
    own single Data gauge above, which only shows a mountpoint when it's
    a genuinely different device from root. That "only if different"
    collapsing is right for a quick top-of-page glance, but wrong here:
    a real deployment has four physically separate drives, and this
    section exists specifically to watch all four at once, including a
    dev setup where some happen to coincide - showing the same numbers
    under more than one label there is more honest than silently
    hiding what looks like a duplicate.

    A backup drive's own directory may not exist yet in dev (nothing's
    ever backed up there) - `usage` is None for a mountpoint that can't
    be read at all, rather than raising, so a not-yet-existing or
    (on the real device) unplugged drive shows as a clear "not
    available" line instead of a crash."""
    targets = backup_targets()
    mounts = [
        ("OS drive", Path("/")),
        ("Data drive", DATA_DIR),
        ("Backup drive A", targets["a"]),
        ("Backup drive B", targets["b"]),
    ]
    result = []
    for label, path in mounts:
        try:
            usage = shutil.disk_usage(path)
            percent = usage.used / usage.total * 100 if usage.total else 0.0
        except OSError:
            usage = None
            percent = None
        result.append({"label": label, "path": str(path), "usage": usage, "percent": percent})
    return result
