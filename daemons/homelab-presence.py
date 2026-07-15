#!/usr/bin/env python3
"""Idle Discord presence: homelab heartbeat. Shows anonymous, healthy-only
homelab stats via the LOCAL Discord client (pypresence over IPC) when no Steam
game is running. Sibling of steam-presence.py and nd-rpc-bridge.py.

OPSEC rules, baked in rather than configured:
  * No hostnames, addresses, service names, versions or ports in any line.
  * Scale numbers rounded/slow-moving (0.1T granularity). The live-workload frame
    is the one deliberate exception: it shows coarse current activity (scrub/
    resilver %, load average, aggregate GB/s) to feel alive, but still no
    per-service or per-flow detail. Note it can surface a degraded state (a
    resilver means reduced redundancy right now) -- kept because the box takes no
    inbound from the open internet, so there is nothing to act on.
  * Backup freshness renders as a checkmark, never minutes, so cadence stays private.
  * Healthy or silent: any degraded core signal (stale backup, low tank, host
    scrape down, Prometheus unreachable) CLEARS presence entirely. A public
    profile must not double as a status page of what is currently weak.

Data: read-only queries to Prometheus (LAN/VPN only).
Yields to steam-presence AND media bridges: a running Steam game, or a fresh
jf-rpc-bridge / nd-rpc-bridge *-nowplaying signal (Jellyfin watch / Navidrome
track), clears the heartbeat so the real activity owns the card.

Channels: a channel is a function returning {details, state} or None. One
channel today (heartbeat); a rotator picks among healthy channels so future
channels (brain ticker, commit pulse) drop in without redesign.

Run: uv run --with pypresence homelab-presence.py [--once]
Env (~/.config/homelab-presence.env): HL_APP_ID (required, a Discord app you
own), PROM_URL, HL_POLL.
"""
import asyncio
import glob
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

from pypresence import Presence
try:
    from pypresence import ActivityType
    _WATCHING = ActivityType.WATCHING
except Exception:                     # old pypresence: fall back to Playing
    _WATCHING = None

APP_ID = os.environ.get("HL_APP_ID")
PROM = os.environ.get("PROM_URL", "http://localhost:9090")
POLL = int(os.environ.get("HL_POLL", "20"))   # also the frame rotation cadence;
                                              # Discord RPC floor is ~15s (5/20s bucket)
IMAGE = os.environ.get("HL_IMAGE", "auto")   # asset key, https URL, "auto" (app icon), "" off


def resolve_image():
    """'auto' -> the app's current App Icon via its public rpc endpoint, so you
    can swap the icon in the dev portal without touching this box."""
    if IMAGE != "auto":
        return IMAGE
    try:
        req = urllib.request.Request(
            f"https://discord.com/api/v9/applications/{APP_ID}/rpc",
            headers={"User-Agent": "homelab-presence/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            h = json.load(r).get("icon")
        return f"https://cdn.discordapp.com/app-icons/{APP_ID}/{h}.png?size=256" if h else ""
    except Exception:
        return ""
BACKUP_MAX_AGE = 7200          # matches the existing >2h Grafana alert threshold
TANK_MIN_FREE = 1e12           # below 1T free is a problem, not a flex
MEDIA_FRESH = 20               # sec; jf/nd bridges rewrite their *-nowplaying every poll
_RUNTIME = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
STEAM_COMMON = "/steamapps/common/"
RUNTIME_RE = re.compile(r"/common/(steamlinuxruntime|proton|steamvr|steamworks)", re.I)

ONCE = "--once" in sys.argv


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- prometheus
def promq(query):
    """Single instant query -> float, or None on any failure."""
    url = f"{PROM}/api/v1/query?" + urllib.parse.urlencode({"query": query})
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            res = json.load(r)["data"]["result"]
        return float(res[0]["value"][1]) if res else None
    except Exception:
        return None


def promq_meta(query, label):
    """First result's value for a given label (e.g. which pool is scanning),
    or None. Used where a frame needs a label, not the numeric value."""
    url = f"{PROM}/api/v1/query?" + urllib.parse.urlencode({"query": query})
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            res = json.load(r)["data"]["result"]
        return res[0]["metric"].get(label) if res else None
    except Exception:
        return None


# ---------------------------------------------------------------- workload
def workload_frame():
    """Live 'what is the box doing right now' frame: (details, state) or None.
    Priority resilver > scrub > heavy compute > heavy I/O > idle. Every signal
    is physical/health only (scan %, load, aggregate disk bytes) -- no service
    names, no per-flow counters -- so it stays on the opsec-clean side of the
    line. The reactive part is the details line; state pairs a phrase that fits
    the action. Disk count is deliberately absent (the snapshot frame already
    carries it); idle leads with the ARC flex instead."""
    scan = promq("max(zpool_scan_active)")               # 0 none / 1 scrub / 2 resilver
    if scan and scan >= 1:
        pool = promq_meta("zpool_scan_active > 0", "pool") or "pool"
        pct = promq("max(zpool_scan_percent)") or 0
        if scan >= 2:
            return (f"resilvering {pool} · {pct:.0f}%", "rebuilding redundancy")
        return (f"scrubbing {pool} · {pct:.0f}%", "verifying every block")
    load = promq('node_load1{instance="pve"}')
    threads = promq('count(node_cpu_seconds_total{instance="pve",mode="idle"})')
    if load and threads and load > threads * 0.5:         # >half the threads busy
        return (f"crunching · load {load:.1f}", f"{int(threads)} threads engaged")
    # whole-disk read+write bytes/s (regex excludes partitions like sda1/nvme0n1p1)
    io = promq('sum(rate(node_disk_read_bytes_total{instance="pve",device=~"sd[a-z]+|nvme[0-9]+n[0-9]+"}[2m]))'
               ' + sum(rate(node_disk_written_bytes_total{instance="pve",device=~"sd[a-z]+|nvme[0-9]+n[0-9]+"}[2m]))')
    if io and io > 5e8:                                   # >0.5 GB/s sustained
        return (f"moving {io/1e9:.1f} GB/s", "sustained across the array")
    hits = promq("node_zfs_arc_hits")
    misses = promq("node_zfs_arc_misses")
    arc = 100 * hits / (hits + misses) if hits and misses and (hits + misses) else None
    d = f"idle · ARC {arc:.1f}% warm" if arc else "idle · all quiet"
    s = f"{int(threads)} threads at rest" if threads else None
    return (d, s)


# ---------------------------------------------------------------- heartbeat
def heartbeat(turn):
    """Rotating homelab stats. None unless EVERY core gate signal is healthy.
    Layout: details line rotates through stat frames (one per poll), state
    line (CTs) and uptime timer stay constant so the card reads as one thing
    with a scrolling top line."""
    tank = promq('node_filesystem_avail_bytes{instance="pve",mountpoint="/tank"}')
    backup = promq("vps_backup_last_success_timestamp_seconds")
    host_up = promq('up{instance="pve",job="node"}')
    if not tank or not backup or host_up != 1:
        return None
    if tank < TANK_MIN_FREE or time.time() - backup > BACKUP_MAX_AGE:
        return None
    # Field -> Discord slot (Watching activity):
    #   details -> card bold line (rotating stat)   state -> card 2nd line (paired stat)
    #   start   -> elapsed timer = uptime.
    #   Both the card header AND the memberlist one-liner come from the app NAME
    #   (set in the dev portal), not from RPC. The card body is only these two
    #   text lines, so a custom string there always displaces a stat.
    # backup freshness stays a silence gate but is never displayed.

    # Disk/pool integrity, computed once and reused by the snapshot + scan frames.
    d_ok = promq("count(smartmon_device_smart_healthy==1)")
    d_all = promq("count(smartmon_device_smart_healthy)")
    p_ok = promq("count(zpool_health==1)")               # zpool.prom: 1 == ONLINE
    p_all = promq("count(zpool_health)")
    integrity = (f"{int(d_ok)} disks · {int(p_ok)} pools healthy"
                 if d_ok and d_ok == d_all and p_ok and p_ok == p_all else None)

    frames = [(f"tank {tank/1e12:.1f}T free", None)]
    watts = promq("ipmi_dcmi_power_consumption_watts")   # rounded to 5W, load not watchable
    temp = promq('ipmi_temperature_celsius{name=~"Inlet.*"}')
    if watts and temp:
        exhaust = promq('ipmi_temperature_celsius{name=~"Exhaust.*"}')
        fans = promq("avg(ipmi_fan_speed_rpm)")
        airflow = (f"exhaust {int(exhaust)}°C · fans {fans/1000:.1f}k RPM"
                   if exhaust and fans else None)
        frames.append((f"drawing {5*round(watts/5)}W · intake {int(temp)}°C", airflow))
    snaps = promq("pve_zfs_snapshots")
    if snaps:
        frames.append((f"{int(snaps)} ZFS snapshots", integrity))
    wl = workload_frame()
    if wl:
        frames.append(wl)
    details, state_override = frames[turn % len(frames)]
    total = promq("pve_ct_total")
    green = promq("pve_ct_running")
    state = f"{int(green)} CTs green" if total and green == total else None
    state = state_override or state
    boot = promq('node_boot_time_seconds{instance="pve"}')
    return {"details": details, "state": state, "start": int(boot) if boot else None}


# ---------------------------------------------------------------- game yield
def steam_game_running():
    """True when any real Steam game process exists (runtime plumbing excluded).
    Cheap /proc scan, same matching idea as steam-presence.py."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            hay = " ".join([
                os.readlink(f"/proc/{pid}/exe"),
                os.readlink(f"/proc/{pid}/cwd"),
                open(f"/proc/{pid}/cmdline").read().replace("\0", " "),
            ]).replace("\\", "/").lower()
        except OSError:
            continue
        if STEAM_COMMON in hay and not RUNTIME_RE.search(hay):
            return True
    return False


def media_playing():
    """True if any media bridge signals an active session via a fresh
    *-nowplaying file (jf-rpc-bridge / nd-rpc-bridge write these every poll while
    something plays). Stale files — a stopped bridge — are ignored, so the
    heartbeat resumes rather than hiding forever."""
    now = time.time()
    for f in glob.glob(os.path.join(_RUNTIME, "*-nowplaying")):
        try:
            if now - os.path.getmtime(f) < MEDIA_FRESH:
                return True
        except OSError:
            pass
    return False


# ---------------------------------------------------------------- main loop
def main():
    if not APP_ID:
        sys.exit("HL_APP_ID not set (create a Discord app, put its id in ~/.config/homelab-presence.env)")
    image = resolve_image()
    rpc, shown = None, None

    def clear(dead=False):
        """dead=True: the pipe is known broken. Skip rpc.clear() -- it reads a
        response from the socket and can block forever on a dead pipe, which
        wedges the whole loop (observed after a Discord client restart)."""
        nonlocal rpc, shown
        if rpc:
            if not dead:
                try:
                    rpc.clear()
                except Exception:
                    pass
            try:
                rpc.close()
            except Exception:
                pass
        rpc, shown = None, None

    turn = 0
    while True:
        lines = None
        if not steam_game_running() and not media_playing():
            lines = heartbeat(turn)
        turn += 1

        if ONCE:
            log(f"would show: {lines}" if lines else "silent (game/media running or unhealthy)")
            return

        if not lines:
            if shown:
                log("going silent")
                clear()
        elif lines != shown:
            try:
                if not rpc:
                    # fresh event loop per connection: pypresence's close()
                    # kills the loop, and a reused dead loop makes every later
                    # connect fail with "Event loop is closed".
                    asyncio.set_event_loop(asyncio.new_event_loop())
                    rpc = Presence(APP_ID)
                    rpc.connect()
                    log("connected to Discord IPC")
                rpc.update(details=lines["details"],
                           **({"state": lines["state"]} if lines["state"] else {}),
                           **({"activity_type": _WATCHING} if _WATCHING else {}),
                           **({"large_image": image} if image else {}),
                           **({"start": lines["start"]} if lines.get("start") else {}))
                shown = lines
                log(f"presence: {lines['details']} / {lines['state']}")
            except Exception as e:       # Discord closed / socket gone: retry next poll
                log(f"update failed ({e.__class__.__name__}: {e}), retrying")
                clear(dead=True)
        time.sleep(POLL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
