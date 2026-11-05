# Playlist Generator

Builds themed Spotify playlists with Claude Code. One repo holds every playlist; the tool and
the Spotify login are shared.

## Layout

| Path | What |
|---|---|
| `template/` | The template. `new` copies it; never curate in here. |
| `playlists/<name>/` | One directory per playlist: `BRIEF.md` (goal, vibe and Spotify description), `Playlist.txt`, `Playlist_before_edits.txt`, `playlist.json` (Spotify link; managed by the tool). |
| `taste/` | Taste reference, generated from the user's play statistics and, if one is set (`TASTE_PLAYLIST` in `spotify-tool/.env`, optional), a taste playlist and past playlists. Not tracked in git. |
| `spotify-tool/app.py` | The sync tool. Run it as `python3 spotify-tool/app.py <command>`. |

## First run

If `spotify-tool/.env` or `spotify-tool/token.json` is missing, this is a new setup: walk the
user through `README.md`'s setup (install, Spotify developer app, `.env`, `auth`) one step at a
time before anything else, doing the parts you can yourself. Ask whether one of their playlists
represents their taste well, for `TASTE_PLAYLIST`; it is optional, and without one the taste
reference is their listening history alone. Then run `taste`.

## Making a new playlist

When the user asks for a playlist, do all of this without being asked for each step:

1. `python3 spotify-tool/app.py new "Name"` — copies `template/` to `playlists/<name>/` and
   creates the playlist on Spotify (private).
   - On Spotify the playlist is named **"(Claude) Name"**. That prefix is the default; pass
     `--no-prefix` only when the user asks for a name without it or gives an exact name to use.
     Pass the name without the prefix; the directory and brief never carry it.
   - To take over a playlist that already exists on Spotify, add its URL: `new "Name" <URL>`.
2. Write the **Goal** and **Vibe** in `playlists/<name>/BRIEF.md` from what the user said. Ask
   only if the request leaves the vibe really open. Also write the **Description**: one or two
   sentences, at most 300 characters, that `push` puts under the title on Spotify.
3. Read `taste/Profile.md`, `taste/Listening.md` and `taste/Hits.md` (whichever exist), then draft
   `Playlist.txt` (one song per line, `Artist - Title`).
4. `python3 spotify-tool/app.py check <name>` and fix what it shows: lines not found, matches
   flagged as another artist, title or recording, and a total runtime outside what was asked
   for. Repeat until it is clean.
5. `python3 spotify-tool/app.py push <name>` and give the user the Spotify link.

## Taste reference

`taste/` mirrors the user's taste playlist and is the source of truth for what they
actually like. Suggestions that line up with it land far more often than generic ones.

The taste playlist is optional. Without one (`TASTE_PLAYLIST` unset), `taste/` holds only
`Listening.md`: lean on its top artists and tracks, and on what the user tells you.

- `taste/Profile.md` — read this first. Artists ranked by recency-weighted score, plus the
  newest additions.
- `taste/Tracks.txt` — every track with the date it was added, newest first, grouped
  by age. Grep it to check whether the user already knows a song or artist.

- `taste/Listening.md` — what is actually played: Spotify's top artists and tracks over the
  last 4 weeks, 6 months and year, and the last 50 plays. The taste playlist says what
  was saved; this says what is on repeat right now.
- `taste/Hits.md` — for each playlist made here, which tracks the user later added to
  the taste playlist. Hits are the best evidence of which kinds of suggestion land, so favour
  more like them. "Not picked up" is weak evidence; it is not a removal.

**Recent additions count for more.** A track's weight halves for every year since it was added
(`TASTE_HALF_LIFE_DAYS`), so lean on the top of the list and the top of the artist table: they
show where the user's taste is now. Older tracks still say what they like, just more quietly.

It refreshes itself: a `SessionStart` hook (`.claude/settings.json`) runs `taste --if-stale`,
which re-syncs from Spotify when the copy is more than a day old and reports what changed. To
force it, run `python3 spotify-tool/app.py taste`. If the hook reports that it could not
refresh, tell the user rather than quietly curating from a stale copy. A
`taste/Tracks.txt` with no dates on its lines is the old hand-made snapshot from
before syncing worked: usable for what they like, but it says nothing about recency.

## Finding music beyond what you already know

Last.fm's listener data covers what reading about music cannot: which tracks and artists
people who like X also play. Use it to widen a draft, especially for niche genres.

- `python3 spotify-tool/app.py similar "Artist"` — similar artists, with the artist's tags.
- `python3 spotify-tool/app.py similar "Artist - Title"` — similar tracks.
- `python3 spotify-tool/app.py tag "celtic punk"` — top artists and tracks for a tag.
- `python3 spotify-tool/app.py artist "Artist"` — Spotify: the artist's top tracks and whole
  catalog with album, year and duration. For niche genres, where guessed titles mostly miss,
  pick real titles from this instead of from memory (`--top` for just the top tracks).

Results already in the taste playlist are marked, so the unmarked ones are the discoveries.
Treat the output as leads to judge against the brief, not as a list to paste in. For music
released recently, search the web. `similar` and `tag` need `LASTFM_API_KEY` in
`spotify-tool/.env`; if it is missing, say so and carry on without. `artist` needs only Spotify.

## Editing a playlist

- Edit `playlists/<name>/Playlist.txt` directly. Add songs in batches, keeping to the brief.
- If the user may have edited the playlist in Spotify, `pull` first.
- Before adding a batch, copy `Playlist.txt` to `Playlist_before_edits.txt` in the same
  directory so the user's removals show up in a diff.
- Commands (the playlist name can be dropped when there is only one playlist):
  - `python3 spotify-tool/app.py check <name>` — dry run of `push`: what each line matches,
    durations, total runtime, and suspicious matches. Changes nothing. Run it before `push`.
  - `python3 spotify-tool/app.py push <name>` — replaces the Spotify playlist with `Playlist.txt`
    and sets its description from the Description in `BRIEF.md` (left alone if there is none).
  - `python3 spotify-tool/app.py pull <name>` — pulls the Spotify playlist back into `Playlist.txt`.
  - `python3 spotify-tool/app.py list` — every playlist with its track count and link.
  - `python3 spotify-tool/app.py export <URL>` — prints the tracks of any playlist the user owns.

## Curation rules
- **Never re-add a song the user has removed.** Removals are deliberate taste decisions.
  Before adding any song, check it against both the current `Playlist.txt` AND
  `Playlist_before_edits.txt`, and `git log -p` on the playlist file where it is tracked. If a
  song was there before and isn't now, it was cut — leave it out.
