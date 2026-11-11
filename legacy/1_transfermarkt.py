#!/usr/bin/env python3
"""
scripts/1_transfermarkt.py - build a local football database (top 5 leagues, past seasons)
from the free Transfermarkt open dataset (CC0 licence), for generating stats tweets.

Two stages:
  download -> saves the dataset's CSV files into data/raw_transfermarkt/ (free, ~once; skips files you have)
  build    -> loads the top 5 leagues for the chosen seasons into football.db (SQLite)

Commands
  python3 scripts/1_transfermarkt.py download            # get the files (add --refresh to re-download)
  python3 scripts/1_transfermarkt.py build               # (re)build football.db from data/raw_transfermarkt/
  python3 scripts/1_transfermarkt.py stats               # row counts and sanity checks

No API key, no account, no third-party packages (Python 3.8+).
Optional: pip3 install truststore  (fixes SSL certificate errors on some Macs)

Data source: https://github.com/dcaribou/transfermarkt-datasets
"""

import argparse
import csv
import gzip
import os
import re
from collections import Counter
import sqlite3
import sys
import time
import urllib.error
import urllib.request

try:   # use the computer's own certificate store (fixes SSL errors on macOS / company networks)
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

# ---------------------------------------------------------------- settings
import paths
RAW_DIR = paths.RAW_TM_DIR
DB_FILE = paths.WORK_DB
BASE_URL = "https://pub-e682421888d945d684bcae8890b0ec20.r2.dev/data"
TABLES = ["competitions", "clubs", "players", "games", "appearances",
          "game_events", "transfers", "game_lineups", "player_valuations"]

LEAGUES = {                 # Transfermarkt competition ids
    "GB1": ("Premier League", "England"),
    "ES1": ("La Liga", "Spain"),
    "IT1": ("Serie A", "Italy"),
    "L1": ("Bundesliga", "Germany"),
    "FR1": ("Ligue 1", "France"),
}
SEASONS = [2021, 2022, 2023, 2024, 2025]   # 2021 = season 2021/22 ... 2025 = 2025/26

csv.field_size_limit(10 ** 7)


# ---------------------------------------------------------------- download
def raw_file(table):
    return os.path.join(RAW_DIR, f"{table}.csv.gz")


def download(table, refresh=False):
    path = raw_file(table)
    if os.path.exists(path) and not refresh:
        print(f"  {table:<13} already downloaded ({os.path.getsize(path) / 1e6:.1f} MB)")
        return
    os.makedirs(RAW_DIR, exist_ok=True)
    url = f"{BASE_URL}/{table}.csv.gz"
    tmp = path + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "football-db/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done, last = 0, 0.0
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if time.time() - last > 1:
                    pct = f"{100 * done / total:.0f}%" if total else f"{done / 1e6:.0f} MB"
                    print(f"\r  {table:<13} {pct}", end="", flush=True)
                    last = time.time()
    except urllib.error.HTTPError as e:
        sys.exit(f"\nHTTP {e.code} downloading {url}")
    except urllib.error.URLError as e:
        sys.exit(f"\nNetwork error downloading {url}: {e.reason}\n"
                 f"If this is an SSL/certificate error, run: pip3 install truststore")
    os.replace(tmp, path)     # only keep complete files
    print(f"\r  {table:<13} downloaded ({os.path.getsize(path) / 1e6:.1f} MB)")


def cmd_download(args):
    print("Downloading the Transfermarkt open dataset (CC0 licence)...")
    for table in TABLES:
        download(table, args.refresh)
    print("Done. Run 'build' next.")


# ---------------------------------------------------------------- build helpers
def rows(table):
    path = raw_file(table)
    if not os.path.exists(path):
        sys.exit(f"Missing {path}. Run 'download' first.")
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        yield from csv.DictReader(f)


def to_int(x):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return None


def clean(x):
    x = (x or "").strip()
    return x or None


def goal_detail(description):
    d = (description or "").lower()
    if "own-goal" in d or "own goal" in d:
        return "Own Goal"
    if "penalty" in d:
        return "Penalty"
    return "Normal Goal"


def card_detail(description):
    d = (description or "").lower()
    if "second yellow" in d:
        return "Second Yellow card"
    if "red" in d:
        return "Red Card"
    if "yellow" in d:
        return "Yellow Card"
    return None


EVENT_TYPES = {"Goals": "Goal", "Cards": "Card", "Substitutions": "subst",
               "Shootout": "Shootout"}

CLUB_SUFFIXES = re.compile(
    r"\s+(?:Association Football Club|Football Club|Fútbol Club|Futbol Club|Calcio|"
    r"Club de Fútbol|Sociedad Anónima Deportiva|S\.?A\.?D\.?|S\.?p\.?A\.?|S\.?r\.?l\.?|"
    r"e\.?\s?V\.?|GmbH & Co\.? KGaA|GmbH|AG|FC|AFC|CF|SC)(?=\s|$)", re.IGNORECASE)
CLUB_PREFIXES = re.compile(
    r"^(?:Fußball-Club|Football Club|Futbol Club|Associazione Sportiva|"
    r"Società Sportiva|Unione Calcio)\s+", re.IGNORECASE)
NAME_OVERRIDES_FILE = paths.CLUB_NAMES   # optional: team_id,name


def short_club(name):
    """Fallback display name: drop legal suffixes from an official club name."""
    if not name:
        return name
    out = name
    for _ in range(3):
        out = CLUB_SUFFIXES.sub("", out).strip(" .,-")
    out = CLUB_PREFIXES.sub("", out).strip()
    return out or name


def name_overrides():
    """Optional config/club_names.csv: team_id,name  (your own spellings)."""
    if not os.path.exists(NAME_OVERRIDES_FILE):
        return {}
    with open(NAME_OVERRIDES_FILE, encoding="utf-8-sig", newline="") as f:
        return {to_int(r["team_id"]): r["name"].strip()
                for r in csv.DictReader(f) if to_int(r.get("team_id")) and r.get("name")}

SCHEMA = """
DROP TABLE IF EXISTS leagues;  DROP TABLE IF EXISTS teams;   DROP TABLE IF EXISTS players;
DROP TABLE IF EXISTS fixtures; DROP TABLE IF EXISTS fixture_events;
DROP TABLE IF EXISTS fixture_player_stats; DROP TABLE IF EXISTS transfers;
DROP TABLE IF EXISTS fixture_lineups;      DROP TABLE IF EXISTS market_values;
DROP VIEW  IF EXISTS player_season_stats;  DROP TABLE IF EXISTS player_season_stats;

CREATE TABLE leagues (league_id TEXT PRIMARY KEY, name TEXT, country TEXT);
CREATE TABLE teams   (team_id INTEGER PRIMARY KEY, name TEXT, full_name TEXT);
CREATE TABLE players (
  player_id INTEGER PRIMARY KEY, name TEXT, first_name TEXT, last_name TEXT,
  birth_date TEXT, nationality TEXT, position TEXT, sub_position TEXT, foot TEXT,
  height_cm INTEGER, photo TEXT);

CREATE TABLE fixtures (
  fixture_id INTEGER PRIMARY KEY, league_id TEXT, season INTEGER, round TEXT,
  date_utc TEXT, status TEXT, venue TEXT, referee TEXT, attendance INTEGER,
  home_team_id INTEGER, away_team_id INTEGER, home_goals INTEGER, away_goals INTEGER,
  home_position INTEGER, away_position INTEGER, home_manager TEXT, away_manager TEXT);

-- goals, cards, substitutions. detail: Normal Goal / Penalty / Own Goal / card colour
CREATE TABLE fixture_events (
  fixture_id INTEGER, minute INTEGER, team_id INTEGER,
  player_id INTEGER, player_name TEXT, assist_id INTEGER, assist_name TEXT,
  player_in_id INTEGER, type TEXT, detail TEXT, description TEXT);

-- one row per player per match played
-- started: 1 = in the starting XI, 0 = came off the bench, NULL = no lineup data
CREATE TABLE fixture_player_stats (
  fixture_id INTEGER, player_id INTEGER, team_id INTEGER, minutes INTEGER,
  goals INTEGER, assists INTEGER, penalties_scored INTEGER,
  yellow INTEGER, red INTEGER,
  started INTEGER, captain INTEGER, shirt_number TEXT, position TEXT,
  PRIMARY KEY (fixture_id, player_id));

-- full match squads: starting XI and substitutes (including unused subs)
CREATE TABLE fixture_lineups (
  fixture_id INTEGER, team_id INTEGER, player_id INTEGER, player_name TEXT,
  role TEXT,            -- 'starter' or 'bench'
  position TEXT, shirt_number TEXT, captain INTEGER,
  played INTEGER,       -- 1 if he appeared in the match (always 1 for starters)
  PRIMARY KEY (fixture_id, player_id));

-- market value history (Transfermarkt valuations), whole careers
CREATE TABLE market_values (
  player_id INTEGER, date TEXT, value_eur INTEGER, team_id INTEGER, team_name TEXT,
  PRIMARY KEY (player_id, date));

CREATE TABLE transfers (
  player_id INTEGER, transfer_date TEXT, transfer_season TEXT,
  from_team_id INTEGER, to_team_id INTEGER, from_team TEXT, to_team TEXT,
  fee_eur REAL, market_value_eur REAL);
"""

VIEWS = """
-- season totals per player, per club, per league (a mid-season transfer = 2 rows)
CREATE VIEW player_season_stats AS
SELECT fps.player_id, fps.team_id, f.league_id, f.season,
       COUNT(*) AS appearances,
       SUM(fps.started = 1) AS starts, SUM(fps.started = 0) AS sub_appearances,
       SUM(fps.captain = 1) AS games_as_captain,
       SUM(fps.minutes) AS minutes,
       SUM(fps.goals) AS goals, SUM(fps.assists) AS assists,
       SUM(fps.penalties_scored) AS penalties_scored,
       SUM(fps.yellow) AS yellow, SUM(fps.red) AS red
FROM fixture_player_stats fps
JOIN fixtures f ON f.fixture_id = fps.fixture_id
GROUP BY fps.player_id, fps.team_id, f.league_id, f.season;

DROP VIEW IF EXISTS v_matches;
CREATE VIEW v_matches AS
SELECT f.fixture_id, l.name AS league, f.season, f.round, f.date_utc,
       h.name AS home, f.home_goals, f.away_goals, a.name AS away, f.status
FROM fixtures f
JOIN leagues l ON l.league_id = f.league_id
LEFT JOIN teams h ON h.team_id = f.home_team_id
LEFT JOIN teams a ON a.team_id = f.away_team_id;

DROP VIEW IF EXISTS v_player_club_league_totals;
CREATE VIEW v_player_club_league_totals AS          -- league totals per club, all seasons loaded
SELECT p.player_id, p.name AS player, t.team_id, t.name AS club,
       COUNT(DISTINCT s.season) AS seasons,
       SUM(s.appearances) AS apps, SUM(s.goals) AS goals, SUM(s.assists) AS assists,
       SUM(s.goals) + SUM(s.assists) AS goal_contributions,
       SUM(s.penalties_scored) AS pens_scored, SUM(s.minutes) AS minutes
FROM player_season_stats s
JOIN players p ON p.player_id = s.player_id
JOIN teams t ON t.team_id = s.team_id
GROUP BY s.player_id, s.team_id;

DROP VIEW IF EXISTS v_player_values;
CREATE VIEW v_player_values AS                       -- latest and highest value per player
SELECT p.player_id, p.name AS player,
       (SELECT value_eur FROM market_values m WHERE m.player_id = p.player_id
        ORDER BY date DESC LIMIT 1) AS latest_value_eur,
       (SELECT date FROM market_values m WHERE m.player_id = p.player_id
        ORDER BY date DESC LIMIT 1) AS latest_value_date,
       (SELECT MAX(value_eur) FROM market_values m WHERE m.player_id = p.player_id) AS peak_value_eur,
       (SELECT date FROM market_values m WHERE m.player_id = p.player_id
        ORDER BY value_eur DESC, date LIMIT 1) AS peak_value_date
FROM players p
WHERE EXISTS (SELECT 1 FROM market_values m WHERE m.player_id = p.player_id);

DROP VIEW IF EXISTS v_transfers_with_value;
CREATE VIEW v_transfers_with_value AS                -- value at the move vs value now
SELECT tr.player_id, p.name AS player, tr.transfer_date, tr.from_team, tr.to_team,
       tr.fee_eur,
       (SELECT value_eur FROM market_values m WHERE m.player_id = tr.player_id
        AND m.date <= tr.transfer_date ORDER BY m.date DESC LIMIT 1) AS value_at_transfer_eur,
       v.latest_value_eur, v.latest_value_date
FROM transfers tr
JOIN players p ON p.player_id = tr.player_id
LEFT JOIN v_player_values v ON v.player_id = tr.player_id;
"""


# ---------------------------------------------------------------- build
def cmd_build(args):
    leagues = set(LEAGUES)
    seasons = {str(s) for s in SEASONS}
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.executescript(SCHEMA)
    cur.executemany("INSERT INTO leagues VALUES (?,?,?)",
                    [(k, v[0], v[1]) for k, v in LEAGUES.items()])

    print("Loading matches...")
    game_ids, club_names, used_clubs = set(), {}, set()
    for r in rows("games"):
        if r.get("competition_id") not in leagues or str(r.get("season")) not in seasons:
            continue
        gid = to_int(r["game_id"])
        game_ids.add(gid)
        home, away = to_int(r.get("home_club_id")), to_int(r.get("away_club_id"))
        hg, ag = to_int(r.get("home_club_goals")), to_int(r.get("away_club_goals"))
        club_names.setdefault(home, clean(r.get("home_club_name")))
        club_names.setdefault(away, clean(r.get("away_club_name")))
        used_clubs.update([home, away])
        cur.execute("INSERT OR REPLACE INTO fixtures VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            gid, r["competition_id"], to_int(r["season"]), clean(r.get("round")),
            clean(r.get("date")), "FT" if hg is not None and ag is not None else "NS",
            clean(r.get("stadium")), clean(r.get("referee")), to_int(r.get("attendance")),
            home, away, hg, ag, to_int(r.get("home_club_position")),
            to_int(r.get("away_club_position")), clean(r.get("home_club_manager_name")),
            clean(r.get("away_club_manager_name"))))

    full_names = {}                    # official names, e.g. "... Football Club"
    for r in rows("clubs"):
        cid = to_int(r.get("club_id"))
        if cid is not None and clean(r.get("name")):
            full_names[cid] = clean(r["name"])

    print("Loading goals, cards and substitutions...")
    pens = {}                          # (game, player) -> penalties scored
    events = []
    event_names = {}                   # club_id -> Counter of short names used in match reports
    for r in rows("game_events"):
        gid = to_int(r.get("game_id"))
        if gid not in game_ids:
            continue
        if clean(r.get("club_name")):
            event_names.setdefault(to_int(r.get("club_id")), Counter())[clean(r["club_name"])] += 1
        etype = EVENT_TYPES.get(r.get("type"), r.get("type"))
        desc = clean(r.get("description"))
        detail = goal_detail(desc) if etype == "Goal" else card_detail(desc) if etype == "Card" else None
        pid = to_int(r.get("player_id"))
        if etype == "Goal" and detail == "Penalty":
            pens[(gid, pid)] = pens.get((gid, pid), 0) + 1
        events.append((gid, to_int(r.get("minute")), to_int(r.get("club_id")), pid,
                       to_int(r.get("player_assist_id")), to_int(r.get("player_in_id")),
                       etype, detail, desc))

    print("Loading lineups...")
    lineups = {}                       # (game, player) -> lineup row
    for r in rows("game_lineups"):
        gid = to_int(r.get("game_id"))
        if gid not in game_ids:
            continue
        pid = to_int(r.get("player_id"))
        if pid is None:
            continue
        role = "starter" if r.get("type") == "starting_lineup" else "bench"
        lineups[(gid, pid)] = [gid, to_int(r.get("club_id")), pid, clean(r.get("player_name")),
                               role, clean(r.get("position")), clean(r.get("number")),
                               1 if to_int(r.get("team_captain")) == 1 else 0]
    subbed_on = {(e[0], e[5]) for e in events if e[6] == "subst" and e[5] is not None}

    print("Loading player appearances...")
    player_ids, appeared = set(), set()
    for r in rows("appearances"):
        gid = to_int(r.get("game_id"))
        if gid not in game_ids:
            continue
        pid = to_int(r.get("player_id"))
        player_ids.add(pid)
        appeared.add((gid, pid))
        used_clubs.add(to_int(r.get("player_club_id")))
        lu = lineups.get((gid, pid))
        started = None if lu is None else (1 if lu[4] == "starter" else 0)
        cur.execute("INSERT OR REPLACE INTO fixture_player_stats VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            gid, pid, to_int(r.get("player_club_id")), to_int(r.get("minutes_played")) or 0,
            to_int(r.get("goals")) or 0, to_int(r.get("assists")) or 0,
            pens.get((gid, pid), 0), to_int(r.get("yellow_cards")) or 0,
            to_int(r.get("red_cards")) or 0, started,
            None if lu is None else lu[7], None if lu is None else lu[6],
            None if lu is None else lu[5]))

    for (gid, pid), lu in lineups.items():
        played = 1 if lu[4] == "starter" or (gid, pid) in appeared or (gid, pid) in subbed_on else 0
        player_ids.add(pid)            # unused subs too, so their names are known
        cur.execute("INSERT OR REPLACE INTO fixture_lineups VALUES (?,?,?,?,?,?,?,?,?)",
                    lu + [played])

    print("Loading players, clubs and transfers...")
    names = {}
    for r in rows("players"):
        pid = to_int(r.get("player_id"))
        names[pid] = clean(r.get("name"))
        if pid not in player_ids:
            continue
        cur.execute("INSERT OR REPLACE INTO players VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            pid, clean(r.get("name")), clean(r.get("first_name")), clean(r.get("last_name")),
            (clean(r.get("date_of_birth")) or "")[:10] or None,
            clean(r.get("country_of_citizenship")), clean(r.get("position")),
            clean(r.get("sub_position")), clean(r.get("foot")),
            to_int(r.get("height_in_cm")), clean(r.get("image_url"))))

    for (gid, minute, team, pid, aid, pin, etype, detail, desc) in events:
        cur.execute("INSERT INTO fixture_events VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            gid, minute, team, pid, names.get(pid), aid, names.get(aid), pin,
            etype, detail, desc))

    teams, overrides = [], name_overrides()
    for cid in used_clubs:            # only clubs that appear in the loaded matches
        if cid is None:
            continue
        if cid in overrides:
            teams.append((cid, overrides[cid], full_names.get(cid) or overrides[cid]))
            continue
        full = full_names.get(cid) or club_names.get(cid)
        short = (event_names[cid].most_common(1)[0][0] if cid in event_names
                 else club_names.get(cid) or short_club(full))
        if short and full and short == full:
            short = short_club(full)
        teams.append((cid, short or full or f"club {cid}", full or short))
    cur.executemany("INSERT OR REPLACE INTO teams VALUES (?,?,?)", teams)

    for r in rows("transfers"):
        pid = to_int(r.get("player_id"))
        if pid not in player_ids:
            continue
        cur.execute("INSERT INTO transfers VALUES (?,?,?,?,?,?,?,?,?)", (
            pid, clean(r.get("transfer_date")), clean(r.get("transfer_season")),
            to_int(r.get("from_club_id")), to_int(r.get("to_club_id")),
            clean(r.get("from_club_name")), clean(r.get("to_club_name")),
            to_int(r.get("transfer_fee")), to_int(r.get("market_value_in_eur"))))

    # use the same short club names as everywhere else when the club is in the database
    cur.execute("UPDATE transfers SET from_team = (SELECT name FROM teams WHERE team_id = from_team_id) "
                "WHERE from_team_id IN (SELECT team_id FROM teams)")
    cur.execute("UPDATE transfers SET to_team = (SELECT name FROM teams WHERE team_id = to_team_id) "
                "WHERE to_team_id IN (SELECT team_id FROM teams)")

    print("Loading market values...")
    for r in rows("player_valuations"):
        pid = to_int(r.get("player_id"))
        if pid not in player_ids or not clean(r.get("date")):
            continue
        cur.execute("INSERT OR REPLACE INTO market_values VALUES (?,?,?,?,?)", (
            pid, clean(r["date"])[:10], to_int(r.get("market_value_in_eur")),
            to_int(r.get("current_club_id")), clean(r.get("current_club_name"))))

    cur.executescript(VIEWS)
    for ddl in ["CREATE INDEX ix_ev_game ON fixture_events(fixture_id)",
                "CREATE INDEX ix_ev_player ON fixture_events(player_id)",
                "CREATE INDEX ix_fps_player ON fixture_player_stats(player_id)",
                "CREATE INDEX ix_fx_season ON fixtures(league_id, season)",
                "CREATE INDEX ix_tr_player ON transfers(player_id)",
                "CREATE INDEX ix_lu_player ON fixture_lineups(player_id)",
                "CREATE INDEX ix_mv_player ON market_values(player_id, date)"]:
        cur.execute(ddl)
    con.commit()
    con.close()
    print(f"\nBuilt {DB_FILE}")
    cmd_stats(args)


def cmd_stats(args):
    if not os.path.exists(DB_FILE):
        sys.exit("No football.db yet. Run 'build'.")
    con = sqlite3.connect(DB_FILE)
    for table in ["leagues", "teams", "players", "fixtures", "fixture_events",
                  "fixture_player_stats", "fixture_lineups", "transfers", "market_values"]:
        n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table:<22}{n:>10,}")

    print("\n  matches per league-season (a full season = 380, or 306 for 18-team leagues):")
    for name, season, n in con.execute(
            "SELECT l.name, f.season, COUNT(*) FROM fixtures f JOIN leagues l USING(league_id) "
            "WHERE f.status='FT' GROUP BY 1,2 ORDER BY 1,2"):
        print(f"    {name:<16}{season}/{str(season + 1)[2:]}  {n}")

    goals, pens, own, app_goals = con.execute("""
        SELECT (SELECT COUNT(*) FROM fixture_events WHERE type='Goal'),
               (SELECT COUNT(*) FROM fixture_events WHERE type='Goal' AND detail='Penalty'),
               (SELECT COUNT(*) FROM fixture_events WHERE type='Goal' AND detail='Own Goal'),
               (SELECT SUM(goals) FROM fixture_player_stats)""").fetchone()
    if goals:
        print(f"\n  sanity checks:")
        print(f"    goal events: {goals:,} | goals in appearances: {app_goals or 0:,} "
              f"(should be close; own goals only appear as events)")
        print(f"    penalties detected: {pens:,} ({100 * pens / goals:.1f}% of goals, "
              f"expect roughly 8-12%)")
        print(f"    own goals detected: {own:,} ({100 * own / goals:.1f}%, expect roughly 2-4%)")
        if pens == 0:
            print("    WARNING: no penalties detected. Paste a few rows of "
                  "\"SELECT description FROM fixture_events WHERE type='Goal' LIMIT 20\" to Claude.")
    lu_games, starters, with_lineup, apps = con.execute("""
        SELECT (SELECT COUNT(DISTINCT fixture_id) FROM fixture_lineups),
               (SELECT COUNT(*) FROM fixture_lineups WHERE role='starter'),
               (SELECT COUNT(*) FROM fixture_player_stats WHERE started IS NOT NULL),
               (SELECT COUNT(*) FROM fixture_player_stats)""").fetchone()
    n_fix = con.execute("SELECT COUNT(*) FROM fixtures WHERE status='FT'").fetchone()[0]
    if n_fix:
        print(f"    lineups: {lu_games:,} of {n_fix:,} matches "
              f"({100 * lu_games / n_fix:.1f}%), "
              f"{starters / max(lu_games, 1) / 2:.1f} starters per team (expect 11.0)")
        print(f"    appearances with started/bench info: {100 * with_lineup / max(apps, 1):.1f}%")
    mv_players, players = con.execute("""
        SELECT (SELECT COUNT(DISTINCT player_id) FROM market_values),
               (SELECT COUNT(*) FROM players)""").fetchone()
    if players:
        print(f"    players with market values: {mv_players:,} of {players:,} "
              f"({100 * mv_players / players:.1f}%)")
    con.close()


# ---------------------------------------------------------------- main
def main():
    p = argparse.ArgumentParser(description="Build a local football database.")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download")
    d.add_argument("--refresh", action="store_true", help="download again even if present")
    sub.add_parser("build")
    sub.add_parser("stats")
    args = p.parse_args()
    {"download": cmd_download, "build": cmd_build, "stats": cmd_stats}[args.cmd](args)


if __name__ == "__main__":
    main()
