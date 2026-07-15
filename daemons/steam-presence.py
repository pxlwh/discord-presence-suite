#!/usr/bin/env python3
"""Steam game -> Discord "Playing" presence, via the LOCAL Discord client
(pypresence over IPC). Token-free and ToS-safe in mechanism: it drives the running
desktop Discord exactly like a game's own RPC does, so presence only shows while
Discord is open here.

Why this exists: Discord's detection DB has native-Linux executables for only ~7
games total, ships empty executable lists for most post-2024 titles, and carries
stale exe names for older ones (RimWorld lists its 2018 binaries). So process
detection misses most of a Linux Steam library. This daemon does the detection
itself -- watches /proc for processes running out of steamapps/common/<installdir>
-- and sets the activity using the game's OFFICIAL Discord application id, looked
up by name in Discord's own detectable DB. Official id => official game name (and
usually icon) on the profile, zero per-game config.

Identity note: the app ids belong to the games' publishers, not us. This is the
same borrowed-id mechanism the custom-RPC ecosystem uses; worst realistic case is
Discord ignoring the activity.

Double-show guard: when a game runs under Proton AND its DB entry has executables,
Discord's own detection can usually match it, so we stand down for that game
rather than create a second activity. Native builds and empty-exe titles are ours.

Run: uv run --with pypresence --with psutil steam-presence.py [--once]
     --once: single scan, print what would be set, exit (no presence pushed).
"""
import asyncio
import json
import os
import re
import sys
import time
import urllib.request

import psutil
from pypresence import Presence

STEAM_ROOT = os.path.expanduser(os.environ.get("STEAM_ROOT", "~/.local/share/Steam"))
CACHE = os.path.expanduser(os.environ.get("SP_CACHE", "~/.cache/steam-presence"))
DB_URL = "https://discord.com/api/v9/applications/detectable"
DB_MAX_AGE = 7 * 86400
POLL = int(os.environ.get("SP_POLL", "10"))
# Steam plumbing that also lives in steamapps/common but is never a game.
RUNTIME_RE = re.compile(r"^(steam linux runtime|proton|steamvr|steamworks)", re.I)

ONCE = "--once" in sys.argv


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- detectable DB
def detectable_db():
    """Discord's public detection DB, cached a week. Stale beats absent."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "detectable.json")
    fresh = os.path.exists(path) and time.time() - os.path.getmtime(path) < DB_MAX_AGE
    if not fresh:
        try:
            req = urllib.request.Request(DB_URL,
                                         headers={"User-Agent": "steam-presence/1.0 (+desktop)"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            json.loads(data)                       # validate before replacing the cache
            with open(path, "wb") as f:
                f.write(data)
            log(f"detectable DB refreshed ({len(data)//1024} KiB)")
        except Exception as e:
            log(f"detectable DB refresh failed, using cache: {e}")
    with open(path) as f:
        return json.load(f)


def norm(name):
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def build_index(db):
    """normalized name/alias -> (app_id, official_name, has_executables)."""
    idx = {}
    for app in db:
        entry = (app["id"], app["name"], bool(app.get("executables")))
        for n in [app.get("name", "")] + (app.get("aliases") or []):
            idx.setdefault(norm(n), entry)
    return idx


# ---------------------------------------------------------------- Steam library
def steam_libraries():
    libs = [STEAM_ROOT]
    vdf = os.path.join(STEAM_ROOT, "steamapps", "libraryfolders.vdf")
    try:
        for m in re.finditer(r'"path"\s+"([^"]+)"', open(vdf).read()):
            if m.group(1) not in libs:
                libs.append(m.group(1))
    except OSError:
        pass
    return libs


def installed_games():
    """lowercased installdir -> manifest name, runtime plumbing excluded."""
    games = {}
    for lib in steam_libraries():
        sa = os.path.join(lib, "steamapps")
        for f in os.listdir(sa) if os.path.isdir(sa) else []:
            if not f.startswith("appmanifest_"):
                continue
            try:
                t = open(os.path.join(sa, f)).read()
                name = re.search(r'"name"\s+"([^"]+)"', t).group(1)
                idir = re.search(r'"installdir"\s+"([^"]+)"', t).group(1)
            except (OSError, AttributeError):
                continue
            if not RUNTIME_RE.match(name):
                games[idir.lower()] = name
    return games


# ---------------------------------------------------------------- process scan
def running_game(games):
    """Newest-started process inside a game's installdir.
    Returns (installdir, create_time, is_wine) or None. Matches on exe, cwd and
    cmdline: native builds hit exe/cwd; Proton hits cmdline, where Proton 11 maps
    the library as S:\\common\\<dir>\\... so we match '/common/<dir>/' not the
    full steamapps path."""
    best = None
    me = os.getuid()
    for p in psutil.process_iter(["exe", "cwd", "cmdline", "create_time", "uids"]):
        try:
            if p.info["uids"] and p.info["uids"].real != me:
                continue
            hay = " ".join(filter(None, [p.info["exe"], p.info["cwd"],
                                         " ".join(p.info["cmdline"] or [])]))
            hay = hay.replace("\\", "/").lower()
            for idir in games:
                if f"/common/{idir}/" in hay or hay.endswith(f"/common/{idir}"):
                    is_wine = ".exe" in hay
                    if best is None or p.info["create_time"] > best[1]:
                        best = (idir, p.info["create_time"], is_wine)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return best


# ---------------------------------------------------------------- main loop
def main():
    idx = build_index(detectable_db())
    db_checked = time.time()
    rpc, current = None, None                     # live connection, (idir, app_id)

    def clear(dead=False):
        """dead=True: pipe known broken. rpc.clear() reads a response and can
        block forever on a dead pipe (wedges the loop), so skip it then."""
        nonlocal rpc, current
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
        rpc, current = None, None

    while True:
        if time.time() - db_checked > 86400:      # daily staleness check is plenty
            idx = build_index(detectable_db())
            db_checked = time.time()

        games = installed_games()
        found = running_game(games)
        if not found:
            if current:
                log(f"game exited, clearing presence")
                clear()
            if ONCE:
                log("no game running")
                return
            time.sleep(POLL)
            continue

        idir, started, is_wine = found
        name = games[idir]
        hit = idx.get(norm(name))
        if not hit:
            if current:
                clear()
            if ONCE:
                log(f"running: {name!r} -- no Discord app entry, would skip")
                return
            time.sleep(POLL)
            continue
        app_id, official, has_exes = hit
        if is_wine and has_exes:                  # Discord's own detection owns this one
            if current:
                clear()
            if ONCE:
                log(f"running: {name!r} under Proton, DB has exes -- Discord's job, would skip")
                return
            time.sleep(POLL)
            continue

        if ONCE:
            log(f"running: {name!r} -> app {app_id} ({official}), started {int(started)}")
            return

        if current != (idir, app_id):
            clear()
        try:
            if not rpc:
                # fresh loop per connection: pypresence's close() kills the
                # asyncio loop and a dead loop breaks every later connect.
                asyncio.set_event_loop(asyncio.new_event_loop())
                rpc = Presence(app_id)
                rpc.connect()
            # repushed every poll (identical payload = Discord-side no-op):
            # a Discord client restart mid-game breaks the pipe, the next
            # push raises, and the reconnect path heals it.
            rpc.update(start=int(started))
            if current != (idir, app_id):
                log(f"presence set: {official} (app {app_id})")
            current = (idir, app_id)
        except Exception as e:                    # Discord closed / socket gone: retry next poll
            log(f"presence set failed ({e.__class__.__name__}: {e}), retrying")
            clear(dead=True)
        time.sleep(POLL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
