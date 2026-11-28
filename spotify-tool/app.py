"""
Spotify <-> Text playlist sync tool.

Usage (run from anywhere; PLAYLIST is a directory name under playlists/):
  python3 app.py new "Name" [URL]   # New playlist from template/. Creates it on Spotify
                                    # as "(Claude) Name" (--no-prefix for just "Name"),
                                    # or adopts the existing playlist at URL.
  python3 app.py list               # Show playlists and their Spotify links
  python3 app.py check [PLAYLIST]   # Dry run: what each line matches on Spotify, with
                                    # durations, total runtime and suspicious matches
  python3 app.py push [PLAYLIST]    # Push Playlist.txt -> Spotify (replaces contents),
                                    # and the Description in BRIEF.md
  python3 app.py pull [PLAYLIST]    # Pull Spotify -> Playlist.txt
  python3 app.py export URL         # Print any playlist you own to stdout
  python3 app.py taste [--if-stale] # Refresh taste/ from the taste-reference playlist,
                                    # plus listening history and playlist hits
  python3 app.py similar "Artist"   # Last.fm: similar artists and tags...
  python3 app.py similar "Artist - Title"   # ...or similar tracks
  python3 app.py tag "celtic punk"  # Last.fm: top artists and tracks for a tag
  python3 app.py artist "Artist"    # Spotify: the artist's top tracks and whole catalog
                                    # (--top for top tracks only, --groups to change
                                    # which releases: album,single,compilation,appears_on)
  python3 app.py mix [PLAYLIST]     # Rebuild a mix from the taste playlist: recent,
                                    # much-played adds plus old favourites, shuffled
                                    # (--size 80, --old-share 0.3, --dry)
  python3 app.py auth               # One-time OAuth login, saves token
  python3 app.py auth --print-url   # Headless: just print the login URL...
  python3 app.py auth REDIRECT_URL  # ...then paste the URL the browser was sent to

PLAYLIST may be omitted when there is only one playlist, or when the current
directory is inside one.

Configuration (via .env or environment):
  SPOTIFY_CLIENT_ID        Required. Spotify developer app client ID.
  SPOTIFY_CLIENT_SECRET    Required. Spotify developer app client secret.
  TASTE_PLAYLIST           Optional. URL or ID of the playlist `taste` mirrors into taste/.
                           Without it, taste/ holds only listening history.
  TASTE_HALF_LIFE_DAYS     Optional. Age at which a track's taste weight halves (default 365).
  TASTE_MAX_AGE_HOURS      Optional. `taste --if-stale` re-syncs after this long (default 24).
  LASTFM_API_KEY           For `similar` and `tag`. Free: https://www.last.fm/api/account/create
"""

import argparse
import base64
import collections
import datetime
import http.server
import json
import os
import re
import shutil
import sys
import threading
import time
import unicodedata
import webbrowser
from urllib.parse import parse_qs, quote, urlparse

import requests

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOL_DIR)

# Load .env
_env_path = os.path.join(TOOL_DIR, ".env")
if os.path.exists(_env_path):
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


class ToolError(Exception):
    pass


def _require_env(name):
    val = os.environ.get(name)
    if not val:
        raise ToolError(f"Missing required config: {name}. Set it in spotify-tool/.env")
    return val


REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = ("playlist-read-private playlist-modify-public playlist-modify-private "
          "user-top-read user-read-recently-played")
# What `taste` needs for taste/Listening.md; tokens from before these were added lack them.
LISTENING_SCOPES = ("user-top-read", "user-read-recently-played")
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API = "https://api.spotify.com/v1"
LASTFM_API = "https://ws.audioscrobbler.com/2.0/"

TEMPLATE_DIR = os.path.join(ROOT, "template")
PLAYLISTS_DIR = os.path.join(ROOT, "playlists")
TASTE_DIR = os.path.join(ROOT, "taste")
TASTE_TRACKS_FILE = os.path.join(TASTE_DIR, "Tracks.txt")
TASTE_PROFILE_FILE = os.path.join(TASTE_DIR, "Profile.md")
TASTE_HITS_FILE = os.path.join(TASTE_DIR, "Hits.md")
TASTE_LISTENING_FILE = os.path.join(TASTE_DIR, "Listening.md")
TOKEN_FILE = os.path.join(TOOL_DIR, "token.json")

# New playlists are named with this on Spotify unless `new --no-prefix` is given.
NAME_PREFIX = "(Claude) "

# Give up rather than sleep through a rate limit longer than this (seconds).
MAX_RETRY_WAIT = 30


# --------------- Token management ---------------

def save_token(data):
    data["_saved_at"] = time.time()
    with open(TOKEN_FILE, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(TOKEN_FILE, 0o600)


def load_token():
    if not os.path.exists(TOKEN_FILE):
        raise ToolError("No Spotify token. Run: python3 spotify-tool/app.py auth")
    with open(TOKEN_FILE) as f:
        return json.load(f)


def get_access_token():
    token = load_token()
    # Refresh if expired (with 60s buffer)
    elapsed = time.time() - token.get("_saved_at", 0)
    if elapsed >= token.get("expires_in", 3600) - 60:
        token = refresh_token(token)
    return token["access_token"]


def _client_auth_header():
    client_id = _require_env("SPOTIFY_CLIENT_ID")
    client_secret = _require_env("SPOTIFY_CLIENT_SECRET")
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    return {
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/x-www-form-urlencoded",
    }


def refresh_token(token):
    resp = requests.post(SPOTIFY_TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": token["refresh_token"],
    }, headers=_client_auth_header(), timeout=30)
    if resp.status_code != 200:
        raise ToolError(f"Token refresh failed: {resp.text}\n"
                        "Run: python3 spotify-tool/app.py auth")
    data = resp.json()
    # Keep the refresh token if a new one wasn't issued
    if "refresh_token" not in data:
        data["refresh_token"] = token["refresh_token"]
    save_token(data)
    return data


def api(method, path, **kwargs):
    """Call the Spotify API, waiting out short rate limits."""
    url = path if path.startswith("http") else SPOTIFY_API + path
    for _ in range(5):
        resp = requests.request(
            method, url, headers={"Authorization": f"Bearer {get_access_token()}"},
            timeout=30, **kwargs)
        if resp.status_code != 429:
            return resp
        wait = float(resp.headers.get("Retry-After", 2))
        if wait > MAX_RETRY_WAIT:
            raise ToolError(f"Spotify rate limit: asked to wait {wait:.0f}s. Try again later.")
        time.sleep(wait)
    return resp


# --------------- Auth flow ---------------

def auth_url():
    client_id = _require_env("SPOTIFY_CLIENT_ID")
    return (
        "https://accounts.spotify.com/authorize?"
        f"client_id={client_id}&response_type=code"
        f"&redirect_uri={quote(REDIRECT_URI)}&scope={quote(SCOPES)}"
    )


def exchange_code(auth_code):
    resp = requests.post(SPOTIFY_TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": auth_code,
        "redirect_uri": REDIRECT_URI,
    }, headers=_client_auth_header(), timeout=30)
    if resp.status_code != 200:
        raise ToolError(f"Token exchange failed: {resp.text}")
    save_token(resp.json())
    print("Authenticated! Token saved to spotify-tool/token.json")


def do_auth(redirected=None, print_url=False):
    if print_url:
        print(auth_url())
        return

    if redirected:
        # The browser was sent to REDIRECT_URI?code=...; nothing needs to be
        # listening there, the code in the address bar is all we need.
        params = parse_qs(urlparse(redirected).query)
        if "code" in params:
            code = params["code"][0]
        elif "error" in params:
            raise ToolError(f"Spotify refused the login: {params['error'][0]}")
        elif "://" not in redirected:
            code = redirected
        else:
            raise ToolError("No code= parameter in that URL. Paste the full address the "
                            "browser ended up at after approving.")
        exchange_code(code)
        return

    auth_code = None

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal auth_code
            params = parse_qs(urlparse(self.path).query)
            if "code" in params:
                auth_code = params["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h1>Done! You can close this tab.</h1>")
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Auth failed.")
            threading.Thread(target=self.server.shutdown).start()

        def log_message(self, *a):
            pass

    url = auth_url()
    print("Opening browser for Spotify login...")
    print(f"If no browser opens (headless server), open this URL yourself:\n\n{url}\n")
    print("Waiting for the callback on http://127.0.0.1:8888/callback ...")
    print("(If the browser is on another machine, the page will fail to load after you "
          "approve. Stop this and run `auth <the URL from the address bar>` instead.)")
    webbrowser.open(url)

    server = http.server.HTTPServer(("127.0.0.1", 8888), Handler)
    server.serve_forever()

    if not auth_code:
        raise ToolError("Auth failed.")
    exchange_code(auth_code)


# --------------- Playlists on disk ---------------

def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        raise ToolError(f"Can't make a directory name out of {name!r}")
    return slug


def playlist_names():
    if not os.path.isdir(PLAYLISTS_DIR):
        return []
    return sorted(d for d in os.listdir(PLAYLISTS_DIR)
                  if os.path.isfile(os.path.join(PLAYLISTS_DIR, d, "playlist.json")))


def resolve_playlist(name=None):
    """Return the directory of the playlist the command is about."""
    names = playlist_names()
    if name:
        for candidate in (name, slugify(name)):
            if candidate in names:
                return os.path.join(PLAYLISTS_DIR, candidate)
        raise ToolError(f"No playlist named {name!r}. Have: {', '.join(names) or '(none)'}")

    rel = os.path.relpath(os.getcwd(), PLAYLISTS_DIR)
    if not rel.startswith("..") and rel != ".":
        inside = rel.split(os.sep)[0]
        if inside in names:
            return os.path.join(PLAYLISTS_DIR, inside)

    if len(names) == 1:
        return os.path.join(PLAYLISTS_DIR, names[0])
    if not names:
        raise ToolError('No playlists yet. Start one: python3 spotify-tool/app.py new "Name"')
    raise ToolError(f"Which playlist? One of: {', '.join(names)}")


def read_meta(pdir):
    with open(os.path.join(pdir, "playlist.json")) as f:
        return json.load(f)


def write_meta(pdir, meta):
    with open(os.path.join(pdir, "playlist.json"), "w") as f:
        json.dump(meta, f, indent=2)
        f.write("\n")


def playlist_file(pdir):
    return os.path.join(pdir, "Playlist.txt")


# Spotify cuts playlist descriptions off at this length.
DESCRIPTION_MAX = 300


def brief_description(pdir):
    """The "## Description" section of BRIEF.md as one line, or None if missing or TBD."""
    path = os.path.join(pdir, "BRIEF.md")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        m = re.search(r"^## Description\s*\n(.*?)(?=^## |\Z)", f.read(), re.M | re.S)
    text = " ".join(m.group(1).split()) if m else ""
    if not text or text.startswith("TBD"):
        return None
    return text


def description_problem(pdir):
    """Why the brief's description can't go to Spotify as it is; None if it can."""
    text = brief_description(pdir)
    if text is None:
        return "BRIEF.md has no Description, so the Spotify description stays as it is."
    if len(text) > DESCRIPTION_MAX:
        return (f"BRIEF.md's Description is {len(text)} characters; "
                f"Spotify allows {DESCRIPTION_MAX}.")
    return None


def create_spotify_playlist(name, public=False):
    resp = api("POST", "/me/playlists", json={"name": name, "public": public})
    if resp.status_code not in (200, 201):
        raise ToolError(f"Could not create Spotify playlist: {resp.status_code} {resp.text}")
    data = resp.json()
    return data["id"], data.get("external_urls", {}).get("spotify")


def ensure_on_spotify(pdir):
    """Return the playlist's Spotify ID, creating the Spotify playlist if needed."""
    meta = read_meta(pdir)
    if not meta.get("spotify_id"):
        meta["spotify_id"], meta["spotify_url"] = create_spotify_playlist(
            meta.get("spotify_name") or meta["name"], meta.get("public", False))
        write_meta(pdir, meta)
        print(f"Created Spotify playlist: {meta['spotify_url']}")
    return meta["spotify_id"]


def do_new(name, url=None, public=False, prefix=True):
    # The prefix belongs to the Spotify name only, not the directory or the brief.
    if name.startswith(NAME_PREFIX):
        name = name[len(NAME_PREFIX):]
    spotify_name = NAME_PREFIX + name if prefix and not url else name
    slug = slugify(name)
    pdir = os.path.join(PLAYLISTS_DIR, slug)
    if os.path.exists(pdir):
        raise ToolError(f"playlists/{slug} already exists")

    meta = {"name": name, "spotify_name": spotify_name, "spotify_id": None, "spotify_url": None, "public": public,
            "created": datetime.date.today().isoformat()}
    tracks = None
    if url:
        meta["spotify_id"] = extract_playlist_id(url)
        meta["spotify_url"] = f"https://open.spotify.com/playlist/{meta['spotify_id']}"
        tracks = [t["line"] for t in fetch_playlist_items(meta["spotify_id"])]
    elif os.path.exists(TOKEN_FILE):
        meta["spotify_id"], meta["spotify_url"] = create_spotify_playlist(spotify_name, public)

    os.makedirs(PLAYLISTS_DIR, exist_ok=True)
    shutil.copytree(TEMPLATE_DIR, pdir)
    for fname in os.listdir(pdir):
        if fname.endswith(".md"):
            path = os.path.join(pdir, fname)
            with open(path) as f:
                text = f.read()
            with open(path, "w") as f:
                f.write(text.replace("{{name}}", name))
    write_meta(pdir, meta)
    if tracks:
        with open(playlist_file(pdir), "w") as f:
            f.write("\n".join(tracks) + "\n")

    print(f"Created playlists/{slug}/")
    if tracks is not None:
        print(f"  Adopted {meta['spotify_url']} ({len(tracks)} tracks pulled)")
    elif meta["spotify_url"]:
        print(f"  Spotify: \"{spotify_name}\" {meta['spotify_url']}")
    else:
        print("  Not on Spotify yet (no token); the first `push` will create it.")
    print(f"  Next: fill in playlists/{slug}/BRIEF.md, then add tracks to Playlist.txt.")


def do_list():
    names = playlist_names()
    if not names:
        print('No playlists yet. Start one: python3 spotify-tool/app.py new "Name"')
        return
    for slug in names:
        pdir = os.path.join(PLAYLISTS_DIR, slug)
        meta = read_meta(pdir)
        count = len(parse_playlist_file(playlist_file(pdir)))
        print(f"{slug}  ({count} tracks)  {meta.get('spotify_url') or 'not on Spotify yet'}")


# --------------- Pull / export: Spotify -> text ---------------

def extract_playlist_id(url):
    m = re.search(r"playlist[/:]([a-zA-Z0-9]+)", url)
    if m:
        return m.group(1)
    if re.fullmatch(r"[a-zA-Z0-9]{16,}", url):
        return url
    raise ToolError(f"Invalid playlist URL: {url}")


def fetch_playlist_items(pid):
    """Return [{"line": "Artist - Title", "artists": [...], "added_at": str|None}, ...]."""
    items = []
    endpoint = f"/playlists/{pid}/items?limit=50"
    while endpoint:
        resp = api("GET", endpoint)
        if resp.status_code != 200:
            raise ToolError(f"API error: {resp.status_code} {resp.text}")
        data = resp.json()
        for entry in data.get("items", []):
            # "item" since Spotify's February 2026 API changes; "track" before.
            t = entry.get("item") or entry.get("track")
            if t and t.get("name"):
                artists = [a["name"] for a in t.get("artists", [])]
                items.append({
                    "line": f"{', '.join(artists)} - {t['name']}",
                    "artists": artists,
                    "added_at": entry.get("added_at"),
                    "uri": t.get("uri"),
                })
        endpoint = data.get("next")
    return items


def do_pull(name=None):
    pdir = resolve_playlist(name)
    pid = read_meta(pdir).get("spotify_id")
    if not pid:
        raise ToolError("This playlist isn't on Spotify yet; `push` it first.")
    tracks = [t["line"] for t in fetch_playlist_items(pid)]
    with open(playlist_file(pdir), "w") as f:
        f.write("\n".join(tracks) + "\n")
    print(f"Pulled {len(tracks)} tracks into {os.path.relpath(playlist_file(pdir), ROOT)}")


def do_export(url):
    tracks = fetch_playlist_items(extract_playlist_id(url))
    for t in tracks:
        print(t["line"])
    print(f"\n# {len(tracks)} tracks", file=sys.stderr)


# --------------- Push: text -> Spotify ---------------

def parse_playlist_file(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        lines = f.readlines()
    return [l.strip() for l in lines
            if l.strip() and not l.strip().startswith("#") and " - " in l]


# Words in a matched title's "- Live" / "(Remix)" part that mean a different recording.
_VERSION_WORDS = re.compile(
    r"\b(live|remix|remixed|mix|acoustic|instrumental|karaoke|demo|edit|version|cover|"
    r"re-?recorded|sped up|slowed|as it should have sounded)\b", re.I)


def _norm(text):
    """Fold case, accents and punctuation so spellings of the same name compare equal."""
    text = unicodedata.normalize("NFKD", text.replace("&", " and "))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _title_core(title):
    """The title without its "- 2011 Remaster" / "(feat. X)" trimmings."""
    core = re.sub(r"\s+-\s+.*$", "", title)
    core = re.sub(r"\s*[\(\[].*?[\)\]]", "", core)
    return _norm(core) or _norm(title)


def _split_line(line):
    """'A, B - Title' -> ({normalised artists}, normalised core title)."""
    artist, title = line.split(" - ", 1)
    return {_norm(a) for a in artist.split(", ")}, _title_core(title)


def _match_problems(line, track):
    """Why `track` may not be the recording `line` asked for; [] if it looks right."""
    want_artists, want_title = _split_line(line)
    problems = []
    if not want_artists & {_norm(a["name"]) for a in track["artists"]}:
        problems.append("different artist")
    if _title_core(track["name"]) != want_title:
        problems.append("different title")
    extra = track["name"][len(re.sub(r"\s+-\s+.*$|\s*[\(\[].*$", "", track["name"])):]
    word = _VERSION_WORDS.search(extra)
    if word and not _VERSION_WORDS.search(line.split(" - ", 1)[1]):
        problems.append(f"may be another recording ({extra.strip(' -()[]')})")
    return problems


def resolve_line(line):
    """Find `line` on Spotify. Returns (track or None, [problems])."""
    artist, title = line.split(" - ", 1)
    queries = [(f"artist:{artist} track:{title}", []),
               # Field search is strict about spelling; plain search is not, so only
               # trust it when the artist comes back right.
               (f"{artist} {title}", ["found by loose search"])]
    for query, caveat in queries:
        resp = api("GET", "/search", params={"q": query, "type": "track", "limit": 5})
        if resp.status_code != 200:
            raise ToolError(f"Search failed for {line!r}: {resp.status_code} {resp.text}")
        found = resp.json().get("tracks", {}).get("items", [])
        if caveat:
            found = [t for t in found if "different artist" not in _match_problems(line, t)]
        if found:
            # Spotify often ranks a remix or live cut first; prefer a clean match.
            best = min(found, key=lambda t: len(_match_problems(line, t)))
            return best, caveat + _match_problems(line, best)
        time.sleep(0.05)
    return None, ["NOT FOUND"]


def resolve_playlist_lines(lines):
    """Resolve every line. Returns [(line, track or None, [problems])]."""
    print(f"Searching for {len(lines)} tracks...")
    results = []
    seen = {}
    for i, line in enumerate(lines):
        track, problems = resolve_line(line)
        if track:
            if track["uri"] in seen:
                problems = problems + [f"duplicate of line {seen[track['uri']]}"]
            seen.setdefault(track["uri"], i + 1)
        results.append((line, track, problems))
        if (i + 1) % 20 == 0:
            print(f"  {i + 1}/{len(lines)} searched...")
        time.sleep(0.05)
    return results


def _fmt_duration(ms):
    minutes, seconds = divmod(round(ms / 1000), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}:{seconds:02d}"


def _track_line(track):
    return f"{', '.join(a['name'] for a in track['artists'])} - {track['name']}"


def _print_problems(results):
    flagged = [r for r in results if r[2]]
    if flagged:
        print(f"\nCheck these ({len(flagged)}):")
        for line, track, problems in flagged:
            print(f"  - {line}: {', '.join(problems)}")
            if track:
                print(f"      matched: {_track_line(track)}  [{track['album']['name']}]")


def do_check(name=None):
    pdir = resolve_playlist(name)
    lines = parse_playlist_file(playlist_file(pdir))
    if not lines:
        raise ToolError(f"No tracks found in {playlist_file(pdir)}")
    results = resolve_playlist_lines(lines)

    print()
    for line, track, problems in results:
        if not track:
            print(f"   ?    {line}")
            continue
        album = track["album"]
        print(f"{'!' if problems else ' '}{_fmt_duration(track['duration_ms']):>5}  "
              f"{_track_line(track)}  [{album['name']}, {(album.get('release_date') or '?')[:4]}]")
    found = [t for _, t, _ in results if t]
    total = sum(t["duration_ms"] for t in found)
    print(f"\n{len(found)}/{len(lines)} tracks found, {_fmt_duration(total)} total "
          f"({total / 3600000:.2f} hours). Nothing was changed on Spotify.")
    problem = description_problem(pdir)
    print(f"\nDescription: {problem or brief_description(pdir)}")
    _print_problems(results)


def do_push(name=None):
    pdir = resolve_playlist(name)
    lines = parse_playlist_file(playlist_file(pdir))
    if not lines:
        raise ToolError(f"No tracks found in {playlist_file(pdir)}")
    playlist_id = ensure_on_spotify(pdir)

    results = resolve_playlist_lines(lines)
    # A line that resolves to a track already in the list would play it twice.
    found = [t for _, t, problems in results
             if t and not any(p.startswith("duplicate") for p in problems)]
    uris = [t["uri"] for t in found]

    print(f"\nFound {len(uris)}/{len(lines)} tracks.")
    if not uris:
        raise ToolError("Nothing found on Spotify; leaving the playlist untouched.")

    print("Replacing playlist contents...")
    # First call uses PUT (replaces), subsequent calls use POST (appends)
    for i in range(0, len(uris), 100):
        resp = api("PUT" if i == 0 else "POST", f"/playlists/{playlist_id}/items",
                   json={"uris": uris[i:i + 100]})
        if resp.status_code not in (200, 201):
            raise ToolError(f"Error adding tracks {i + 1}-{i + 100}: "
                            f"{resp.status_code} {resp.text}")

    description = brief_description(pdir)
    if description_problem(pdir):
        print(description_problem(pdir))
    else:
        resp = api("PUT", f"/playlists/{playlist_id}", json={"description": description})
        if resp.status_code != 200:
            raise ToolError(f"Could not set the description: {resp.status_code} {resp.text}")
        print("Description set from BRIEF.md.")

    total = sum(t["duration_ms"] for t in found)
    print(f"Done! Pushed {len(uris)} tracks ({_fmt_duration(total)}) to "
          f"{read_meta(pdir)['spotify_url']}")
    _print_problems(results)


# --------------- Taste reference ---------------

# (label, max age in days). Tracks are grouped under these headings, newest first.
TASTE_TIERS = [
    ("Last 90 days — strongest signal", 90),
    ("3 to 12 months ago", 365),
    ("1 to 3 years ago", 3 * 365),
    ("Older than 3 years — weakest signal", None),
]
_DATED_LINE = re.compile(r"^\d{4}-\d{2}-\d{2}  ")


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def _previous_taste_tracks():
    if not os.path.exists(TASTE_TRACKS_FILE):
        return None
    with open(TASTE_TRACKS_FILE) as f:
        return {_DATED_LINE.sub("", l.strip()) for l in f
                if l.strip() and not l.startswith("#")}


def taste_is_fresh():
    marker = TASTE_PROFILE_FILE if os.environ.get("TASTE_PLAYLIST") else TASTE_LISTENING_FILE
    if not os.path.exists(marker):
        return False
    max_age = float(os.environ.get("TASTE_MAX_AGE_HOURS", 24)) * 3600
    return time.time() - os.path.getmtime(marker) < max_age


def build_taste(items, source_url, half_life, today):
    """Render (tracks_file_text, profile_text) from fetched playlist items."""
    tracks = []
    for it in items:
        added = None
        if it["added_at"]:
            try:
                added = datetime.date.fromisoformat(it["added_at"][:10])
            except ValueError:
                pass
        # Spotify reports unknown add dates as null or the 1970 epoch.
        if added and added.year < 2006:
            added = None
        age = (today - added).days if added else None
        weight = 0.5 ** (max(age, 0) / half_life) if age is not None else 0.0
        tracks.append({**it, "added": added, "age": age, "weight": weight})
    tracks.sort(key=lambda t: t["added"] or datetime.date.min, reverse=True)

    def weight_at(days):
        return 0.5 ** (days / half_life)

    tiers = []
    lower = 0
    for label, upper in TASTE_TIERS:
        members = [t for t in tracks if t["age"] is not None and t["age"] >= lower
                   and (upper is None or t["age"] < upper)]
        if upper is None:
            members += [t for t in tracks if t["age"] is None]
            span = f"weight below {weight_at(lower):.2f}"
        else:
            span = f"weight {weight_at(upper):.2f} to {weight_at(lower):.2f}"
            lower = upper
        tiers.append((label, span, members))

    dated = [t for t in tracks if t["added"]]
    bulk_note = None
    if dated:
        day, n = collections.Counter(t["added"] for t in dated).most_common(1)[0]
        if n / len(tracks) > 0.3:
            bulk_note = (f"{n} of {len(tracks)} tracks share the add date {day}, which looks "
                         "like a bulk import rather than taste on that day. Recency says "
                         "little about those; rely on it for tracks added since.")

    header = [
        f"# Taste playlist — taste reference. {len(tracks)} tracks, synced {today}.",
        f"# Source: {source_url}",
        "# Generated by `spotify-tool/app.py taste`; edits are overwritten.",
        "# Each line: date added, then Artist - Title. Newest first: the nearer the top,",
        f"# the more it says about current taste (weight halves every {half_life:g} days).",
    ]
    if bulk_note:
        header.append(f"# NOTE: {bulk_note}")
    out = header
    for label, span, members in tiers:
        out += ["", f"## {label} ({len(members)} tracks, {span})"]
        out += [f"{t['added'] or '????-??-??'}  {t['line']}" for t in members]
    tracks_text = "\n".join(out) + "\n"

    scores = collections.defaultdict(lambda: {"score": 0.0, "n": 0, "latest": None})
    for t in tracks:
        for artist in t["artists"]:
            s = scores[artist]
            s["score"] += t["weight"]
            s["n"] += 1
            if t["added"] and (s["latest"] is None or t["added"] > s["latest"]):
                s["latest"] = t["added"]
    top = sorted(scores.items(), key=lambda kv: (-kv[1]["score"], kv[0]))[:60]

    profile = [
        "# Taste profile",
        "",
        f"Generated {today} from the taste playlist ({len(tracks)} tracks) by "
        "`spotify-tool/app.py taste`. Edits are overwritten.",
        "",
        f"Every track counts `0.5 ^ (days since added / {half_life:g})`: added today = 1.0, "
        f"{half_life:g} days ago = 0.5. Recent additions dominate; old ones still count a "
        "little.",
    ]
    if bulk_note:
        profile += ["", f"**Caveat:** {bulk_note}"]
    profile += ["", "## Where the weight sits", "",
                "| Added | Tracks | Share of total weight |", "|---|---|---|"]
    total = sum(t["weight"] for t in tracks) or 1.0
    for label, _, members in tiers:
        share = sum(t["weight"] for t in members) / total
        profile.append(f"| {label} | {len(members)} | {share:.0%} |")
    profile += ["", "## Artists by recency-weighted score", "",
                "Score is the sum of that artist's track weights, so a few recent adds can "
                "outrank many old ones.", "",
                "| Artist | Score | Tracks | Latest add |", "|---|---|---|---|"]
    for artist, s in top:
        profile.append(f"| {artist} | {s['score']:.2f} | {s['n']} | {s['latest'] or '?'} |")
    profile += ["", "## Newest 50 additions", ""]
    profile += [f"- {t['added']}  {t['line']}" for t in dated[:50]]
    return tracks_text, "\n".join(profile) + "\n"


class TasteIndex:
    """Looks tracks and artists up in the taste playlist, forgiving spelling and versions."""

    def __init__(self, items):
        self.by_title = collections.defaultdict(list)
        self.artist_counts = collections.Counter()
        for it in items:
            artists = {_norm(a) for a in it["artists"]}
            title = it["line"][len(", ".join(it["artists"])) + 3:]
            self.by_title[_title_core(title)].append((artists, (it["added_at"] or "")[:10]))
            self.artist_counts.update(artists)

    @classmethod
    def from_file(cls):
        """Rebuild from taste/Tracks.txt; None if it has not been synced."""
        if not os.path.exists(TASTE_TRACKS_FILE):
            return None
        items = []
        with open(TASTE_TRACKS_FILE) as f:
            for l in f:
                m = _DATED_LINE.match(l)
                if m and " - " in l:
                    line = l[m.end():].strip()
                    items.append({"line": line, "artists": line.split(" - ", 1)[0].split(", "),
                                  "added_at": l[:10]})
        return cls(items)

    def added(self, artists, title):
        """Date the track was added ('' if undated), or None if it is not there."""
        artists = {_norm(a) for a in artists}
        dates = [d for have, d in self.by_title.get(_title_core(title), ()) if have & artists]
        return min(dates) if dates else None

    def artist_tracks(self, artist):
        return self.artist_counts.get(_norm(artist), 0)


def build_hits(index, today):
    """Render taste/Hits.md: which playlist tracks later joined the taste playlist.

    Returns (text, number of hits)."""
    out = [
        "# Hits",
        "",
        f"Generated {today} by `spotify-tool/app.py taste`. Edits are overwritten.",
        "",
        "For each playlist made here: which of its tracks are in the taste playlist. A **hit** "
        "was added on or after the day the playlist was created, so the playlist most likely "
        "introduced it. **Already known** tracks were there before. The rest have not been "
        "picked up, which is weak evidence at best: the taste playlist is not where every "
        "liked song goes, and new playlists take time to be listened to.",
    ]
    total_hits = 0
    for slug in playlist_names():
        pdir = os.path.join(PLAYLISTS_DIR, slug)
        if read_meta(pdir).get("mix"):
            continue  # Drawn from the taste playlist itself: every track is "already known".
        created = read_meta(pdir).get("created") or ""
        # Tracks cut since the last batch still count: they were suggested too.
        lines = list(dict.fromkeys(
            parse_playlist_file(playlist_file(pdir))
            + parse_playlist_file(os.path.join(pdir, "Playlist_before_edits.txt"))))
        hits, known, rest = [], [], []
        for line in lines:
            artist, title = line.split(" - ", 1)
            added = index.added(artist.split(", "), title)
            if added is None:
                rest.append(line)
            elif added and created and added >= created:
                hits.append((added, line))
            else:
                known.append(line)
        total_hits += len(hits)
        out += ["", f"## {slug}", "",
                f"Created {created or '?'}. {len(lines)} tracks: {len(hits)} hits, "
                f"{len(known)} already known, {len(rest)} not picked up."]
        for label, members in (("Hits", [f"{d}  {l}" for d, l in sorted(hits, reverse=True)]),
                               ("Already known", known), ("Not picked up", rest)):
            if members:
                out += ["", f"### {label}", ""] + [f"- {m}" for m in members]
    if len(out) == 5:
        out += ["", "No playlists yet."]
    return "\n".join(out) + "\n", total_hits


# (Spotify time_range, heading)
LISTENING_RANGES = [
    ("short_term", "Last 4 weeks"),
    ("medium_term", "Last 6 months"),
    ("long_term", "Last year"),
]


def _get_items(path):
    resp = api("GET", path)
    if resp.status_code != 200:
        raise ToolError(f"{path.split('?')[0]}: {resp.status_code} {resp.text}")
    return resp.json().get("items", [])


def build_listening(index, today):
    """Render taste/Listening.md from Spotify's play statistics."""
    def mark(track):
        if index is None:
            return _track_line(track)
        known = index.added([a["name"] for a in track["artists"]], track["name"]) is not None
        return _track_line(track) + ("" if known else "  *(not in the taste playlist)*")

    intro = "What actually gets played, from Spotify's own statistics, most played first."
    if index is not None:
        intro += (" The taste playlist says what was saved; this says what is on repeat. "
                  "Tracks marked *(not in the taste playlist)* are played a lot without having "
                  "been saved there.")
    out = [
        "# Listening history",
        "",
        f"Generated {today} by `spotify-tool/app.py taste`. Edits are overwritten.",
        "",
        intro,
    ]
    for key, label in LISTENING_RANGES:
        artists = _get_items(f"/me/top/artists?time_range={key}&limit=30")
        tracks = _get_items(f"/me/top/tracks?time_range={key}&limit=50")
        out += ["", f"## {label}", "", "### Top artists", ""]
        for i, a in enumerate(artists, 1):
            genres = ", ".join(a.get("genres") or [])
            out.append(f"{i}. {a['name']}" + (f" — {genres}" if genres else ""))
        out += ["", "### Top tracks", ""]
        out += [f"{i}. {mark(t)}" for i, t in enumerate(tracks, 1)]
    recent = _get_items("/me/player/recently-played?limit=50")
    out += ["", "## Most recent plays", "",
            "Spotify only reports the last 50, newest first.", ""]
    out += [f"- {p.get('played_at', '')[:10]}  {mark(p['track'])}"
            for p in recent if p.get("track")]
    return "\n".join(out) + "\n"


def sync_listening(index, today):
    """Write taste/Listening.md if the token allows it. Returns a note for the report."""
    missing = [sc for sc in LISTENING_SCOPES if sc not in (load_token().get("scope") or "")]
    if missing:
        return ("Listening history skipped: the Spotify login predates it. To add it, "
                "run `auth --print-url` and `auth <redirect URL>` again.")
    try:
        _atomic_write(TASTE_LISTENING_FILE, build_listening(index, today))
    except (ToolError, requests.RequestException, KeyError) as e:
        return f"Listening history NOT refreshed ({e})."
    return "Listening history in taste/Listening.md."


def do_taste(if_stale=False):
    if if_stale and taste_is_fresh():
        return
    source = os.environ.get("TASTE_PLAYLIST")
    if not source:
        do_taste_listening_only()
        return
    pid = extract_playlist_id(source)
    half_life = float(os.environ.get("TASTE_HALF_LIFE_DAYS", 365))

    items = fetch_playlist_items(pid)
    if not items:
        raise ToolError("Taste playlist came back empty; keeping the existing reference.")
    before = _previous_taste_tracks()
    tracks_text, profile_text = build_taste(
        items, f"https://open.spotify.com/playlist/{pid}", half_life, datetime.date.today())

    os.makedirs(TASTE_DIR, exist_ok=True)
    _atomic_write(TASTE_TRACKS_FILE, tracks_text)
    _atomic_write(TASTE_PROFILE_FILE, profile_text)

    today = datetime.date.today()
    index = TasteIndex(items)
    hits_text, n_hits = build_hits(index, today)
    _atomic_write(TASTE_HITS_FILE, hits_text)

    msg = f"Taste reference synced from the taste playlist: {len(items)} tracks"
    if before is not None:
        now = {t["line"] for t in items}
        msg += f" (+{len(now - before)} / -{len(before - now)} since last sync)"
    print(msg + ". See taste/Profile.md.")
    print(f"{n_hits} tracks from playlists made here have since been added to the "
          "taste playlist. See taste/Hits.md.")
    print(sync_listening(index, today))


def do_taste_listening_only():
    """No TASTE_PLAYLIST: taste/ is Spotify's play statistics alone."""
    os.makedirs(TASTE_DIR, exist_ok=True)
    # Files from a taste playlist that is no longer configured would mislead.
    for path in (TASTE_TRACKS_FILE, TASTE_PROFILE_FILE, TASTE_HITS_FILE):
        if os.path.exists(path):
            os.remove(path)
    print("No TASTE_PLAYLIST set, so the taste reference is listening history only "
          "(Spotify's top artists and tracks, recent plays).")
    print(sync_listening(None, datetime.date.today()))


# --------------- Last.fm: what listeners say sounds alike ---------------

def lastfm(method, **params):
    key = os.environ.get("LASTFM_API_KEY")
    if not key:
        raise ToolError("Missing LASTFM_API_KEY. Get a free one at "
                        "https://www.last.fm/api/account/create and add it to spotify-tool/.env")
    resp = requests.get(LASTFM_API, timeout=30, params={
        "method": method, "api_key": key, "format": "json", "autocorrect": 1, **params})
    try:
        data = resp.json()
    except ValueError:
        raise ToolError(f"Last.fm {method}: {resp.status_code} {resp.text[:200]}")
    if "error" in data:
        raise ToolError(f"Last.fm {method}: {data.get('message', data['error'])}")
    return data


def _as_list(value):
    # Last.fm returns a bare object instead of a list when there is exactly one result.
    return value if isinstance(value, list) else [value] if value else []


def _tags(data, n=8):
    return ", ".join(t["name"] for t in _as_list(data.get("toptags", {}).get("tag"))[:n])


def _known_artist(index, name):
    n = index.artist_tracks(name) if index else 0
    return f"  [{n} in taste playlist]" if n else ""


def _known_track(index, artist, title):
    if index and index.added([artist], title) is not None:
        return "  [in taste playlist]"
    return ""


def do_similar(query, limit=25):
    index = TasteIndex.from_file()
    if " - " in query:
        artist, title = query.split(" - ", 1)
        tags = _tags(lastfm("track.gettoptags", artist=artist, track=title))
        similar = _as_list(lastfm("track.getsimilar", artist=artist, track=title, limit=limit)
                           .get("similartracks", {}).get("track"))
        print(f"Tracks similar to {query}" + (f"  (tags: {tags})" if tags else ""))
        for t in similar:
            name = t["artist"]["name"]
            print(f"  {float(t.get('match', 0)):.2f}  {name} - {t['name']}"
                  f"{_known_track(index, name, t['name'])}")
        if not similar:
            print("  Nothing: Last.fm has too few listeners for this track. Try the artist.")
        return
    tags = _tags(lastfm("artist.gettoptags", artist=query))
    similar = _as_list(lastfm("artist.getsimilar", artist=query, limit=limit)
                       .get("similarartists", {}).get("artist"))
    print(f"Artists similar to {query}" + (f"  (tags: {tags})" if tags else ""))
    for a in similar:
        print(f"  {float(a.get('match', 0)):.2f}  {a['name']}{_known_artist(index, a['name'])}")
    if not similar:
        print("  Nothing: Last.fm does not know this artist well enough.")


def do_tag(tag, limit=25):
    index = TasteIndex.from_file()
    artists = _as_list(lastfm("tag.gettopartists", tag=tag, limit=limit)
                       .get("topartists", {}).get("artist"))
    tracks = _as_list(lastfm("tag.gettoptracks", tag=tag, limit=limit)
                      .get("tracks", {}).get("track"))
    if not artists and not tracks:
        print(f"Last.fm has nothing tagged {tag!r}.")
        return
    print(f"Top artists tagged {tag!r}")
    for a in artists:
        print(f"  {a['name']}{_known_artist(index, a['name'])}")
    print(f"\nTop tracks tagged {tag!r}")
    for t in tracks:
        name = t["artist"]["name"]
        print(f"  {name} - {t['name']}{_known_track(index, name, t['name'])}")


# --------------- Spotify: an artist's catalog ---------------

def _get_json(path, **params):
    resp = api("GET", path, params=params)
    if resp.status_code != 200:
        raise ToolError(f"Spotify {path}: {resp.status_code} {resp.text[:200]}")
    return resp.json()


def find_artist(name):
    """The Spotify artist best matching `name`, plus the runners-up."""
    found = _get_json("/search", q=name, type="artist", limit=5)["artists"]["items"]
    if not found:
        raise ToolError(f"No artist on Spotify matches {name!r}.")
    # Search ranks by popularity; an exact name beats a more popular near-miss.
    exact = [a for a in found if _norm(a["name"]) == _norm(name)]
    best = exact[0] if exact else found[0]
    return best, [a for a in found if a is not best]


def artist_albums(artist_id, groups):
    albums, url, params = [], f"/artists/{artist_id}/albums", {
        "include_groups": groups, "limit": 50}
    while url:
        data = _get_json(url, **params)
        albums += data["items"]
        url, params = data.get("next"), {}
    return albums


def do_artist(name, groups="album,single", top_only=False):
    """Print an artist's top tracks and every track on their releases, as
    `Artist - Title` lines ready for Playlist.txt."""
    index = TasteIndex.from_file()
    artist, others = find_artist(name)
    genres = ", ".join(artist.get("genres", [])[:6])
    print(f"{artist['name']}" + (f"  ({genres})" if genres else "")
          + f"  {artist['external_urls']['spotify']}{_known_artist(index, artist['name'])}")
    if others and _norm(artist["name"]) != _norm(name):
        print("  Not this one? Also found: " + "; ".join(a["name"] for a in others))

    def show(track, album=None):
        album = album or track.get("album", {})
        year = (album.get("release_date") or "")[:4]
        line = _track_line(track)
        print(f"  {_fmt_duration(track['duration_ms']):>5}  {line}  [{album.get('name', '?')}"
              f"{', ' + year if year else ''}]"
              f"{_known_track(index, track['artists'][0]['name'], track['name'])}")

    # Top tracks are optional context: some app tiers can't call this endpoint.
    resp = api("GET", f"/artists/{artist['id']}/top-tracks", params={"market": "from_token"})
    if resp.status_code == 200:
        print("\nTop tracks")
        for t in resp.json().get("tracks", []):
            show(t)
    if top_only:
        return

    albums = artist_albums(artist["id"], groups)
    tracks = {}
    for album in albums:
        items, url, params = [], f"/albums/{album['id']}/tracks", {"limit": 50}
        while url:
            data = _get_json(url, **params)
            items += data["items"]
            url, params = data.get("next"), {}
        tracks[album["id"]] = [t for t in items
                               if any(a["id"] == artist["id"] for a in t["artists"])]
        time.sleep(0.05)
    # Singles, deluxe editions and re-recordings repeat songs: each song is listed
    # once, under the earliest album it appeared on (singles only if no album has it).
    owner = {}
    for album in sorted(albums, key=lambda a: (a["album_type"] != "album",
                                               a.get("release_date") or "9")):
        for t in tracks[album["id"]]:
            owner.setdefault(_title_core(t["name"]), t["id"])
    albums.sort(key=lambda a: a.get("release_date") or "", reverse=True)
    print(f"\nCatalog, newest first ({len(owner)} songs on {len(albums)} releases: {groups})")
    for album in albums:
        mine = [t for t in tracks[album["id"]] if owner[_title_core(t["name"])] == t["id"]]
        if not mine:
            continue
        print(f" {album['name']} ({(album.get('release_date') or '?')[:4]}, "
              f"{album['album_type']})")
        for t in mine:
            show(t, album)


# --------------- Main ---------------

# --------------- Mix: a playlist drawn from the taste playlist itself ---------------

MIX_STATE_FILE = "mix-state.json"
MIX_RECENT_DAYS = 120      # "Recently added" for the core of the mix
MIX_RECENT_HALF_LIFE = 60  # How fast a recent add fades out of the core
MIX_OLD_DAYS = 180         # Old favourites are at least this old
MIX_REPEAT_WEEKS = 3       # Old favourites picked this recently are mostly skipped


def _mix_key(artists, title):
    return (_norm(artists[0]) if artists else "", _title_core(title))


def _top_tracks(time_range, pages=2):
    """Spotify's most-played tracks for a range, most played first (up to 50 per page)."""
    out = []
    for page in range(pages):
        out += _get_items(f"/me/top/tracks?time_range={time_range}&limit=50&offset={page * 50}")
    return out


def _play_scores():
    """{key: {"short": s, "medium": s, "long": s, "recent": n}} from Spotify's statistics."""
    scores = collections.defaultdict(lambda: collections.Counter())
    for rng in ("short_term", "medium_term", "long_term"):
        tracks = _top_tracks(rng)
        for rank, t in enumerate(tracks):
            key = _mix_key([a["name"] for a in t["artists"]], t["name"])
            scores[key][rng.split("_")[0]] = max(scores[key][rng.split("_")[0]],
                                                1 - rank / max(len(tracks), 1))
    for p in _get_items("/me/player/recently-played?limit=50"):
        t = p.get("track")
        if t:
            scores[_mix_key([a["name"] for a in t["artists"]], t["name"])]["recent"] += 1
    return scores


def _weighted_sample(pool, weight, k, rng):
    """k items without replacement, each drawn with probability proportional to weight."""
    keyed = [(rng.random() ** (1 / w), i) for i, w in ((i, weight(i)) for i in pool) if w > 0]
    return [i for _, i in sorted(keyed, key=lambda x: x[0], reverse=True)[:k]]


def _spread_artists(tracks, rng):
    """Shuffle, then move tracks so the same artist doesn't play twice in a row."""
    tracks = tracks[:]
    rng.shuffle(tracks)
    for _ in range(3):
        for i in range(1, len(tracks)):
            if set(tracks[i]["artists"]) & set(tracks[i - 1]["artists"]):
                for j in range(len(tracks)):
                    if j in (i, i - 1):
                        continue
                    neighbours = [tracks[n] for n in (j - 1, j + 1, i - 1, i + 1)
                                  if 0 <= n < len(tracks) and n not in (i, j)]
                    if not any(set(tracks[j]["artists"]) & set(n["artists"]) or
                               set(tracks[i]["artists"]) & set(n["artists"])
                               for n in neighbours):
                        tracks[i], tracks[j] = tracks[j], tracks[i]
                        break
    return tracks


def do_mix(name, size=80, old_share=0.3, dry=False, seed=None):
    """Rebuild a mix playlist: recent, much-played adds plus a few old favourites, shuffled."""
    import random
    source = os.environ.get("TASTE_PLAYLIST")
    if not source:
        raise ToolError("mix draws from the taste playlist; set TASTE_PLAYLIST first.")
    pdir = resolve_playlist(name)
    meta = read_meta(pdir)
    state_path = os.path.join(pdir, MIX_STATE_FILE)
    state = {"pushed": [], "removed": [], "history": []}
    if os.path.exists(state_path):
        with open(state_path) as f:
            state.update(json.load(f))
    today = datetime.date.today()
    rng = random.Random(seed if seed is not None else today.isoformat())

    # Tracks taken out of the mix on Spotify since the last push stay out.
    if meta.get("spotify_id") and state["pushed"]:
        current = {it["uri"] for it in fetch_playlist_items(meta["spotify_id"])}
        if current:
            known = {r["uri"] for r in state["removed"]}
            for p in state["pushed"]:
                if p["uri"] not in current and p["uri"] not in known:
                    state["removed"].append({**p, "date": today.isoformat()})
                    print(f"Removed on Spotify, left out from now on: {p['line']}")
    removed_uris = {r["uri"] for r in state["removed"]}
    removed_keys = {_mix_key(*_split_raw(r["line"])) for r in state["removed"]}

    items, seen = [], set()
    for it in fetch_playlist_items(extract_playlist_id(source)):
        uri = it.get("uri") or ""
        title = it["line"][len(", ".join(it["artists"])) + 3:]
        key = _mix_key(it["artists"], title)
        if (not uri.startswith("spotify:track:") or uri in seen or key in seen
                or uri in removed_uris or key in removed_keys):
            continue
        seen |= {uri, key}
        try:
            added = datetime.date.fromisoformat((it["added_at"] or "")[:10])
            age = (today - added).days if added.year >= 2006 else None
        except ValueError:
            age = None
        items.append({**it, "key": key, "age": age})

    plays = _play_scores()
    for it in items:
        p = plays.get(it["key"], {})
        it["short"], it["medium"], it["long"] = p.get("short", 0), p.get("medium", 0), p.get("long", 0)
        it["recent"] = p.get("recent", 0)

    # Core: added in the last few months, or on repeat right now.
    n_old = round(size * old_share)
    core_pool = [it for it in items
                 if (it["age"] is not None and it["age"] <= MIX_RECENT_DAYS) or it["short"]]

    def core_weight(it):
        fresh = 0.5 ** (it["age"] / MIX_RECENT_HALF_LIFE) if it["age"] is not None else 0
        return 0.3 + fresh + 2 * it["short"] + it["medium"] + 0.3 * min(it["recent"], 3)
    core = _weighted_sample(core_pool, lambda it: core_weight(it) ** 2, size - n_old, rng)

    # Old favourites: long-loved tracks, plus older ones by artists still in favour.
    picked = {it["uri"] for it in core}
    recent_old = {u for h in state["history"][-MIX_REPEAT_WEEKS:] for u in h.get("old", [])}
    artist_score = collections.Counter()
    for it in items:
        if it["age"] is not None:
            for a in it["artists"]:
                artist_score[a] += 0.5 ** (it["age"] / 365)
    top_artist = max(artist_score.values(), default=1)
    old_pool = [it for it in items if it["uri"] not in picked
                and it["age"] is not None and it["age"] >= MIX_OLD_DAYS]

    def old_weight(it):
        w = (0.5 + 3 * it["long"] + 2 * it["medium"]
             + 2 * max((artist_score[a] for a in it["artists"]), default=0) / top_artist)
        return w * (0.1 if it["uri"] in recent_old else 1)
    old = _weighted_sample(old_pool, old_weight, n_old, rng)

    mix = _spread_artists(core + old, rng)
    lines = [it["line"] for it in mix]
    old_uris = {it["uri"] for it in old}
    print(f"Mix of {len(mix)}: {len(core)} recent or on repeat, {len(old)} old favourites.")
    for it in mix:
        tag = "old " if it["uri"] in old_uris else "new "
        age = f"{it['age']:>4}d" if it["age"] is not None else "   ?d"
        print(f"  {tag}{age}  {it['line']}")
    if dry:
        print("\n--dry: nothing written or pushed.")
        return

    with open(playlist_file(pdir), "w") as f:
        f.write(f"# Rebuilt {today} by `app.py mix`; edits here are overwritten next time.\n"
                "# Remove tracks on Spotify instead: they stay out of later mixes.\n")
        f.write("\n".join(lines) + "\n")
    playlist_id = ensure_on_spotify(pdir)
    uris = [it["uri"] for it in mix]
    for i in range(0, len(uris), 100):
        resp = api("PUT" if i == 0 else "POST", f"/playlists/{playlist_id}/items",
                   json={"uris": uris[i:i + 100]})
        if resp.status_code not in (200, 201):
            raise ToolError(f"Error adding tracks {i + 1}-{i + 100}: "
                            f"{resp.status_code} {resp.text}")
    description = brief_description(pdir)
    if description and not description_problem(pdir):
        api("PUT", f"/playlists/{playlist_id}", json={"description": description})

    state["pushed"] = [{"uri": it["uri"], "line": it["line"]} for it in mix]
    state["history"] = (state["history"] + [{"date": today.isoformat(),
                                             "old": sorted(old_uris)}])[-12:]
    _atomic_write(state_path, json.dumps(state, indent=1) + "\n")
    if not meta.get("mix"):
        meta["mix"] = True
        write_meta(pdir, meta)
    print(f"\nPushed {len(uris)} tracks to {read_meta(pdir)['spotify_url']}")


def _split_raw(line):
    artist, title = line.split(" - ", 1)
    return artist.split(", "), title



def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    parser = argparse.ArgumentParser(usage=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("auth")
    p.add_argument("redirected", nargs="?")
    p.add_argument("--print-url", action="store_true")

    p = sub.add_parser("new")
    p.add_argument("name")
    p.add_argument("url", nargs="?")
    p.add_argument("--public", action="store_true")
    p.add_argument("--no-prefix", action="store_true")

    sub.add_parser("list")
    for cmd in ("check", "push", "pull"):
        sub.add_parser(cmd).add_argument("playlist", nargs="?")
    sub.add_parser("export").add_argument("url")
    sub.add_parser("taste").add_argument("--if-stale", action="store_true")
    for cmd, arg in (("similar", "query"), ("tag", "tag")):
        p = sub.add_parser(cmd)
        p.add_argument(arg)
        p.add_argument("--limit", type=int, default=25)
    p = sub.add_parser("mix")
    p.add_argument("playlist", nargs="?")
    p.add_argument("--size", type=int, default=80)
    p.add_argument("--old-share", type=float, default=0.3)
    p.add_argument("--dry", action="store_true")
    p.add_argument("--seed")
    p = sub.add_parser("artist")
    p.add_argument("name")
    p.add_argument("--top", action="store_true")
    p.add_argument("--groups", default="album,single")

    args = parser.parse_args()
    try:
        if args.cmd == "auth":
            do_auth(args.redirected, args.print_url)
        elif args.cmd == "new":
            do_new(args.name, args.url, args.public, not args.no_prefix)
        elif args.cmd == "list":
            do_list()
        elif args.cmd == "check":
            do_check(args.playlist)
        elif args.cmd == "push":
            do_push(args.playlist)
        elif args.cmd == "pull":
            do_pull(args.playlist)
        elif args.cmd == "export":
            do_export(args.url)
        elif args.cmd == "taste":
            do_taste(args.if_stale)
        elif args.cmd == "similar":
            do_similar(args.query, args.limit)
        elif args.cmd == "tag":
            do_tag(args.tag, args.limit)
        elif args.cmd == "mix":
            do_mix(args.playlist, args.size, args.old_share, args.dry, args.seed)
        elif args.cmd == "artist":
            do_artist(args.name, args.groups, args.top)
    except (ToolError, requests.RequestException) as e:
        if args.cmd == "taste" and args.if_stale:
            # Runs unattended at session start: report, but never fail the session.
            print(f"Taste reference NOT refreshed ({e}). taste/ may be out of date.")
            return
        print(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
