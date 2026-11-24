#!/usr/bin/env python3
"""Write a fake Spotify extended-history zip (and a basic-account-data one) for tests."""
import json, random, sys, zipfile, datetime as dt
random.seed(4)
artists = ["Nightwish", "Lindsey Stirling", "Taylor Swift", "The Shins", "Metric", "Fall Out Boy", "Phoebe Bridgers", "Daft Punk", "Hozier", "Amaranthe"] + [f"Band {i}" for i in range(60)]
def rows(n, start):
    out = []
    t = start
    for _ in range(n):
        t += dt.timedelta(minutes=random.randint(3, 600))
        a = random.choices(artists, weights=[30 - min(i, 28) for i in range(len(artists))])[0]
        out.append({"ts": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "ms_played": random.choice([random.randint(1000, 25000), random.randint(120000, 280000)]),
                    "master_metadata_track_name": f"{a} song {random.randint(1, 8)}", "master_metadata_album_artist_name": a,
                    "master_metadata_album_album_name": "Album", "spotify_track_uri": "spotify:track:x", "reason_end": "trackdone",
                    "shuffle": random.random() < .4, "skipped": random.random() < .2})
    out.append({"ts": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "ms_played": 5000, "master_metadata_track_name": None, "master_metadata_album_artist_name": None, "episode_name": "Podcast"})
    return out
r = rows(6000, dt.datetime(2022, 3, 1))
with zipfile.ZipFile(sys.argv[1], "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("Spotify Extended Streaming History/Streaming_History_Audio_2022-2023_0.json", json.dumps(r[:3000]))
    z.writestr("Spotify Extended Streaming History/Streaming_History_Audio_2023-2025_1.json", json.dumps(r[3000:]))
    z.writestr("Spotify Extended Streaming History/ReadMeFirst_ExtendedStreamingHistory.pdf", b"%PDF")
basic = [{"endTime": x["ts"][:16].replace("T", " "), "artistName": x["master_metadata_album_artist_name"] or "Unknown Artist", "trackName": x["master_metadata_track_name"] or "Unknown Track", "msPlayed": x["ms_played"]} for x in r[-2000:]]
with zipfile.ZipFile(sys.argv[2], "w", zipfile.ZIP_STORED) as z:
    z.writestr("Spotify Account Data/StreamingHistory_music_0.json", json.dumps(basic))
    z.writestr("Spotify Account Data/Userdata.json", json.dumps({"username": "friendsam"}))
