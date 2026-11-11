#!/usr/bin/env python3
"""
scripts/2_understat.py - add Understat data (xG, shots, current season, upcoming matches)
to football.db, alongside the Transfermarkt history.

Two stages, like scripts/1_transfermarkt.py:
  fetch  -> downloads raw JSON into data/raw_understat/ (resumable, polite: 1 request / 1.5 s)
  build  -> loads everything into us_* tables in football.db (Transfermarkt tables untouched)

Commands
  python3 scripts/2_understat.py update                     # WEEKLY: current season, new + recent matches
  python3 scripts/2_understat.py plan                       # what's downloaded, what's left
  python3 scripts/2_understat.py fetch --no-matches         # league data only: ~30 requests, ~1 min
  python3 scripts/2_understat.py fetch --season 2026        # current season incl. every match's shots
  python3 scripts/2_understat.py fetch --max-requests 2000  # history in chunks (resumes each time)
  python3 scripts/2_understat.py build                      # (re)build the us_* tables
  python3 scripts/2_understat.py stats                      # counts and checks

update (the weekly run) downloads:
  - the current season's league data (results, fixtures, team and player season stats)
  - every played match not downloaded yet, whatever its date (e.g. a postponed match)
  - again, every played match from the last 30 days (--window), to pick up Understat's
    corrections; files downloaded less than 12 hours ago are not fetched again
Older match files are never downloaded again: data/raw_understat/ is the frozen archive.
The current season is worked out from today's date (from July = the new season).

Understat has no official API: this reads the same data its website loads.
Keep the delay, don't run it in parallel, and use the data for your own analysis/tweets.
No third-party packages needed (Python 3.8+). Optional: pip3 install truststore
"""

import argparse
import glob
import gzip
import json
import zlib
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

try:   # use the computer's own certificate store (fixes SSL errors on macOS / company networks)
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

# ---------------------------------------------------------------- settings
import paths
RAW_DIR = paths.RAW_US_DIR
DB_FILE = paths.WORK_DB
BASE_URL = "https://understat.com"

LEAGUES = {                       # Understat code -> (name, Transfermarkt id)
    "EPL": ("Premier League", "GB1"),
    "La_Liga": ("La Liga", "ES1"),
    "Serie_A": ("Serie A", "IT1"),
    "Bundesliga": ("Bundesliga", "L1"),
    "Ligue_1": ("Ligue 1", "FR1"),
}
FIRST_SEASON = 2021
CURRENT_SEASON = date.today().year if date.today().month >= 7 else date.today().year - 1
SEASONS = list(range(FIRST_SEASON, CURRENT_SEASON + 1))   # 2026 = 2026/27
WINDOW_DAYS = 30            # update: played matches this recent are downloaded again
REFETCH_AFTER_HOURS = 12    # ...unless their file is younger than this


# ---------------------------------------------------------------- download
class BudgetSpent(Exception):
    pass


class Client:
    def __init__(self, max_requests, delay):
        self.max_requests, self.delay, self.used, self.last = max_requests, delay, 0, 0.0

    def get_json(self, path):
        if self.used >= self.max_requests:
            raise BudgetSpent()
        wait = self.delay - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(f"{BASE_URL}/{path}", headers={
            "X-Requested-With": "XMLHttpRequest",          # Understat's data endpoints need it
            "User-Agent": "Mozilla/5.0 (compatible; football-db/1.0; personal use)",
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate"})                # formats we can decompress
        self.last = time.time()
        self.used += 1
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(decode_body(resp.read(),
                                                  resp.headers.get("Content-Encoding", "")))
            except urllib.error.HTTPError as e:
                if e.code in (429, 503) and attempt < 2:
                    print(f"  server busy (HTTP {e.code}), waiting 60 s...")
                    time.sleep(60)
                    continue
                if e.code == 404:
                    return None
                sys.exit(f"HTTP {e.code} on {path}. If this keeps happening, Understat may be "
                         f"blocking automated requests: stop and try again later.")
            except urllib.error.URLError as e:
                sys.exit(f"Network error on {path}: {e.reason}\n"
                         f"If this is an SSL/certificate error, run: pip3 install truststore")
            except ValueError as e:     # not JSON, or compressed in a format we can't read
                sys.exit(f"Understat returned data the script can't read for {path} ({e}). "
                         f"The site may have changed: paste this message to Claude.")
        return None


def decode_body(raw, encoding):
    """Decompress gzip/deflate responses (some servers compress even when not asked)."""
    encoding = (encoding or "").lower()
    if raw[:2] == b"\x1f\x8b" or "gzip" in encoding:
        return gzip.decompress(raw)
    if "deflate" in encoding:
        try:
            return zlib.decompress(raw)
        except zlib.error:
            return zlib.decompress(raw, -zlib.MAX_WBITS)    # raw deflate stream
    if "br" in encoding:
        raise ValueError("server used Brotli compression")
    return raw


def league_file(code, season):
    return os.path.join(RAW_DIR, "league", f"{code}_{season}.json")


def match_file(match_id):
    return os.path.join(RAW_DIR, "match", f"{match_id}.json")


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def selected(args):
    leagues = [args.league] if getattr(args, "league", None) else list(LEAGUES)
    seasons = [args.season] if getattr(args, "season", None) else SEASONS
    for lg in leagues:
        if lg not in LEAGUES:
            sys.exit(f"Unknown league {lg}. Use one of: {', '.join(LEAGUES)}")
    return [(lg, s) for lg in leagues for s in seasons]


def finished_matches(code, season):
    path = league_file(code, season)
    if not os.path.exists(path):
        return []
    return [str(m["id"]) for m in read_json(path).get("dates", []) if m.get("isResult")]


def cmd_plan(args):
    print(f"{'league':<16}{'season':<9}{'league data':>12}{'matches done':>14}{'to fetch':>10}")
    total = 0
    for code, s in selected(args):
        have = os.path.exists(league_file(code, s))
        done = [m for m in finished_matches(code, s) if os.path.exists(match_file(m))]
        todo = len(finished_matches(code, s)) - len(done)
        need_league = 1 if (not have or s == CURRENT_SEASON) else 0
        total += need_league + todo
        print(f"{LEAGUES[code][0]:<16}{s}/{str(s + 1)[2:]:<4}"
              f"{'yes' if have else 'no':>12}{len(done):>14}{todo:>10}"
              f"{'  (+~380 once league data is in)' if not have else ''}")
    print(f"\nRequests needed now (estimate): {total}  "
          f"(~{total * 1.6 / 60:.0f} min at the default pace)")


def cmd_fetch(args):
    client = Client(args.max_requests, args.delay)
    try:
        for code, s in selected(args):
            name = f"{LEAGUES[code][0]} {s}/{str(s + 1)[2:]}"
            path = league_file(code, s)
            if not os.path.exists(path) or s == CURRENT_SEASON:
                data = client.get_json(f"getLeagueData/{code}/{s}")
                if data is None:
                    print(f"[{client.used}] {name}: no data on Understat (skipped)")
                    continue
                write_json(path, data)
                played = sum(1 for m in data.get("dates", []) if m.get("isResult"))
                print(f"[{client.used}] {name}: {len(data.get('dates', []))} matches "
                      f"({played} played), {len(data.get('players', []))} players")
            if args.no_matches:
                continue
            todo = [m for m in finished_matches(code, s) if not os.path.exists(match_file(m))]
            for i, mid in enumerate(todo, 1):
                data = client.get_json(f"getMatchData/{mid}")
                if data is not None:
                    write_json(match_file(mid), data)
                if i % 25 == 0 or i == len(todo):
                    print(f"[{client.used}] {name}: match details {i}/{len(todo)}")
    except BudgetSpent:
        print(f"\nStopped after {client.used} requests (budget reached). "
              f"Run the same command again: it resumes where it stopped.")
        return
    except KeyboardInterrupt:
        print(f"\nInterrupted after {client.used} requests. Run again to resume.")
        return
    print(f"\nDone: {client.used} requests. Run 'build' next.")


def cmd_update(args):
    """Weekly run: current season league data, new played matches, recent matches again."""
    client = Client(args.max_requests, args.delay)
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=args.window)).strftime("%Y-%m-%d %H:%M:%S")
    fresh = time.time() - REFETCH_AFTER_HOURS * 3600
    totals = {"new": 0, "again": 0}
    leagues = [args.league] if args.league else list(LEAGUES)
    print(f"Season {CURRENT_SEASON}/{str(CURRENT_SEASON + 1)[2:]}, window: matches played since "
          f"{cutoff[:10]} are downloaded again")
    try:
        for code in leagues:
            if code not in LEAGUES:
                sys.exit(f"Unknown league {code}. Use one of: {', '.join(LEAGUES)}")
            name = f"{LEAGUES[code][0]} {CURRENT_SEASON}/{str(CURRENT_SEASON + 1)[2:]}"
            data = client.get_json(f"getLeagueData/{code}/{CURRENT_SEASON}")
            if data is None:
                print(f"[{client.used}] {name}: no data on Understat yet (skipped)")
                continue
            write_json(league_file(code, CURRENT_SEASON), data)
            played = [m for m in data.get("dates", []) if m.get("isResult")]
            new = [str(m["id"]) for m in played if not os.path.exists(match_file(m["id"]))]
            again = [str(m["id"]) for m in played
                     if os.path.exists(match_file(m["id"])) and str(m.get("datetime", "")) >= cutoff
                     and os.path.getmtime(match_file(m["id"])) < fresh]
            print(f"[{client.used}] {name}: {len(played)} played, {len(new)} new, "
                  f"{len(again)} recent to download again")
            for kind, ids in (("new", new), ("again", again)):
                for mid in ids:
                    match = client.get_json(f"getMatchData/{mid}")
                    if match is not None:
                        write_json(match_file(mid), match)
                        totals[kind] += 1
    except BudgetSpent:
        print(f"\nStopped after {client.used} requests (budget reached). Run again to resume.")
        return
    except KeyboardInterrupt:
        print(f"\nInterrupted after {client.used} requests. Run again to resume.")
        return
    print(f"\nDone: {client.used} requests, {totals['new']} new matches, "
          f"{totals['again']} recent matches refreshed. Run 'build' next.")


# ---------------------------------------------------------------- build
SCHEMA = """
DROP TABLE IF EXISTS us_matches;      DROP TABLE IF EXISTS us_team_matches;
DROP TABLE IF EXISTS us_player_season; DROP TABLE IF EXISTS us_player_match;
DROP TABLE IF EXISTS us_shots;

-- every match of each season, including upcoming fixtures (is_result = 0)
CREATE TABLE us_matches (
  match_id INTEGER PRIMARY KEY, league TEXT, tm_league_id TEXT, season INTEGER,
  datetime TEXT, is_result INTEGER,
  home_id INTEGER, home TEXT, away_id INTEGER, away TEXT,
  home_goals INTEGER, away_goals INTEGER, home_xg REAL, away_xg REAL,
  forecast_home_win REAL, forecast_draw REAL, forecast_away_win REAL);

-- each team's view of each played match: xG for/against, pressing (PPDA), expected points
CREATE TABLE us_team_matches (
  team_id INTEGER, team TEXT, league TEXT, season INTEGER, datetime TEXT, h_a TEXT,
  scored INTEGER, conceded INTEGER, xg REAL, xga REAL, npxg REAL, npxga REAL,
  ppda REAL, ppda_allowed REAL, deep INTEGER, deep_allowed INTEGER,
  xpts REAL, pts INTEGER, result TEXT);

-- season totals per player per league (team = "Club A,Club B" after a mid-season move)
CREATE TABLE us_player_season (
  player_id INTEGER, player TEXT, league TEXT, season INTEGER, team TEXT, position TEXT,
  games INTEGER, minutes INTEGER, goals INTEGER, npg INTEGER, penalty_goals INTEGER,
  assists INTEGER, xg REAL, npxg REAL, xa REAL, shots INTEGER, key_passes INTEGER,
  yellow INTEGER, red INTEGER, xg_chain REAL, xg_buildup REAL,
  PRIMARY KEY (player_id, league, season));

-- one row per player per match (from the match rosters)
CREATE TABLE us_player_match (
  match_id INTEGER, player_id INTEGER, player TEXT, team_id INTEGER, h_a TEXT,
  position TEXT, minutes INTEGER, goals INTEGER, own_goals INTEGER, shots INTEGER,
  xg REAL, assists INTEGER, xa REAL, key_passes INTEGER, yellow INTEGER, red INTEGER,
  xg_chain REAL, xg_buildup REAL, PRIMARY KEY (match_id, player_id));

-- every shot: minute, outcome, xG, position (x, y from 0 to 1), situation, body part
CREATE TABLE us_shots (
  shot_id INTEGER PRIMARY KEY, match_id INTEGER, minute INTEGER, player_id INTEGER,
  player TEXT, h_a TEXT, result TEXT, x REAL, y REAL, xg REAL, situation TEXT,
  shot_type TEXT, last_action TEXT, assisted_by TEXT);
"""

VIEWS = """
-- Understat times are UTC (add 1-2 hours for Paris/Madrid/Rome/Berlin time)
DROP VIEW IF EXISTS us_upcoming;
CREATE VIEW us_upcoming AS                 -- fixtures not played yet, soonest first
SELECT league, season, datetime AS datetime_utc, home, away FROM us_matches
WHERE is_result = 0 AND datetime >= strftime('%Y-%m-%d %H:%M:%S', 'now', '-3 hours')
  AND season = (SELECT MAX(season) FROM us_matches)
ORDER BY datetime;

DROP VIEW IF EXISTS us_postponed;
CREATE VIEW us_postponed AS                -- scheduled date has passed but no result yet
SELECT league, season, datetime AS original_datetime_utc, home, away FROM us_matches
WHERE is_result = 0 AND datetime < strftime('%Y-%m-%d %H:%M:%S', 'now', '-3 hours')
  AND season = (SELECT MAX(season) FROM us_matches)
ORDER BY datetime;

DROP VIEW IF EXISTS us_xg_table;
CREATE VIEW us_xg_table AS                 -- real points vs expected points, per season
SELECT league, season, team, COUNT(*) AS played, SUM(pts) AS points,
       ROUND(SUM(xpts), 1) AS expected_points, ROUND(SUM(pts) - SUM(xpts), 1) AS luck,
       SUM(scored) AS goals_for, ROUND(SUM(xg), 1) AS xg_for,
       SUM(conceded) AS goals_against, ROUND(SUM(xga), 1) AS xg_against
FROM us_team_matches GROUP BY league, season, team_id;

DROP VIEW IF EXISTS us_finishing;
CREATE VIEW us_finishing AS                -- goals vs xG: who finishes above expectation
SELECT player, league, season, team, minutes, goals, npg, ROUND(xg, 1) AS xg,
       ROUND(npxg, 1) AS npxg, ROUND(npg - npxg, 1) AS npg_minus_npxg, shots
FROM us_player_season WHERE shots >= 20;
"""


def num(x, cast=float):
    try:
        return cast(float(x)) if cast is int else cast(x)
    except (TypeError, ValueError):
        return None


def cmd_build(args):
    if not os.path.isdir(RAW_DIR):
        sys.exit("Nothing downloaded yet (no data/raw_understat/ folder). Run 'fetch' first.")
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.executescript(SCHEMA)
    match_meta = {}
    for path in sorted(glob.glob(os.path.join(RAW_DIR, "league", "*.json"))):
        code, season = os.path.basename(path)[:-5].rsplit("_", 1)
        season = int(season)
        league, tm_id = LEAGUES.get(code, (code, None))
        data = read_json(path)
        for m in data.get("dates", []):
            mid = num(m.get("id"), int)
            match_meta[str(mid)] = (league, season)
            fc = m.get("forecast") or {}
            cur.execute("INSERT OR REPLACE INTO us_matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                mid, league, tm_id, season, m.get("datetime"), 1 if m.get("isResult") else 0,
                num((m.get("h") or {}).get("id"), int), (m.get("h") or {}).get("title"),
                num((m.get("a") or {}).get("id"), int), (m.get("a") or {}).get("title"),
                num((m.get("goals") or {}).get("h"), int), num((m.get("goals") or {}).get("a"), int),
                num((m.get("xG") or {}).get("h")), num((m.get("xG") or {}).get("a")),
                num(fc.get("w")), num(fc.get("d")), num(fc.get("l"))))
        for t in (data.get("teams") or {}).values():
            for h in t.get("history", []):
                ppda = h.get("ppda") or {}
                ppda_a = h.get("ppda_allowed") or {}
                cur.execute("INSERT INTO us_team_matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    num(t.get("id"), int), t.get("title"), league, season, h.get("date"),
                    h.get("h_a"), num(h.get("scored"), int), num(h.get("missed"), int),
                    num(h.get("xG")), num(h.get("xGA")), num(h.get("npxG")), num(h.get("npxGA")),
                    round(ppda["att"] / ppda["def"], 2) if ppda.get("def") else None,
                    round(ppda_a["att"] / ppda_a["def"], 2) if ppda_a.get("def") else None,
                    num(h.get("deep"), int), num(h.get("deep_allowed"), int),
                    num(h.get("xpts")), num(h.get("pts"), int), h.get("result")))
        for p in data.get("players", []):
            goals, npg = num(p.get("goals"), int), num(p.get("npg"), int)
            cur.execute("INSERT OR REPLACE INTO us_player_season VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                num(p.get("id"), int), p.get("player_name"), league, season, p.get("team_title"),
                p.get("position"), num(p.get("games"), int), num(p.get("time"), int), goals, npg,
                (goals - npg) if goals is not None and npg is not None else None,
                num(p.get("assists"), int), num(p.get("xG")), num(p.get("npxG")), num(p.get("xA")),
                num(p.get("shots"), int), num(p.get("key_passes"), int),
                num(p.get("yellow_cards"), int), num(p.get("red_cards"), int),
                num(p.get("xGChain")), num(p.get("xGBuildup"))))

    files = glob.glob(os.path.join(RAW_DIR, "match", "*.json"))
    print(f"Loading {len(files):,} match files...")
    for path in files:
        data = read_json(path)
        mid = num(os.path.basename(path)[:-5], int)
        for side in ("h", "a"):
            for r in ((data.get("rosters") or {}).get(side) or {}).values():
                cur.execute("INSERT OR REPLACE INTO us_player_match VALUES "
                            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    mid, num(r.get("player_id"), int), r.get("player"), num(r.get("team_id"), int),
                    r.get("h_a"), r.get("position"), num(r.get("time"), int),
                    num(r.get("goals"), int), num(r.get("own_goals"), int), num(r.get("shots"), int),
                    num(r.get("xG")), num(r.get("assists"), int), num(r.get("xA")),
                    num(r.get("key_passes"), int), num(r.get("yellow_card"), int),
                    num(r.get("red_card"), int), num(r.get("xGChain")), num(r.get("xGBuildup"))))
            for s in ((data.get("shots") or {}).get(side) or []):
                cur.execute("INSERT OR REPLACE INTO us_shots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    num(s.get("id"), int), mid, num(s.get("minute"), int),
                    num(s.get("player_id"), int), s.get("player"), s.get("h_a"), s.get("result"),
                    num(s.get("X")), num(s.get("Y")), num(s.get("xG")), s.get("situation"),
                    s.get("shotType"), s.get("lastAction"), s.get("player_assisted")))

    cur.executescript(VIEWS)
    for ddl in ["CREATE INDEX IF NOT EXISTS ix_us_pm_player ON us_player_match(player_id)",
                "CREATE INDEX IF NOT EXISTS ix_us_shots_match ON us_shots(match_id)",
                "CREATE INDEX IF NOT EXISTS ix_us_shots_player ON us_shots(player_id)",
                "CREATE INDEX IF NOT EXISTS ix_us_m_season ON us_matches(league, season)"]:
        cur.execute(ddl)
    con.commit()
    con.close()
    print(f"Built Understat tables in {DB_FILE}")
    cmd_stats(args)


def cmd_stats(args):
    if not os.path.exists(DB_FILE):
        sys.exit("No football.db yet.")
    con = sqlite3.connect(DB_FILE)
    try:
        for table in ["us_matches", "us_team_matches", "us_player_season",
                      "us_player_match", "us_shots"]:
            n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table:<20}{n:>10,}")
    except sqlite3.OperationalError:
        sys.exit("No Understat tables yet. Run 'build'.")
    print("\n  per league-season: played / scheduled, match details downloaded")
    rows = con.execute("""
        SELECT m.league, m.season, SUM(m.is_result), COUNT(*),
               SUM(m.is_result AND EXISTS (SELECT 1 FROM us_player_match pm
                                           WHERE pm.match_id = m.match_id))
        FROM us_matches m GROUP BY 1, 2 ORDER BY 1, 2""").fetchall()
    for league, season, played, total, detailed in rows:
        print(f"    {league:<16}{season}/{str(season + 1)[2:]}  {played:>3} / {total:<3}"
              f"  details: {detailed}/{played}")
    goals, shots_goals = con.execute("""
        SELECT (SELECT SUM(home_goals + away_goals) FROM us_matches m WHERE is_result = 1
                AND EXISTS (SELECT 1 FROM us_shots s WHERE s.match_id = m.match_id)),
               (SELECT COUNT(*) FROM us_shots WHERE result IN ('Goal', 'OwnGoal'))""").fetchone()
    if goals:
        print(f"\n  check: goals in results {goals:,} vs goals in shot data {shots_goals:,} "
              f"(should be close)")
    nxt = con.execute("SELECT datetime_utc, home, away FROM us_upcoming LIMIT 3").fetchall()
    if nxt:
        print("\n  next fixtures (UTC):")
        for d, h, a in nxt:
            print(f"    {d}  {h} vs {a}")
    late = con.execute("SELECT original_datetime_utc, home, away FROM us_postponed").fetchall()
    if late:
        print(f"\n  postponed / awaiting result: {len(late)}")
        for d, h, a in late[:5]:
            print(f"    was {d}  {h} vs {a}")
    con.close()


# ---------------------------------------------------------------- main
def main():
    p = argparse.ArgumentParser(description="Add Understat data to football.db.")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("update", "plan", "fetch", "build", "stats"):
        sp = sub.add_parser(name)
        if name in ("update", "plan", "fetch"):
            sp.add_argument("--league", help="one league: " + ", ".join(LEAGUES))
        if name in ("plan", "fetch"):
            sp.add_argument("--season", type=int, help="one season start year, e.g. 2026")
        if name == "update":
            sp.add_argument("--window", type=int, default=WINDOW_DAYS,
                            help=f"days of recent matches to download again (default {WINDOW_DAYS})")
        if name in ("update", "fetch"):
            sp.add_argument("--max-requests", type=int, default=100000)
            sp.add_argument("--delay", type=float, default=1.5,
                            help="seconds between requests (default 1.5; please keep >= 1)")
        if name == "fetch":
            sp.add_argument("--no-matches", action="store_true",
                            help="league data only (results, fixtures, season stats)")
    args = p.parse_args()
    if getattr(args, "delay", 1.5) < 1:
        sys.exit("Please keep --delay at 1 second or more: Understat is a free site.")
    {"update": cmd_update, "plan": cmd_plan, "fetch": cmd_fetch, "build": cmd_build,
     "stats": cmd_stats}[args.cmd](args)


if __name__ == "__main__":
    main()
