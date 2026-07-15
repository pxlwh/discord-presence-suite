# discord-presence-suite

A family of token-free Discord rich-presence daemons. Each one drives the
**local desktop Discord client** over its IPC socket (via
[pypresence](https://github.com/qwertyquerty/pypresence)), exactly the way a
game's own Rich Presence does. No bot token, no OAuth, no account credentials:
presence only shows while Discord is running on your machine.

Four daemons, four sources:

| Daemon | Shows | How |
|---|---|---|
| `steam-presence.py` | Steam games as **Playing** | Scans `/proc` for processes under `steamapps/common/`, then sets the game's *official* Discord app id, looked up by name in Discord's own detectable-games DB |
| `nd-rpc-bridge.py` | Navidrome / music as **Listening** | Three source tiers: desktop MPRIS (`playerctl`), phone over KDE Connect (`jeepney`/D-Bus), and Subsonic `getNowPlaying` fallback. Cover art from iTunes / Deezer / MusicBrainz |
| `homelab-presence.py` | An idle homelab **heartbeat** | Rotating, healthy-or-silent stats pulled from Prometheus; yields to games and media so it only shows when you are genuinely idle |
| `jf-rpc-bridge.py` | Jellyfin now-playing | Reads Jellyfin's `/Sessions` API and forwards state to a presence renderer over WebSocket, with TMDB / Jikan cover + metadata lookups |

## Why token-free matters

Most custom-RPC tools ask for a bot token or your account token. These do not.
They talk to the Discord client that is already running and authenticated on your
desktop, the same private IPC interface games use. Nothing leaves your machine
except the presence Discord itself publishes, and it stops the moment you close
Discord.

## Design notes worth a look

- **steam-presence** exists because Discord's Linux game detection is threadbare:
  its detection DB ships native-Linux executables for only a handful of titles and
  empty exe lists for most modern ones. This daemon does the detection itself and
  borrows the publisher's official app id so the game shows with its real name and
  icon, zero per-game config. It stands down for Proton titles Discord *can* detect,
  to avoid a double activity.
- **nd-rpc-bridge** merges three unreliable sources into one clean presence, with
  staleness tracking so a closed/paused player is dropped instead of showing a
  frozen track.
- **homelab-presence** is *healthy-or-silent*: any degraded signal clears the card
  entirely, so a public profile never doubles as a status page of what is currently
  weak. It yields to the other daemons via a small `*-nowplaying` signal file.
- **jf-rpc-bridge** is the outlier: rather than driving pypresence directly, it reads
  Jellyfin's `/Sessions` API and forwards now-playing state to a presence renderer
  over WebSocket. It resolves cover art from Jellyfin's own library first (proxied
  through a public URL, since Jellyfin is usually private), falls back to TMDB then
  Jikan / MyAnimeList, and detects anime vs non-anime to pick the right metadata source.
- The three pypresence daemons (steam, nd-rpc, homelab) handle the pitfall where a
  Discord client restart wedges the IPC pipe: they skip the blocking clear on a dead
  pipe and rebuild a fresh event loop per reconnect.

## Layout

```
daemons/   the four presence scripts
systemd/   user services (uv-based ExecStart, EnvironmentFile config)
env/       *.env.example templates (copy to ~/.config/<name>.env and fill in)
```

## Install (per daemon)

```sh
# 1. drop the script in place
install -m 0755 daemons/homelab-presence.py ~/.local/bin/

# 2. configure it
cp env/homelab-presence.env.example ~/.config/homelab-presence.env
$EDITOR ~/.config/homelab-presence.env        # fill in your Discord app id, etc.

# 3. run it as a user service
cp systemd/homelab-presence.service ~/.config/systemd/user/
systemctl --user enable --now homelab-presence.service
```

Each script also runs standalone for testing, e.g.
`uv run --with pypresence --with psutil daemons/steam-presence.py --once`.

## Requirements

- [`uv`](https://github.com/astral-sh/uv) (the services run scripts through it)
- `pypresence` (all), `psutil` (steam), `jeepney` (nd, for KDE Connect), `aiohttp` (jf)
- a Discord application id you own, for each daemon that needs one
- source-specific: Steam, a Navidrome/Subsonic server, Prometheus, or Jellyfin

## A note on the placeholders

These are lightly genericized from a personal setup. Values like `example.com`,
`pve`, and the `instance="pve"` Prometheus filters are placeholders. Swap in your
own hosts, node labels, and Discord app ids.

## License

MIT, see [LICENSE](LICENSE).
