"""Temporary development helper (removed before merge): facts about the five leagues and two
real answers, trimmed, printed as base64 so they can be read from the run log."""
import base64, gzip, json, sys, os
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fotmob_probe import fetch, as_json, SITE

IDS = [47, 87, 55, 54, 53]


def get(path):
    r = fetch(f"{SITE}/api/data/{path}", "application/json")
    d = as_json(r["body"]) if r["status"] == 200 else None
    if d is None:
        print(f"!! {path}: {r['error'] or r['status']}")
    return d


def emit(name, obj):
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
    b = base64.b64encode(gzip.compress(raw, 9)).decode()
    print(f"@@ {name} json={len(raw)} b64={len(b)}")
    for i in range(0, len(b), 3000):
        print(f"B64|{name}|{b[i:i + 3000]}")


def trim_league(d):
    return {"details": {k: d.get("details", {}).get(k) for k in ("id", "name", "selectedSeason", "latestSeason", "country")},
            "allAvailableSeasons": d.get("allAvailableSeasons"),
            "fixtures": {"allMatches": (d.get("fixtures") or {}).get("allMatches")},
            "table": d.get("table")}


def trim_match(d):
    c, h = d.get("content") or {}, d.get("header") or {}
    g = dict(d.get("general") or {}); g.pop("teamColors", None)
    ps = {k: {kk: vv for kk, vv in v.items() if kk not in ("shotmap", "funFacts")}
          for k, v in (c.get("playerStats") or {}).items()}
    return {"general": g, "header": {"teams": h.get("teams"), "status": h.get("status"), "events": h.get("events")},
            "content": {"shotmap": {"shots": (c.get("shotmap") or {}).get("shots")}, "playerStats": ps,
                        "lineup": c.get("lineup"),
                        "stats": {"Periods": {"All": ((c.get("stats") or {}).get("Periods") or {}).get("All")}},
                        "matchFacts": {"events": (c.get("matchFacts") or {}).get("events")}}}


def describe(d, tag):
    ms = (d.get("fixtures") or {}).get("allMatches") or []
    ids = Counter(str(m.get("id")) for m in ms)
    st = Counter((m["status"].get("finished"), m["status"].get("started"), m["status"].get("cancelled"),
                  m["status"].get("awarded"), (m["status"].get("reason") or {}).get("longKey")) for m in ms)
    t = d.get("table") or []
    tb = ((t[0].get("data") or {}).get("table") or {}) if t else {}
    print(f"## {tag}: {d.get('details', {}).get('name')} {d.get('details', {}).get('selectedSeason')} "
          f"matches={len(ms)} distinct={len(ids)} dup={[k for k, v in ids.items() if v > 1]}")
    print(f"   status (finished, started, cancelled, awarded, reason): {dict(st)}")
    print(f"   table entries={len(t)} keys={list((t[0].get('data') or {}).keys()) if t else None} "
          f"table keys={list(tb.keys())} all={len(tb.get('all') or [])} xg={len(tb.get('xg') or [])}")
    print("   teams: " + " | ".join(f"{x.get('id')}={x.get('name')}" for x in tb.get("all") or []))
    odd = [m for m in ms if m["status"].get("cancelled") or m["status"].get("awarded")
           or (not m["status"].get("finished") and m["status"].get("started"))]
    for m in odd[:5]:
        print("   odd:", json.dumps(m, ensure_ascii=False)[:400])
    if len(ms) != 380 and len(ms) != 306:
        per = Counter()
        for m in ms:
            per[m["home"]["name"]] += 1; per[m["away"]["name"]] += 1
        print("   matches per team:", dict(per))
        rounds = Counter(str(m.get("round")) for m in ms)
        print("   rounds:", {k: v for k, v in rounds.items() if v not in (10, 9)})
        for m in ms:
            if not str(m.get("round")).isdigit():
                print("   non-round:", json.dumps(m, ensure_ascii=False)[:400])


first = None
for i in IDS:
    d = get(f"leagues?id={i}")
    if d:
        describe(d, str(i))
        first = first or d
old = get("leagues?id=47&season=2023%2F2024")
if old:
    describe(old, "47 2023/24")
old2 = get("leagues?id=54&season=2020%2F2021")
if old2:
    describe(old2, "54 2020/21")
if first:
    emit("league", trim_league(first))
    m = get("matchDetails?matchId=5795512")
    if m:
        emit("match", trim_match(m))
        print("match top keys:", list(m.keys()), "content keys:", list((m.get("content") or {}).keys()))
