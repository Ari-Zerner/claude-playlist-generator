#!/usr/bin/env python3
"""Turn stats/data/spotify.json (from collect.py) into the stats app's profile format,
and print a share link for it. The app reads the same format from a Spotify data export."""
import base64, collections, json, os, sys, zlib, datetime as dt
HERE = os.path.dirname(os.path.abspath(__file__))
APP_URL = os.environ.get("STATS_APP_URL", "https://app.arizerner.com/soundprint/")

def build(d):
    A = d["artists"]
    taste = d["taste"]
    terms = {}
    score = collections.Counter()
    for key, rng in (("s", "short_term"), ("m", "medium_term"), ("l", "long_term")):
        ids = d["top"][rng]["artists"]
        terms[key] = {"a": [A[i]["name"] for i in ids],
                      "t": [[t["name"], t["artists"][0][1]] for t in d["top"][rng]["tracks"]]}
        for r, i in enumerate(ids):
            score[A[i]["name"]] += (50 - r) / 50
    lib = collections.Counter(t["artists"][0][1] for t in taste)
    for name, n in lib.items():
        score[name] += min(n, 60) / 60  # library depth counts, a little
    name_genres = {a["name"]: a["genres"][:3] for a in A.values()}
    artists = [[n, round(w, 3), name_genres.get(n, [])] for n, w in score.most_common(50)]
    tscore = collections.Counter(); tart = {}
    for key, rng in (("s", "short_term"), ("m", "medium_term"), ("l", "long_term")):
        for r, t in enumerate(d["top"][rng]["tracks"]):
            k = (t["name"], t["artists"][0][1]); tscore[k] += (50 - r) / 50
    tracks = [[k[0], k[1], round(w, 3)] for k, w in tscore.most_common(50)]
    months = collections.Counter(t["at"][:7] for t in taste)
    first = min(months); last = max(months)
    y, m = map(int, first.split("-")); seq = []
    while f"{y:04d}-{m:02d}" <= last:
        k = f"{y:04d}-{m:02d}"; seq.append([k, months.get(k, 0)])
        m += 1
        if m > 12: y, m = y + 1, 1
    genres = collections.Counter()
    for t in taste:
        gs = A.get(t["artists"][0][0], {}).get("genres", [])
        for g in gs: genres[g] += 1 / len(gs)
    eras = collections.Counter(t["release"][:4] for t in taste if t.get("release") and t["release"][:4] > "1900")
    years = []
    by_year = collections.defaultdict(collections.Counter)
    by_year_t = {}
    for t in sorted(taste, key=lambda x: x["at"]):
        yr = t["at"][:4]; by_year[yr][t["artists"][0][1]] += 1
    for yr in sorted(by_year):
        (top, n), = by_year[yr].most_common(1)
        years.append([int(yr), top, n, sum(by_year[yr].values())])
    pops = [t["pop"] for t in taste if t.get("pop")]
    hist = [0] * 10
    for p in pops: hist[min(p // 10, 9)] += 1
    deep = [[t["name"], t["artists"][0][1], t["pop"]] for t in taste if t.get("pop") == 1][:0]
    lib_ms = sum(t["ms"] or 0 for t in taste)
    rising = [n for n in terms["s"]["a"][:25] if n not in terms["l"]["a"]][:8]
    return {
        "v": 1, "name": d["user"]["name"], "src": "api", "gen": d["generated"][:10],
        "span": [taste[0]["at"][:10] if taste else None, d["generated"][:10]],
        "stats": {"lib": len(taste), "artists": len(lib), "hours": round(lib_ms / 3.6e6),
                  "pls": sum(1 for p in d["playlists"] if p["mine"]),
                  "pop": round(sum(pops) / len(pops)) if pops else None,
                  "genres": len(genres), "rising": rising},
        "artists": artists, "tracks": tracks, "terms": terms,
        "months": seq, "monthsLabel": "songs saved",
        "genres": [[g, round(w, 1)] for g, w in genres.most_common(30)],
        "eras": sorted([[int(k), v] for k, v in eras.items()]),
        "years": years, "popHist": hist,
    }

def share_link(p):
    raw = json.dumps(p, separators=(",", ":"), ensure_ascii=False).encode()
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    z = c.compress(raw) + c.flush()
    return APP_URL + "#s=" + base64.urlsafe_b64encode(z).decode().rstrip("="), len(raw), len(z)

if __name__ == "__main__":
    d = json.load(open(os.path.join(HERE, "data", "spotify.json")))
    p = build(d)
    out = os.path.join(HERE, "data", "profile.json")
    json.dump(p, open(out, "w"), ensure_ascii=False, separators=(",", ":"))
    link, n, z = share_link(p)
    open(os.path.join(HERE, "data", "share-link.txt"), "w").write(link + "\n")
    print(f"{out}: {n} bytes, {z} compressed, link {len(link)} chars")
