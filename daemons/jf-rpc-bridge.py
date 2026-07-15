#!/usr/bin/env python3
"""Push Jellyfin /Sessions state to anime_rpc's webserver over WS.

anime_rpc runs the Discord Social-SDK presence side; we feed it State
dicts so the activity title can be the show name (StatusDisplayType.Details).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

import aiohttp

JF_URL      = os.environ.get("JF_URL",      "http://jellyfin.local:8096")
JF_PUBLIC   = os.environ.get("JF_PUBLIC",   "https://jellyfin.example.com")
# Discord-reachable cover proxy (/?jf=<itemId>). Jellyfin is
# private-network-only, so posters must be served through this public HTTPS proxy.
JF_COVER    = os.environ.get("JF_COVER_BASE", "https://cover-proxy.example.com")
TMDB_KEY    = os.environ.get("TMDB_KEY",    "")   # set in override.conf (kept out of git)
TMDB_BASE   = "https://api.themoviedb.org/3"
TMDB_IMG    = "https://image.tmdb.org/t/p/w500"
JF_API_KEY  = os.environ.get("JF_API_KEY",  "")   # set in override.conf (kept out of git)
JF_USERNAME = os.environ.get("JF_USERNAME", "your-jellyfin-user")
RPC_WS      = os.environ.get("RPC_WS",      "ws://127.0.0.1:56727/ws")
ORIGIN      = "jellyfin"
APP_ID      = int(os.environ.get("DISCORD_APP_ID", "0"))  # your Discord application id
POLL_INTERVAL = 5.0

# Signal file other daemons watch to yield to an active Jellyfin watch
# (homelab-presence hides its heartbeat while this is fresh). Rewritten every
# poll while playing so mtime stays current; a stale file = bridge stopped.
NOWPLAYING = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "jf-nowplaying")

# anime_rpc.states.WatchingState
ST_STOPPED, ST_PAUSED, ST_PLAYING = 0, 1, 2

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jf-bridge")

JIKAN_BASE = "https://api.jikan.moe/v4"
_JIKAN_CACHE: dict[str, int | None] = {}  # title → mal_id (None = miss, cached as negative)
_JIKAN_LOCK = asyncio.Lock()
_JIKAN_LAST = 0.0  # monotonic

_TMDB_CACHE: dict[str, str | None] = {}   # "tv:1234" / "tvdb:1234" → poster URL


async def tmdb_poster(http: aiohttp.ClientSession, tmdb_id: str, kind: str) -> str | None:
    if not (TMDB_KEY and tmdb_id):
        return None
    key = f"{kind}:{tmdb_id}"
    if key in _TMDB_CACHE:
        return _TMDB_CACHE[key]
    url = f"{TMDB_BASE}/{kind}/{tmdb_id}"
    try:
        async with http.get(url, params={"api_key": TMDB_KEY}, timeout=8) as r:
            if r.status != 200:
                _TMDB_CACHE[key] = None
                return None
            data = await r.json()
        path = data.get("poster_path")
        out = f"{TMDB_IMG}{path}" if path else None
        _TMDB_CACHE[key] = out
        return out
    except Exception as e:
        log.warning("tmdb lookup failed for %s: %s", key, e)
        _TMDB_CACHE[key] = None
        return None


async def tmdb_poster_by_tvdb(http: aiohttp.ClientSession, tvdb_id: str) -> str | None:
    """Resolve a TVDB id → TMDB id via /find, then fetch poster."""
    if not (TMDB_KEY and tvdb_id):
        return None
    key = f"tvdb:{tvdb_id}"
    if key in _TMDB_CACHE:
        return _TMDB_CACHE[key]
    url = f"{TMDB_BASE}/find/{tvdb_id}"
    try:
        async with http.get(url, params={"api_key": TMDB_KEY,
                                         "external_source": "tvdb_id"},
                            timeout=8) as r:
            if r.status != 200:
                _TMDB_CACHE[key] = None
                return None
            data = await r.json()
        tv = (data.get("tv_results") or [])
        movie = (data.get("movie_results") or [])
        ep = (data.get("tv_episode_results") or [])
        # episode-level tvdb id resolves to an Episode result; we want
        # the parent series. tv_episode_results carry show_id.
        if tv:
            path = tv[0].get("poster_path")
        elif ep:
            show_id = ep[0].get("show_id")
            poster = await tmdb_poster(http, str(show_id), "tv") if show_id else None
            _TMDB_CACHE[key] = poster
            return poster
        elif movie:
            path = movie[0].get("poster_path")
        else:
            _TMDB_CACHE[key] = None
            return None
        out = f"{TMDB_IMG}{path}" if path else None
        _TMDB_CACHE[key] = out
        return out
    except Exception as e:
        log.warning("tmdb /find tvdb=%s failed: %s", tvdb_id, e)
        _TMDB_CACHE[key] = None
        return None


async def jikan_lookup_mal(http: aiohttp.ClientSession, title: str) -> int | None:
    if not title:
        return None
    if title in _JIKAN_CACHE:
        return _JIKAN_CACHE[title]
    async with _JIKAN_LOCK:
        global _JIKAN_LAST
        wait = 0.4 - (asyncio.get_event_loop().time() - _JIKAN_LAST)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            params = {"q": title, "limit": 1}
            async with http.get(f"{JIKAN_BASE}/anime", params=params, timeout=8) as r:
                if r.status != 200:
                    _JIKAN_CACHE[title] = None
                    return None
                data = await r.json()
                results = data.get("data") or []
                mal_id = (results[0].get("mal_id") if results else None)
                _JIKAN_CACHE[title] = mal_id
                _JIKAN_LAST = asyncio.get_event_loop().time()
                return mal_id
        except Exception as e:
            log.warning("jikan lookup failed for %r: %s", title, e)
            _JIKAN_CACHE[title] = None
            return None


def jf_image_url(item: dict) -> str | None:
    """Discord-reachable poster URL for the now-playing item.

    Jellyfin already holds the correct artwork (Shoko/arr matched it), so
    this beats TMDB/MAL scraping: no external API, no rate limits, no
    wrong-title hits. But Jellyfin is private-network-only and Discord fetches the
    URL from the public internet, so we hand out the cover proxy
    (`/?jf=<itemId>`) which proxies the Primary image over the private network.

    Episodes use the SERIES poster — an episode's own Primary is a
    screenshot and is usually absent anyway (ImageTags empty). We only emit
    a URL when the art is known to exist (tag present), so the proxy never
    502s and TMDB fallback can take over instead.
    """
    typ = item.get("Type")
    if typ == "Episode":
        sid = item.get("SeriesId")
        if sid and item.get("SeriesPrimaryImageTag"):
            return f"{JF_COVER}/?jf={sid}"
    iid = item.get("Id")
    if iid and (item.get("ImageTags") or {}).get("Primary"):
        return f"{JF_COVER}/?jf={iid}"
    return None


_ANIME_PROVIDERS = {"AniList", "AniDB", "MyAnimeList", "Mal", "Shoko Series", "Shoko File"}


def _is_anime(item: dict) -> bool:
    """Treat the item as anime only if a Shokofin/AniDB/AniList/MAL
    provider id is set on the item or its series, OR genres contain
    'anime'. Otherwise we should NOT do Jikan fallback (Family Guy etc).
    """
    pids = item.get("ProviderIds") or {}
    if any(k in pids for k in _ANIME_PROVIDERS):
        return True
    series_pids = item.get("SeriesProviderIds") or {}
    if any(k in series_pids for k in _ANIME_PROVIDERS):
        return True
    genres = [g.lower() for g in (item.get("Genres") or [])]
    if "anime" in genres:
        return True
    return False


def build_state(item: dict, play: dict) -> dict:
    """Map a JF NowPlayingItem + PlayState → anime_rpc State dict."""
    typ = item.get("Type")
    if typ == "Episode":
        series = item.get("SeriesName") or item.get("Name") or "Unknown"
        s = item.get("ParentIndexNumber")
        e = item.get("IndexNumber")
        title = series
        episode = (
            f"S{s:02d}E{e:02d}" if s is not None and e is not None
            else f"{e:02d}" if e is not None else "?"
        )
        ep_title = item.get("Name") or ""
    elif typ in ("Movie", "Video"):
        title = item.get("Name") or "Unknown"
        episode = "Movie"
        ep_title = ""
    else:
        title = item.get("Name") or "Unknown"
        episode = item.get("Type") or "—"
        ep_title = ""

    pos_ticks = play.get("PositionTicks") or 0
    dur_ticks = item.get("RunTimeTicks") or 0
    paused    = bool(play.get("IsPaused"))

    state: dict = {
        "origin": ORIGIN,
        "title": title,
        "episode": episode,
        "position": int(pos_ticks // 10_000),     # ticks → ms
        "duration": int(dur_ticks // 10_000),
        "watching_state": ST_PAUSED if paused else ST_PLAYING,
        "rewatching": False,
        "display_name": "Jellyfin",
        "application_id": APP_ID,
    }
    if ep_title:
        state["episode_title"] = ep_title

    # Cover: Jellyfin's own poster, primary source for every item. An
    # explicit image_url overrides anime_rpc's MAL scrape, so this covers
    # anime and non-anime alike. TMDB stays only as a run-loop fallback
    # for the rare item Jellyfin has no artwork for.
    jf_img = jf_image_url(item)
    if jf_img:
        state["image_url"] = jf_img

    # If Shokofin populates a MAL URL we hand it off — anime_rpc's MAL
    # provider then auto-fetches episode titles (and cover, when no
    # image_url above wins).
    pids = item.get("ProviderIds") or {}
    mal = pids.get("MyAnimeList") or pids.get("Mal")
    if mal:
        state["url"] = f"https://myanimelist.net/anime/{mal}"
        state["url_text"] = "MyAnimeList"
    state["_is_anime"] = _is_anime(item)

    # Stash provider hints for non-anime TMDB lookup in the run loop.
    series_pids = item.get("SeriesProviderIds") or {}
    tmdb_id = pids.get("Tmdb") or series_pids.get("Tmdb")
    if tmdb_id:
        state["_tmdb_id"] = str(tmdb_id)
        state["_tmdb_kind"] = "movie" if typ in ("Movie", "Video") else "tv"
    tvdb_id = pids.get("Tvdb") or series_pids.get("Tvdb")
    if tvdb_id:
        state["_tvdb_id"] = str(tvdb_id)
    # Surface TVDB / TMDB url_text when present (cosmetic)
    tvdb = pids.get("Tvdb")
    tmdb = pids.get("Tmdb")
    if "url" not in state:
        if tvdb:
            state["url"] = f"https://www.thetvdb.com/dereferrer/series/{tvdb}"
            state["url_text"] = "TheTVDB"
        elif tmdb:
            state["url"] = f"https://www.themoviedb.org/tv/{tmdb}"
            state["url_text"] = "TheMovieDB"
    return state


async def fetch_session(session: aiohttp.ClientSession) -> dict | None:
    headers = {"X-Emby-Token": JF_API_KEY}
    async with session.get(f"{JF_URL}/Sessions", headers=headers, timeout=8) as r:
        r.raise_for_status()
        sessions = await r.json()
    for s in sessions:
        if s.get("UserName") != JF_USERNAME:
            continue
        if not s.get("NowPlayingItem"):
            continue
        return s
    return None


async def run() -> None:
    last_sent: dict | None = None
    log.info("connecting to %s", RPC_WS)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=None)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        while True:
            try:
                async with http.ws_connect(RPC_WS) as ws:
                    log.info("ws connected")
                    while True:
                        try:
                            sess = await fetch_session(http)
                        except Exception:
                            log.exception("fetch_session failed")
                            sess = None

                        if sess:
                            state = build_state(sess["NowPlayingItem"], sess.get("PlayState") or {})
                            is_anime = state.pop("_is_anime", False)
                            tmdb_id = state.pop("_tmdb_id", None)
                            tmdb_kind = state.pop("_tmdb_kind", "tv")
                            tvdb_id = state.pop("_tvdb_id", None)
                            if is_anime and "url" not in state:
                                mal_id = await jikan_lookup_mal(http, state.get("title", ""))
                                if mal_id:
                                    state["url"] = f"https://myanimelist.net/anime/{mal_id}"
                                    state["url_text"] = "MyAnimeList"
                            # Fallback cover only when Jellyfin had no
                            # poster: non-anime → TMDB via public CDN.
                            # Prefer direct Tmdb id, fall back to Tvdb→Tmdb.
                            if not is_anime and "image_url" not in state:
                                poster = None
                                if tmdb_id:
                                    poster = await tmdb_poster(http, tmdb_id, tmdb_kind)
                                if not poster and tvdb_id:
                                    poster = await tmdb_poster_by_tvdb(http, tvdb_id)
                                if poster:
                                    state["image_url"] = poster
                        else:
                            state = {"origin": ORIGIN}  # clear

                        # Yield signal for homelab-presence: fresh mtime while a
                        # Jellyfin item is playing/paused, removed when stopped.
                        try:
                            if sess:
                                with open(NOWPLAYING, "w") as _f:
                                    _f.write(state.get("title", "playing"))
                            elif os.path.exists(NOWPLAYING):
                                os.remove(NOWPLAYING)
                        except OSError:
                            pass

                        if state != last_sent:
                            await ws.send_json(state)
                            log.info("→ %s", json.dumps({k: v for k, v in state.items() if k != "position"}))
                            last_sent = state
                        else:
                            await ws.send_str("keepalive")

                        await asyncio.sleep(POLL_INTERVAL)
            except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionResetError) as e:
                log.warning("ws disconnect: %s — retry in 5s", e)
                last_sent = None
                await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.exit(0)
