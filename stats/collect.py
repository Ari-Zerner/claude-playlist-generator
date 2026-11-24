#!/usr/bin/env python3
"""Collect the user's Spotify listening data into stats/data/spotify.json for the stats page."""
import json, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "spotify-tool"))
import app  # noqa

def get(path):
    r = app.api("GET", path)
    if r.status_code != 200:
        raise app.ToolError(f"{path}: {r.status_code} {r.text[:200]}")
    return r.json()

def paged(path, key="items"):
    out = []
    while path:
        d = get(path); out += d.get(key, []); path = d.get("next")
    return out

def slim_track(t):
    al = t.get("album") or {}
    imgs = al.get("images") or []
    return {"id": t.get("id"), "name": t.get("name"),
            "artists": [[a.get("id"), a.get("name")] for a in t.get("artists", [])],
            "album": al.get("name"), "release": al.get("release_date"),
            "img": (imgs[-2] if len(imgs) > 1 else imgs[0])["url"] if imgs else None,
            "ms": t.get("duration_ms"), "pop": t.get("popularity"), "explicit": t.get("explicit")}

def slim_artist(a):
    imgs = a.get("images") or []
    return {"id": a["id"], "name": a["name"], "genres": a.get("genres", []),
            "pop": a.get("popularity"), "followers": (a.get("followers") or {}).get("total"),
            "img": (imgs[-2] if len(imgs) > 1 else imgs[0])["url"] if imgs else None}

def main():
    me = get("/me")
    out = {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "user": {"name": me.get("display_name"), "followers": me["followers"]["total"],
                    "img": (me.get("images") or [{}])[0].get("url")},
           "top": {}, "recent": [], "liked": [], "taste": [], "playlists": [], "artists": {}}
    artist_ids = set()
    for rng in ("short_term", "medium_term", "long_term"):
        arts = get(f"/me/top/artists?limit=50&time_range={rng}")["items"]
        trks = get(f"/me/top/tracks?limit=50&time_range={rng}")["items"]
        out["top"][rng] = {"artists": [a["id"] for a in arts], "tracks": [slim_track(t) for t in trks]}
        for a in arts: out["artists"][a["id"]] = slim_artist(a)
    for it in get("/me/player/recently-played?limit=50")["items"]:
        out["recent"].append({"at": it["played_at"], **slim_track(it["track"])})
    for it in paged("/me/tracks?limit=50"):
        if it.get("track"): out["liked"].append({"at": it["added_at"], **slim_track(it["track"])})
    pid = app.extract_playlist_id(os.environ.get("TASTE_PLAYLIST", ""))
    for it in paged(f"/playlists/{pid}/items?limit=50"):
        t = it.get("item") or it.get("track")
        if t and t.get("name"): out["taste"].append({"at": it["added_at"], **slim_track(t)})
    for p in paged("/me/playlists?limit=50"):
        if not p: continue
        out["playlists"].append({"name": p["name"], "mine": p["owner"]["id"] == me["id"],
                                 "n": (p.get("items") or p.get("tracks") or {}).get("total")})
    for coll in ("recent", "liked", "taste"):
        for t in out[coll]: artist_ids.update(a[0] for a in t["artists"] if a[0])
    for k in out["top"].values():
        for t in k["tracks"]: artist_ids.update(a[0] for a in t["artists"] if a[0])
    todo = [i for i in artist_ids if i not in out["artists"]]
    for i in range(0, len(todo), 50):
        for a in get("/artists?ids=" + ",".join(todo[i:i+50]))["artists"]:
            if a: out["artists"][a["id"]] = slim_artist(a)
    os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
    path = os.path.join(HERE, "data", "spotify.json")
    with open(path, "w") as f: json.dump(out, f, separators=(",", ":"))
    print(f"{path}: {len(out['liked'])} liked, {len(out['taste'])} taste, "
          f"{len(out['artists'])} artists, {len(out['playlists'])} playlists")

if __name__ == "__main__":
    main()
