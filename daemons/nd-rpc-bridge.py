#!/usr/bin/env python3
"""Navidrome now-playing -> Discord rich presence, via the LOCAL Discord client
(pypresence over IPC). Token-free and ToS-safe: it drives the running desktop
Discord exactly like a game does, so presence only shows while Discord is open here.

Three source tiers, exact wherever possible:
  1. DESKTOP  -- Navidrome web player in a browser, read over MPRIS (playerctl).
                 Exact position + real pause + instant clear when the tab closes.
  2. PHONE    -- Symfonium etc., read over KDE Connect's mprisremote D-Bus plugin
                 (jeepney). Also exact position + real pause, works over the network.
  3. SUBSONIC -- Navidrome getNowPlaying. Last resort (phone unreachable by KDE
                 Connect). No pause/position in the protocol, so the bar is an
                 estimate from minutesAgo and there is no real pause.

Pause: Discord's "Listening" bar ALWAYS animates and cannot be frozen, so on pause
we drop start/end entirely (the bar disappears) rather than let it drift or jitter.

Closed-player handling: a closed desktop tab leaves a stale Subsonic entry, and so
does a closed Symfonium IF KDE Connect is reachable. The Subsonic fallback skips the
web client always, and skips the phone client when KDE Connect is reachable (tiers 1
and 2 are authoritative for those), so closing a player clears presence promptly.

Cover art is looked up from a public source (iTunes then Deezer) because Navidrome is
private-network-only and Discord's servers can't fetch the cover (or a file:// artUrl).

Run: uv run --with pypresence --with jeepney nd-rpc-bridge.py
     (env from ~/.config/nd-rpc-bridge.env)
"""
import hashlib
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request

from pypresence import Presence
try:
    from pypresence import ActivityType
    _LISTENING = ActivityType.LISTENING
except Exception:
    _LISTENING = None

ND_URL = os.environ.get("ND_URL", "https://navidrome.example.com")
ND_USER = os.environ["ND_USER"]
ND_PASS = os.environ["ND_PASS"]
ND_ME = os.environ.get("ND_ME", ND_USER)            # username to match in now-playing
APP_ID = os.environ["DISCORD_APP_ID"]               # a Discord application id ("Navidrome")
WEB_CLIENT = os.environ.get("WEB_CLIENT", "NavidromeUI")     # browser web player name
PHONE_PLAYER = os.environ.get("PHONE_PLAYER", "Symfonium")   # phone app (KDE Connect player)
# Dedicated desktop MPRIS clients (bus-name prefixes) to treat as Navidrome outright,
# in addition to the browser web player. They expose clean metadata directly.
NAV_PLAYERS = os.environ.get("NAV_PLAYERS", "supersonic feishin").lower().split()
# Navidrome cover proxy over a public HTTPS tunnel: primary cover source (Navidrome's own
# embedded art, ~100% of the library, no third-party music-API queries per track).
COVER_FUNNEL = os.environ.get("COVER_FUNNEL", "https://cover-proxy.example.com")
POLL = 1
SEEK = 3        # s; repush if real position diverges from the bar's expected position
PHONE_STALE = 15  # s; KDE Connect caches a closed app's state (paused or frozen-playing),
                  # so if the phone state stops changing this long, treat it as gone.

# Yield signal other daemons watch (homelab-presence hides its heartbeat while a
# track is showing). Rewritten each poll while playing; removed when it stops.
NOWPLAYING = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "nd-nowplaying")

# ---------------------------------------------------------------- Subsonic (tier 3)
def subsonic(view, **params):
    salt = hashlib.sha1(os.urandom(8)).hexdigest()[:12]
    token = hashlib.md5((ND_PASS + salt).encode()).hexdigest()
    p = {"u": ND_USER, "t": token, "s": salt, "v": "1.16.1", "c": "ndrpc", "f": "json", **params}
    url = f"{ND_URL}/rest/{view}.view?" + urllib.parse.urlencode(p)
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())["subsonic-response"]

def now_playing():
    entries = (subsonic("getNowPlaying").get("nowPlaying") or {}).get("entry") or []
    if isinstance(entries, dict):
        entries = [entries]
    return [e for e in entries if e.get("username") == ND_ME]

# ---------------------------------------------------------------- MPRIS desktop (tier 1)
def _pctl(*args):
    try:
        return subprocess.run(["playerctl", *args],
                              capture_output=True, text=True, timeout=3).stdout.strip()
    except Exception:
        return ""

def mpris_navidrome():
    """First MPRIS player that is Navidrome and Playing/Paused, else None.
    Prefer plasma-browser-integration (clean split metadata) over native chromium."""
    names = [n for n in _pctl("-l").splitlines() if n]
    def rank(n):                                   # dedicated client > clean browser > native chromium
        nl = n.lower()
        if any(nl.startswith(p) for p in NAV_PLAYERS):
            return 0
        return 1 if nl.startswith("plasma-browser") else 2 if nl.startswith("chromium") else 3
    names.sort(key=rank)
    for n in names:
        if n.lower().startswith("kdeconnect"):     # phone is tier 2's job (jeepney), skip here
            continue
        status = _pctl("-p", n, "status")
        if status not in ("Playing", "Paused"):
            continue
        meta = {}
        for line in _pctl("-p", n, "metadata").splitlines():
            parts = line.split(None, 2)            # "<player> <key> <value...>"
            if len(parts) == 3 and parts[1] not in meta:
                meta[parts[1]] = parts[2]          # keep FIRST: a multi-artist track emits
                                                   # one xesam:artist line per artist and the
                                                   # first is the primary (else we show the last)
        url = meta.get("xesam:url", "")
        src = meta.get("kde:mediaSrc", "")
        title = meta.get("xesam:title", "")
        is_native = any(n.lower().startswith(p) for p in NAV_PLAYERS)
        is_nav = (is_native or "navidrome" in url.lower() or "navidrome" in src.lower()
                  or title.endswith(" - Navidrome"))
        if not is_nav:
            continue
        try:
            pos = float(_pctl("-p", n, "position"))
        except ValueError:
            pos = 0.0
        try:
            length = int(meta.get("mpris:length", "0")) // 1_000_000
        except ValueError:
            length = 0
        return {"status": status, "position": pos, "length": length, "player": n,
                "title": title, "artist": meta.get("xesam:artist", ""),
                "album": meta.get("xesam:album", "")}
    return None

# ---------------------------------------------------------------- KDE Connect phone (tier 2)
KDEC = "org.kde.kdeconnect"
try:
    from jeepney import DBusAddress, new_method_call, Properties
    from jeepney.io.blocking import open_dbus_connection
    _dbus = open_dbus_connection(bus="SESSION")
except Exception:
    _dbus = None

def _uw(d):                       # KDE Connect variants come back as (signature, value)
    return {k: (v[1] if isinstance(v, tuple) and len(v) == 2 else v) for k, v in d.items()}

def kdec_query():
    """(reachable, phone_state_or_None) for PHONE_PLAYER via KDE Connect mprisremote."""
    if _dbus is None:
        return False, None
    try:
        daemon = DBusAddress("/modules/kdeconnect", bus_name=KDEC,
                             interface="org.kde.kdeconnect.daemon")
        ids = _dbus.send_and_get_reply(new_method_call(daemon, "devices", "bb",
                                                       (True, True))).body[0]
    except Exception:
        return False, None
    reachable = bool(ids)
    for dev in ids:
        try:
            path = f"/modules/kdeconnect/devices/{dev}/mprisremote"
            addr = DBusAddress(path, bus_name=KDEC,
                               interface="org.kde.kdeconnect.device.mprisremote")
            props = Properties(addr)
            cur = _uw(_dbus.send_and_get_reply(props.get_all()).body[0])
            players = cur.get("playerList") or []
            if not players:                                   # cold plugin: ask the phone
                _dbus.send_and_get_reply(new_method_call(addr, "requestPlayerList"))
                time.sleep(0.3)
                cur = _uw(_dbus.send_and_get_reply(props.get_all()).body[0])
                players = cur.get("playerList") or []
            if PHONE_PLAYER not in players:                   # app closed on the phone
                continue
            if cur.get("player") != PHONE_PLAYER:
                _dbus.send_and_get_reply(props.set("player", "s", PHONE_PLAYER))
                time.sleep(0.2)
                cur = _uw(_dbus.send_and_get_reply(props.get_all()).body[0])
            return reachable, {
                "isPlaying": bool(cur.get("isPlaying")),
                "position": int(cur.get("position") or 0) // 1000,    # ms -> s
                "length": int(cur.get("length") or 0) // 1000,
                "title": cur.get("title") or "",
                "artist": cur.get("artist") or "",
                "album": cur.get("album") or "",
            }
        except Exception:
            continue
    return reachable, None

# ---------------------------------------------------------------- cover lookup
_cover = {}
def _itunes(artist, album):
    q = urllib.parse.urlencode({"term": f"{artist} {album}", "entity": "album", "limit": 1})
    with urllib.request.urlopen("https://itunes.apple.com/search?" + q, timeout=5) as r:
        res = (json.load(r).get("results") or [])
    a = res[0].get("artworkUrl100") if res else None
    return a.replace("100x100bb", "512x512bb") if a else None

def _deezer(artist, album):
    q = urllib.parse.urlencode({"q": f"{artist} {album}", "limit": 1})
    with urllib.request.urlopen("https://api.deezer.com/search/album?" + q, timeout=5) as r:
        res = (json.load(r).get("data") or [])
    return (res[0].get("cover_big") or res[0].get("cover_xl")) if res else None

_UA = {"User-Agent": "nd-rpc-bridge/1.0 (https://github.com/pxlwh)"}   # MusicBrainz requires a real UA
def _musicbrainz(artist, album):
    # broad community catalog (obscure/netlabel releases iTunes+Deezer miss); cover
    # served from Cover Art Archive (public, Discord-fetchable). Verify art exists so
    # we never hand Discord a 404 that renders as a broken image.
    q = urllib.parse.urlencode({"query": f'releasegroup:"{album}" AND artist:"{artist}"',
                                "fmt": "json", "limit": 3})
    req = urllib.request.Request("https://musicbrainz.org/ws/2/release-group/?" + q, headers=_UA)
    with urllib.request.urlopen(req, timeout=6) as r:
        rgs = (json.load(r).get("release-groups") or [])
    for rg in rgs[:3]:
        try:
            caa = urllib.request.Request(
                f"https://coverartarchive.org/release-group/{rg['id']}", headers=_UA)
            with urllib.request.urlopen(caa, timeout=6) as cr:
                imgs = json.load(cr).get("images") or []
            front = next((i for i in imgs if i.get("front")), imgs[0] if imgs else None)
            if front:
                t = front.get("thumbnails", {})
                return t.get("500") or t.get("large") or front.get("image")
        except Exception:
            continue
    return None

def cover_url(artist, album):
    k = f"{artist}|{album}".lower()
    if k in _cover:
        return _cover[k]
    url = None
    for fetch in (_itunes, _deezer, _musicbrainz):
        try:
            url = fetch(artist, album)
            if url:
                break
        except Exception:
            pass
    _cover[k] = url
    return url

# ---------------------------------------------------------------- presence
def push(rpc, title, artist, album, start, length, playing, cover_id=None):
    # Funnel (Navidrome's own embedded art) is primary: ~100% of the library and no
    # third-party music-API queries. cover_url (iTunes/Deezer/MusicBrainz) is the
    # fallback for a track not in Navidrome. Omit on a total miss (a bad key draws "?").
    img = (f"{COVER_FUNNEL}/?id={urllib.parse.quote(cover_id)}"
           if (cover_id and COVER_FUNNEL) else None) or cover_url(artist, album)
    kw = dict(name=(title or "Unknown")[:128],
              details=(title or "Unknown")[:128],
              state=(f"by {artist}"[:128] if artist else None))
    if img:                          # omit when no cover; an invalid key draws a broken "?"
        kw["large_image"] = img
        kw["large_text"] = (album or "Navidrome")[:128]
    if playing:
        kw["start"] = start                 # start + end => seek bar; omitted while
        if length > 0:                      # paused, because Discord's bar can't freeze
            kw["end"] = start + length
    if _LISTENING is not None:
        rpc.update(activity_type=_LISTENING, **kw)
    else:
        rpc.update(**kw)

def main():
    rpc = Presence(APP_ID)
    connected = False
    last_sig = last_playing = None
    last_start = 0
    phone_key, phone_fresh = None, 0.0      # staleness tracking for the cached phone source
    desktop_key, desktop_fresh = None, 0.0  # same: drop a paused/forgotten desktop player after STALE
    while True:
        try:
            if not connected:
                rpc.connect()              # raises if Discord desktop isn't running
                connected = True
                last_sig = last_playing = None

            sub = now_playing()
            web = next((e for e in sub if e.get("playerName") == WEB_CLIENT), None)
            desktop = mpris_navidrome()
            kdec_reach, phone = kdec_query()

            cands = []                                       # (prefer a Playing source)
            if desktop:
                dkey = (desktop["title"], int(desktop["position"]))
                if dkey != desktop_key:                 # position advancing => still active
                    desktop_key, desktop_fresh = dkey, time.time()
                if time.time() - desktop_fresh < PHONE_STALE:   # paused/forgotten > 15s => drop, like the phone
                    t, a, al = desktop["title"], desktop["artist"], desktop["album"]
                    length = desktop["length"]
                    if desktop["player"].startswith("chromium"):   # native = mangled metadata
                        if web:
                            t = web.get("title") or t
                            a = web.get("artist") or a
                            al = web.get("album") or al
                            length = length or int(web.get("duration") or 0)
                        elif not a and t.endswith(" - Navidrome"):
                            parts = t.rsplit(" - ", 2)
                            if len(parts) == 3:
                                t, a = parts[0], parts[1]
                    cands.append({"playing": desktop["status"] == "Playing",
                                  "pos": desktop["position"], "length": length,
                                  "title": t, "artist": a, "album": al})
            else:
                desktop_key = None
            # Phone counts ONLY while actively playing: isPlaying true AND position
            # advancing. KDE Connect caches a closed/paused app's last state, which
            # would otherwise show a stale phone track over what you're really playing.
            if phone and phone["isPlaying"]:
                pkey = (phone["title"], phone["position"])    # position must move = live
                if pkey != phone_key:
                    phone_key, phone_fresh = pkey, time.time()
                if time.time() - phone_fresh < PHONE_STALE:   # frozen position => closed, drop
                    cands.append({"playing": True, "pos": phone["position"],
                                  "length": phone["length"], "title": phone["title"],
                                  "artist": phone["artist"], "album": phone["album"]})
            else:
                phone_key = None

            chosen = next((c for c in cands if c["playing"]), cands[0] if cands else None)

            if chosen is None:                               # tier 3: Subsonic estimate
                def owned(pn):                               # handled by tier 1 or 2 already?
                    p = (pn or "").lower()
                    if p == WEB_CLIENT.lower():              # browser web player -> tier 1
                        return True
                    if any(p.startswith(x) for x in NAV_PLAYERS):  # desktop client -> tier 1
                        return True
                    if kdec_reach and p == PHONE_PLAYER.lower():   # phone -> tier 2
                        return True
                    return False
                pe = sorted((e for e in sub if not owned(e.get("playerName"))),
                            key=lambda e: e.get("minutesAgo", 999))
                e = pe[0] if pe else None
                if e:
                    dur = int(e.get("duration") or 0)
                    mago = e.get("minutesAgo", 999)
                    fresh = (mago * 60 <= dur + 90) if dur > 0 else (mago <= 10)
                    if fresh:
                        chosen = {"playing": True, "pos": mago * 60, "length": dur,
                                  "title": e.get("title") or "Unknown",
                                  "artist": e.get("artist", ""), "album": e.get("album", "")}

            if chosen and chosen["title"]:
                title, artist, album = chosen["title"], chosen["artist"], chosen["album"]
                playing, length = chosen["playing"], chosen["length"]
                start = int(time.time()) - int(chosen["pos"]) if playing else 0
                cover_id = next((e.get("coverArt") for e in sub
                                 if (e.get("title") or "") == title and e.get("coverArt")), None)
                sig = (title, artist, album)
                push_now = (sig != last_sig                          # new track
                            or playing != last_playing               # play<->pause
                            or (playing and abs(start - last_start) > SEEK))  # seek
                if push_now:
                    push(rpc, title, artist, album, start, length, playing, cover_id)
                    last_sig, last_playing, last_start = sig, playing, start
            elif last_sig is not None:
                rpc.clear()
                last_sig = last_playing = None

            # Yield signal for homelab-presence (heartbeat hides while music shows).
            try:
                if chosen and chosen["title"]:
                    with open(NOWPLAYING, "w") as _f:
                        _f.write(chosen["title"])
                elif os.path.exists(NOWPLAYING):
                    os.remove(NOWPLAYING)
            except OSError:
                pass
        except Exception:
            connected = False
            time.sleep(15)
            continue
        time.sleep(POLL)

if __name__ == "__main__":
    main()
