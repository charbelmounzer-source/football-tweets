"""TEMPORARY probe (remove before any merge): ask FotMob again for 2010/11 matches that the
14 Nov run stored as "empty page", plus control matches of Aug-Dec 2010 that loaded fine.
Reads FotMob only; no BigQuery, no writes."""

import json
import sys
import urllib.request

from football import fotmob, model

EMPTIES = [int(x) for x in sys.argv[1].split()]


def raw(path):
    req = urllib.request.Request(f"{fotmob.BASE_URL}/{path}", headers=fotmob.HEADERS)
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read()
        if body[:2] == b"\x1f\x8b":
            import gzip
            body = gzip.decompress(body)
        return resp.status, body


def describe(client, mid):
    payload = client.match(mid)
    slim = model.slim_match(payload)
    keys = sorted(payload) if isinstance(payload, dict) else type(payload).__name__
    size = len(json.dumps(payload)) if payload is not None else 0
    starters = [len(slim["lineup"][s]["starters"]) for s in ("home", "away")] if slim.get("lineup") else None
    return (f"{mid}: slim match_id={slim.get('match_id')}, {size} bytes, keys={keys}, "
            f"starters={starters}, players={len(slim.get('players') or [])}, goals={len(slim.get('goals') or [])}")


client = fotmob.Client(max_requests=200)

# controls: 4 finished Aug-Dec 2010 matches of each league (those all loaded on 14 Nov)
controls = []
for code in ("EPL", "La_Liga", "Serie_A", "Bundesliga", "Ligue_1"):
    try:
        lg = model.slim_league(client.league(code, 2010))
    except Exception as e:                                   # noqa: BLE001
        print(f"league {code} 2010/11 failed: {e}")
        continue
    autumn = [m for m in lg["matches"] if m["finished"] and (m["kickoff"] or "") < "2011-01-01"]
    controls += [m["id"] for m in autumn[::max(1, len(autumn) // 3)][:3]]

print(f"== controls (Aug-Dec 2010, loaded fine on 14 Nov): {len(controls)}")
for mid in controls:
    print("  " + describe(client, mid))

print(f"== stored as 'empty page' on 14 Nov: {len(EMPTIES)}")
full = 0
for mid in EMPTIES:
    line = describe(client, mid)
    full += "slim match_id=None" not in line
    print("  " + line)
print(f"== {full} of {len(EMPTIES)} 'empty' matches now have content")

status, body = raw(f"matchDetails?matchId={EMPTIES[0]}")
print(f"== raw answer for {EMPTIES[0]}: HTTP {status}, {len(body)} bytes: {body[:600]!r}")
