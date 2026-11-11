"""FotMob probe: can a plain script read FotMob, and is the data we need in the answer?

    python tools/fotmob_probe.py                 # Premier League, latest finished match
    python tools/fotmob_probe.py --match 4506324 # a match you choose
    python tools/fotmob_probe.py --outline       # JSON endpoints only, and print every field

At most 6 requests, 2 s apart. It tries two ways of reading a league and a match:
  A. the JSON endpoints the website calls   (www.fotmob.com/api/data/...)
  B. the web page itself, which carries its data in a <script id="__NEXT_DATA__"> block
Every answer is saved in probe_out/ so the real field names can be read afterwards.
FotMob has no official API: this is a test, not something to run in a loop.
"""

import argparse
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

try:   # same reason as football/understat.py: certificate store on Windows / company networks
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

SITE = "https://www.fotmob.com"
LEAGUE_ID = 47            # Premier League on FotMob
DELAY = 2.0
OUT = "probe_out"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
NEXT_DATA = re.compile(rb'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

_last = [0.0]


def fetch(url, accept):
    """-> dict(status, ctype, body, error). Never raises."""
    wait = DELAY - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": accept,
        "Accept-Language": "en-GB,en;q=0.9", "Accept-Encoding": "gzip",
        "Referer": SITE + "/"})
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            status, headers, body, final = resp.status, resp.headers, resp.read(), resp.geturl()
    except urllib.error.HTTPError as e:
        status, headers, body, final = e.code, e.headers, e.read(), url
    except Exception as e:                                   # DNS, TLS, timeout...
        return {"status": None, "ctype": "", "body": b"", "url": url,
                "error": f"{type(e).__name__}: {getattr(e, 'reason', e)}"}
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    return {"status": status, "ctype": headers.get("Content-Type", ""), "body": body,
            "url": final, "error": None}


def as_json(body):
    try:
        return json.loads(body)
    except ValueError:
        return None


def from_page(body):
    """The data block of a FotMob web page, or None."""
    m = NEXT_DATA.search(body)
    return as_json(m.group(1)) if m else None


def walk(node):
    """Every dict inside a JSON value, depth first."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def outline(node, path="", lines=None, limit=4000):
    """Every field of a payload as 'path: type = example'. Lists show one merged element;
    dicts keyed by ids show one entry; short sets of text values are listed in full."""
    lines = [] if lines is None else lines
    if len(lines) >= limit:
        return lines
    if isinstance(node, dict):
        keys = list(node)
        if len(keys) > 3 and all(str(k).lstrip("-").isdigit() for k in keys):
            lines.append(f"{path}{{id}}  x{len(keys)}, shown as a list:")
            outline(list(node.values()), path + "{id}", lines, limit)
        else:
            for k in keys:
                outline(node[k], f"{path}.{k}" if path else str(k), lines, limit)
    elif isinstance(node, list):
        dicts = [e for e in node if isinstance(e, dict)]
        if dicts and len(dicts) == len(node):
            lines.append(f"{path}[]  x{len(node)}")
            merged, seen = {}, {}
            for e in dicts:
                for k, v in e.items():
                    if isinstance(v, list) and isinstance(merged.get(k), list):
                        merged[k] = merged[k] + v          # nested lists: keep every element
                    elif merged.get(k) in (None, [], {}, ""):
                        merged[k] = v
                    if isinstance(v, (str, bool)) and k not in ("id",):
                        seen.setdefault(k, set()).add(v)
            outline(merged, path + "[]", lines, limit)
            for k, vals in seen.items():
                if len(dicts) >= 5 and 1 < len(vals) <= 60:
                    lines.append(f"{path}[].{k}  values: " + " | ".join(sorted(map(str, vals))))
        else:
            lines.append(f"{path}[]  x{len(node)}  e.g. {json.dumps(node[:3], ensure_ascii=False)[:90]}")
    else:
        lines.append(f"{path}: {type(node).__name__} = {json.dumps(node, ensure_ascii=False)[:70]}")
    return lines


def finished_matches(data):
    """Match ids of finished matches found anywhere in a league payload, in page order."""
    found = {}
    for d in walk(data):
        status = d.get("status")
        if "id" in d and isinstance(status, dict) and status.get("finished") \
                and isinstance(d.get("home"), dict) and isinstance(d.get("away"), dict):
            found[str(d["id"])] = (f'{d["home"].get("name")} - {d["away"].get("name")}',
                                   str(status.get("utcTime", "")))
    return sorted(found.items(), key=lambda kv: kv[1][1])


def describe_league(data):
    matches = finished_matches(data)
    lines = [f"finished matches found: {len(matches)}"]
    if matches:
        mid, (name, when) = matches[-1]
        lines.append(f"latest: {mid}  {name}  {when}")
    seasons = [d.get("allAvailableSeasons") for d in walk(data) if d.get("allAvailableSeasons")]
    if seasons:
        lines.append(f"seasons listed: {len(seasons[0])} (oldest {seasons[0][-1]})")
    return lines, (matches[-1][0] if matches else None)


def describe_match(data):
    """What the tables need: shots with xG and position, players with minutes and xG."""
    shots, players, team_xg = [], 0, False
    for d in walk(data):
        if "expectedGoals" in d and ("x" in d or "min" in d) and "playerName" in d:
            shots.append(d)
        if isinstance(d.get("playerStats"), dict):
            players = max(players, len(d["playerStats"]))
        if d.get("key") == "expected_goals" and "stats" in d:
            team_xg = True
    lines = [f"shots with xG: {len(shots)}"]
    if shots:
        lines.append("shot fields: " + ", ".join(sorted(shots[0])))
    lines.append(f"players with match stats: {players}")
    lines.append(f"team xG in match stats: {'yes' if team_xg else 'not found'}")
    return lines, bool(shots), players > 0


def attempt(label, url, accept, extract, describe, save_as):
    """One request, printed and saved. -> (payload or None, value returned by describe)."""
    print(f"\n[{label}] {url}")
    r = fetch(url, accept)
    if r["error"]:
        print(f"  FAILED  {r['error']}")
        return None, None
    print(f"  HTTP {r['status']}, {r['ctype'] or 'no content type'}, {len(r['body']):,} bytes"
          + (f"\n  redirected to {r['url']}" if r["url"] != url else ""))
    os.makedirs(OUT, exist_ok=True)
    data = extract(r["body"]) if r["status"] == 200 else None
    if data is None:
        with open(os.path.join(OUT, save_as + ".raw.txt"), "wb") as f:
            f.write(r["body"][:200_000])
        start = r["body"][:160].decode("utf-8", "replace").replace("\n", " ").strip()
        print(f"  NO DATA  starts with: {start or '(empty)'}")
        return None, None
    with open(os.path.join(OUT, save_as + ".json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    lines, *rest = describe(data)
    for line in lines:
        print("  " + line)
    return data, rest


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--league", type=int, default=LEAGUE_ID)
    ap.add_argument("--match", help="FotMob match id (default: latest finished match of the league)")
    ap.add_argument("--outline", action="store_true",
                    help="JSON endpoints only (2 requests) and print every field of both answers")
    args = ap.parse_args()
    print(f"FotMob probe - python {sys.version.split()[0]}, "
          f"{'GitHub Actions' if os.environ.get('GITHUB_ACTIONS') else 'local machine'}")

    api_l, a = attempt("league, JSON endpoint", f"{SITE}/api/data/leagues?id={args.league}",
                       "application/json", as_json, describe_league, "league_api")
    page_l, b = (None, None) if args.outline else attempt(
        "league, web page", f"{SITE}/leagues/{args.league}",
        "text/html", from_page, describe_league, "league_page")
    match_id = args.match or (a and a[0]) or (b and b[0])

    api_ok = page_ok = (False, False)
    if match_id:
        api_m, c = attempt("match, JSON endpoint", f"{SITE}/api/data/matchDetails?matchId={match_id}",
                       "application/json", as_json, describe_match, "match_api")
        _, d = (None, None) if args.outline else attempt(
            "match, web page", f"{SITE}/match/{match_id}",
            "text/html", from_page, describe_match, "match_page")
        api_ok, page_ok = tuple(c or (False, False)), tuple(d or (False, False))
    else:
        print("\nNo match id found: pass one with --match (the number in a fotmob.com/match/... link).")

    if args.outline:
        for title, data in (("LEAGUE", api_l), ("MATCH", api_m if match_id else None)):
            print(f"\n=== {title} FIELDS ===")
            print("\n".join(outline(data)) if data is not None else "(no answer)")

    def word(ok):
        return "yes" if ok else "NO"
    if args.outline:
        print("\n(web pages not tested in --outline mode: ignore that column)")
    print("\n=== Result ===")
    print(f"league list   JSON endpoint: {word(api_l is not None)}   web page: {word(page_l is not None)}")
    print(f"match shots   JSON endpoint: {word(api_ok[0])}   web page: {word(page_ok[0])}")
    print(f"match players JSON endpoint: {word(api_ok[1])}   web page: {word(page_ok[1])}")
    print(f"answers saved in {OUT}/")
    usable = (api_l is not None or page_l is not None) and (api_ok[0] or page_ok[0])
    return 0 if usable else 1


if __name__ == "__main__":
    sys.exit(main())
