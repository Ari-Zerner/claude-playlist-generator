# Playlist Generator

Builds themed Spotify playlists with Claude Code. Each playlist is a directory under
`playlists/` holding a brief and a plain-text `Playlist.txt`; a small Python tool creates the
playlist on Spotify and syncs it both ways. Suggestions are steered by a taste reference that
is pulled from one of your own playlists and weighted towards what you added recently.

## First-time setup on a new machine

The quickest route: clone (or unzip) this repo, open [Claude Code](https://claude.com/claude-code)
in it, and say "set me up". It walks you through the steps below. Doing it by hand:

### 1. Get the code and install dependencies

```bash
git clone https://github.com/Ari-Zerner/claude-playlist-generator.git
cd claude-playlist-generator
python3 -m pip install -r spotify-tool/requirements.txt
```

The only dependency is `requests`. On Debian/Ubuntu the pip command above is refused by
[PEP 668](https://peps.python.org/pep-0668/), which protects the system Python. There it is
usually already satisfied — check with `python3 -c "import requests"` — and if not, prefer:

```bash
sudo apt install python3-requests   # or, to keep it isolated:
python3 -m venv .venv && . .venv/bin/activate && pip install -r spotify-tool/requirements.txt
```

Avoid `--break-system-packages`; it does what the name says.

### 2. Create a Spotify developer app

1. Go to https://developer.spotify.com/dashboard and click **Create app**.
2. Name it anything (e.g. "Playlist Tool").
3. Under **Redirect URIs**, add exactly:
   ```
   http://127.0.0.1:8888/callback
   ```
4. Check **Web API** as the API you're using, then save.
5. Open the app's **Settings** to copy the **Client ID** and **Client secret**.

### 3. Configure credentials

```bash
cp spotify-tool/.env.example spotify-tool/.env
```

Fill in `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`. Optionally set `TASTE_PLAYLIST` to
a playlist of yours that best represents your taste; without it, suggestions are steered by
your Spotify listening history alone.

Optionally add `LASTFM_API_KEY` for the `similar` and `tag` commands. Create a free key at
https://www.last.fm/api/account/create (any application name; the callback URL can be left
empty) and copy the **API key**. The shared secret is not used.

`.env` and the generated `token.json` are gitignored — they hold secrets and must never be
committed.

### 4. Authenticate

```bash
python3 spotify-tool/app.py auth
```

This opens a browser for the Spotify login, then writes `spotify-tool/token.json`. It refreshes
itself from then on; re-run `auth` only if refreshing starts failing, or if `taste` reports
that listening history was skipped: a login made before that feature existed lacks the
`user-top-read` and `user-read-recently-played` permissions it needs.

**On a headless server** there's no browser, and nothing needs to reach the server:

```bash
python3 spotify-tool/app.py auth --print-url
```

Open the printed URL on any device and approve. The browser is then sent to
`http://127.0.0.1:8888/callback?code=...`, which fails to load — that's expected. Copy that
whole address from the address bar and hand it back:

```bash
python3 spotify-tool/app.py auth 'http://127.0.0.1:8888/callback?code=...'
```

The code in it is single-use and expires within minutes, so do the two steps back to back.

## Starting a new playlist

```bash
python3 spotify-tool/app.py new "Road Trip"
```

That copies `template/` to `playlists/road-trip/` and creates a private playlist on Spotify
named "(Claude) Road Trip". Then describe the playlist in `playlists/road-trip/BRIEF.md`, fill
`Playlist.txt`, and `push`. In practice you just tell Claude Code what playlist you want and it
does all four.

- `--no-prefix` names it exactly as given, without "(Claude) ".
- `--public` makes it public.
- `new "Road Trip" <playlist URL>` adopts a playlist that already exists instead of creating one.

## Commands

All take the form `python3 spotify-tool/app.py <command>`. `<name>` is a directory under
`playlists/`; leave it out when there is only one playlist or you are inside its directory.

| Command | What it does |
| --- | --- |
| `new "Name" [URL]` | New playlist from `template/`, created on Spotify (or adopted from URL). |
| `list` | Every playlist with its track count and Spotify link. |
| `check [name]` | Dry run of `push`: each line's Spotify match, durations, total runtime, suspicious matches. |
| `push [name]` | Replaces the Spotify playlist with `Playlist.txt`. |
| `pull [name]` | Pulls the Spotify playlist back into `Playlist.txt`. |
| `export <URL>` | Prints the tracks of any playlist you own or collaborate on. |
| `taste [--if-stale]` | Rebuilds `taste/` from play statistics and, if set, `TASTE_PLAYLIST` and past playlists. |
| `similar "Artist"` | Last.fm: similar artists and the artist's tags. |
| `similar "Artist - Title"` | Last.fm: similar tracks. |
| `tag "celtic punk"` | Last.fm: top artists and tracks for a tag. |
| `artist "Artist" [--top] [--groups G]` | Spotify: the artist's top tracks and every song on their albums and singles, as `Artist - Title` lines. |
| `auth` | One-time OAuth login; saves `token.json`. |

`push` searches Spotify for each line. When the strict search finds nothing it retries with a
loose one and accepts a result only if the artist matches; among several results it prefers
the one that is not a remix, live cut or other variant. It reports anything it could not
find or is unsure about. `check` does the same search without touching Spotify and adds each
match's album, year and duration and the total runtime, so wrong versions and a playlist
that runs long or short are caught before pushing. A line that resolves to a track already
in the list is flagged and pushed only once.

`similar`, `tag` and `artist` mark results that are already in the taste playlist. `artist`
lists each song once, under the earliest album it is on (so singles, deluxe editions and
re-recordings don't repeat it); `--groups album,single,compilation,appears_on` widens what it
reads, `--top` stops after the top tracks. It needs no Last.fm key.

## Taste reference

`TASTE_PLAYLIST` is optional. When it is set, `taste` reads it along with the date each track
was added, and writes:

- `taste/Tracks.txt` — every track, dated, newest first, grouped by age.
- `taste/Profile.md` — artists ranked by recency-weighted score, and the newest additions.

A track's weight is `0.5 ^ (days since added / TASTE_HALF_LIFE_DAYS)`, so with the default of
365 something added a year ago counts half as much as something added today.

The same run writes two more files:

- `taste/Listening.md` — Spotify's top artists and tracks for the last 4 weeks, 6 months and
  year, plus the last 50 plays. Tracks played a lot but absent from the taste playlist are
  marked.
- `taste/Hits.md` — for each playlist under `playlists/`, which tracks are in the taste
  playlist: **hits** (added on or after the day the playlist was created), already known, and
  not picked up. It is the feedback loop on past suggestions.

Without `TASTE_PLAYLIST`, `taste` writes only `taste/Listening.md`.

It stays current on its own: `.claude/settings.json` has a `SessionStart` hook that runs
`taste --if-stale` whenever a Claude Code session starts here, re-syncing if the copy is older
than `TASTE_MAX_AGE_HOURS`. `taste/` is generated, so it is gitignored.

## Files

| Path | Purpose |
| --- | --- |
| `template/` | What `new` copies: an empty `BRIEF.md` and `Playlist.txt`. |
| `playlists/<name>/BRIEF.md` | Goal and vibe for that playlist, and the description `push` sets on Spotify. |
| `playlists/<name>/Playlist.txt` | The working playlist, one `Artist - Title` per line. |
| `playlists/<name>/Playlist_before_edits.txt` | Snapshot taken before each batch of additions, so removals are visible in a diff. |
| `playlists/<name>/playlist.json` | The Spotify playlist it syncs with. Written by the tool. |
| `taste/` | Generated taste reference. |
| `CLAUDE.md` | Workflow and curation rules that Claude Code follows. |
| `spotify-tool/app.py` | The tool. |

## Configuration reference

Set in `spotify-tool/.env`.

| Variable | Required | Meaning |
| --- | --- | --- |
| `SPOTIFY_CLIENT_ID` | yes | Spotify developer app client ID. |
| `SPOTIFY_CLIENT_SECRET` | yes | Spotify developer app client secret. |
| `TASTE_PLAYLIST` | no | URL or ID of the playlist mirrored into `taste/`. |
| `TASTE_HALF_LIFE_DAYS` | no | Days for a track's taste weight to halve. Default 365. |
| `TASTE_MAX_AGE_HOURS` | no | How old `taste/` may get before `taste --if-stale` re-syncs. Default 24. |
| `LASTFM_API_KEY` | for `similar`, `tag` | Last.fm API key. |

## Requirements

Python 3.8+ and the `requests` library (`spotify-tool/requirements.txt`).

## License

MIT. See `LICENSE`.
