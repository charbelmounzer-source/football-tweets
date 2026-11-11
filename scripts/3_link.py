#!/usr/bin/env python3
"""
scripts/3_link.py - connect Understat (us_* tables) with Transfermarkt (the other tables)
inside football.db: same matches, same clubs, same players.

  python3 scripts/3_link.py          # build / rebuild all links and the combined views
  python3 scripts/3_link.py report   # show link quality and the biggest unlinked players

Run it again after any `scripts/1_transfermarkt.py build` or `scripts/2_understat.py build`.

How it links
  1. matches  - same league, same date (+/- 1 day), same score; then same home/away clubs
  2. clubs    - from the linked matches; clubs with no shared matches (e.g. just
                promoted) by name, against every club in the Transfermarkt files
  3. players  - per league-season, inside the linked clubs: name similarity plus
                minutes / appearances / goals (near-identical in both sources);
                players new to the top 5 leagues by exact name in the Transfermarkt files

Fix any wrong or missing link in config/link_overrides.csv (kind,us_id,tm_id), e.g.
  player,8260,418560
  team,229,11
then run scripts/3_link.py again. An empty tm_id removes a link.

No third-party packages needed (Python 3.8+).
"""

import csv
import difflib
import gzip
import os
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import date, timedelta

import paths
DB_FILE = paths.WORK_DB
RAW_DIR = paths.RAW_TM_DIR
OVERRIDES = paths.LINK_OVERRIDES
csv.field_size_limit(10 ** 7)

# ---------------------------------------------------------------- names
TRANSLIT = str.maketrans({"ø": "o", "Ø": "o", "ß": "ss", "æ": "ae", "Æ": "ae", "ł": "l",
                          "Ł": "l", "đ": "d", "Đ": "d", "ı": "i", "œ": "oe", "þ": "th",
                          "ð": "d", "-": " ", "'": "", "’": "", ".": " "})
CLUB_WORDS = {"fc", "afc", "cf", "sc", "ac", "as", "ss", "us", "ssc", "sv", "vfl", "vfb",
              "tsg", "fsv", "1", "club", "de", "calcio", "football", "futbol", "the",
              "cd", "ud", "rc", "rcd", "sd", "ca", "og", "stade", "olympique",
              "rb", "bsc", "sporting", "1899", "1900", "1909", "04", "05", "1846", "1907"}


def norm(name):
    s = unicodedata.normalize("NFKD", (name or "").translate(TRANSLIT))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", s).split())


def name_sim(a, b):
    """0..1 similarity for player names, tolerant of missing first names / accents."""
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = a.split(), b.split()
    if ta[-1] == tb[-1] and (ta[0][0] == tb[0][0] or len(ta) == 1 or len(tb) == 1):
        return 0.92                                    # same surname, same initial
    if set(ta) <= set(tb) or set(tb) <= set(ta):
        return 0.85                                    # "Rodri" vs "Rodri Hernandez"
    overlap = len(set(ta) & set(tb)) / len(set(ta) | set(tb))
    return max(difflib.SequenceMatcher(None, a, b).ratio() * 0.9, overlap * 0.85)


def club_key(name):
    words = [w for w in norm(name).split() if w not in CLUB_WORDS]
    return " ".join(words) or norm(name)


def club_sim(a, b):
    ka, kb = club_key(a), club_key(b)
    if len(ka) < 3 or len(kb) < 3:      # e.g. a name in Cyrillic becomes empty: never a match
        return 0.0
    if ka == kb:
        return 1.0
    wa, wb = set(ka.split()), set(kb.split())
    if wa <= wb or wb <= wa:            # whole words contained: "malaga" vs "malaga cf"
        return 0.9
    return difflib.SequenceMatcher(None, ka, kb).ratio()


def raw_rows(table):
    path = os.path.join(RAW_DIR, f"{table}.csv.gz")
    if not os.path.exists(path):
        return []
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def to_int(x):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 1. matches
def link_matches(con):
    tm = defaultdict(list)
    for fid, lg, season, d, h, a, hg, ag in con.execute(
            "SELECT fixture_id, league_id, season, date_utc, home_team_id, away_team_id, "
            "home_goals, away_goals FROM fixtures WHERE status='FT'"):
        tm[(lg, season, str(d)[:10], hg, ag)].append((fid, h, a))
    us = con.execute("SELECT match_id, tm_league_id, season, datetime, home_id, away_id, "
                     "home_goals, away_goals FROM us_matches WHERE is_result=1").fetchall()
    links, ambiguous = {}, []
    for mid, lg, season, dt, h, a, hg, ag in us:
        try:
            d0 = date.fromisoformat(str(dt)[:10])
        except ValueError:
            continue
        cands = []
        for delta in (0, -1, 1):
            cands += tm.get((lg, season, (d0 + timedelta(days=delta)).isoformat(), hg, ag), [])
        if len(cands) == 1:
            links[mid] = (cands[0][0], "date_score")
        elif cands:
            ambiguous.append((mid, h, a, cands))
    return links, ambiguous, us


# ---------------------------------------------------------------- 2. clubs
def link_teams(con, match_links):
    us_home = {mid: (h, a) for mid, h, a in con.execute(
        "SELECT match_id, home_id, away_id FROM us_matches")}
    tm_teams = {fid: (h, a) for fid, h, a in con.execute(
        "SELECT fixture_id, home_team_id, away_team_id FROM fixtures")}
    votes = defaultdict(Counter)
    for mid, (fid, _) in match_links.items():
        (uh, ua), (th, ta) = us_home[mid], tm_teams[fid]
        votes[uh][th] += 1
        votes[ua][ta] += 1
    team_map = {}
    for us_id, c in votes.items():
        tm_id, n = c.most_common(1)[0]
        if n >= 3 and n / sum(c.values()) >= 0.8:
            team_map[us_id] = (tm_id, "matches", round(n / sum(c.values()), 3))
    return team_map


def link_teams_by_name(con, team_map):
    """Clubs without shared matches (e.g. promoted this season): match names."""
    us_names = dict(con.execute(
        "SELECT home_id, home FROM us_matches UNION SELECT away_id, away FROM us_matches"))
    taken = {v[0] for v in team_map.values()}
    pool = {}
    for r in raw_rows("clubs"):
        cid = to_int(r.get("club_id"))
        if cid and r.get("name"):
            pool[cid] = [r["name"]]
    for cid, name, full in con.execute("SELECT team_id, name, full_name FROM teams"):
        pool.setdefault(cid, []).extend([n for n in (name, full) if n])
    for us_id, us_name in us_names.items():
        if us_id in team_map or not us_name:
            continue
        scored = sorted(((max(club_sim(us_name, n) for n in names), cid)
                         for cid, names in pool.items() if cid not in taken), reverse=True)
        if scored and scored[0][0] >= 0.85 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.05):
            team_map[us_id] = (scored[0][1], "name", round(scored[0][0], 3))
            taken.add(scored[0][1])


def resolve_more_matches(con, match_links, ambiguous, us, team_map):
    """Use the club map: break same-day same-score ties, then link by home/away pairing."""
    tmap = {k: v[0] for k, v in team_map.items()}
    # drop date+score links whose clubs disagree (e.g. Union Berlin v Bochum 14/12/2024: 1-1 on
    # Understat, awarded 0-2 on Transfermarkt, was matched to another 1-1 game the next day)
    tm_fix = {fid: (h, a) for fid, h, a in con.execute(
        "SELECT fixture_id, home_team_id, away_team_id FROM fixtures")}
    us_teams = {row[0]: (row[4], row[5]) for row in us}
    for mid, (fid, _) in list(match_links.items()):
        h, a = us_teams.get(mid, (None, None))
        if h in tmap and a in tmap and (tmap[h], tmap[a]) != tm_fix.get(fid):
            del match_links[mid]
    for mid, h, a, cands in ambiguous:
        hits = [c for c in cands if tmap.get(h) == c[1] and tmap.get(a) == c[2]]
        if len(hits) == 1:
            match_links[mid] = (hits[0][0], "date_score_teams")
    pairs = defaultdict(list)
    for fid, lg, season, h, a in con.execute(
            "SELECT fixture_id, league_id, season, home_team_id, away_team_id FROM fixtures "
            "WHERE status='FT'"):
        pairs[(lg, season, h, a)].append(fid)
    used = {v[0] for v in match_links.values()}
    for mid, lg, season, dt, h, a, hg, ag in us:
        if mid in match_links or h not in tmap or a not in tmap:
            continue
        fids = [f for f in pairs.get((lg, season, tmap[h], tmap[a]), []) if f not in used]
        if len(fids) == 1:
            match_links[mid] = (fids[0], "teams")   # same fixture, score differs between sources
            used.add(fids[0])


# ---------------------------------------------------------------- 3. players
def link_players(con, team_map):
    tmap = {k: v[0] for k, v in team_map.items()}
    title_to_id = {}
    for tid, title in con.execute(
            "SELECT home_id, home FROM us_matches UNION ALL SELECT away_id, away FROM us_matches"):
        title_to_id.setdefault(title, Counter())[tid] += 1
    title_to_id = {t: c.most_common(1)[0][0] for t, c in title_to_id.items()}
    league_ids = dict(con.execute("SELECT name, league_id FROM leagues"))

    tm_pool = defaultdict(lambda: defaultdict(lambda: [None, 0, 0, 0]))   # (lg,season,team)->pid->stats
    tm_names = dict(con.execute("SELECT player_id, name FROM players"))
    for pid, team, lg, season, apps, mins, goals in con.execute(
            "SELECT player_id, team_id, league_id, season, appearances, minutes, goals "
            "FROM player_season_stats"):
        st = tm_pool[(lg, season, team)][pid]
        st[0], st[1], st[2], st[3] = tm_names.get(pid), apps or 0, mins or 0, goals or 0

    votes = defaultdict(Counter)
    us_rows = con.execute("SELECT player_id, player, league, season, team, games, minutes, goals "
                          "FROM us_player_season").fetchall()
    by_ls = defaultdict(list)
    for row in us_rows:
        by_ls[(row[2], row[3])].append(row)
    for (league, season), rows in by_ls.items():
        lg = league_ids.get(league)
        pairs = []
        for us_pid, us_name, _, _, team_title, games, mins, goals in rows:
            tm_ids = {tmap.get(title_to_id.get(t.strip())) for t in (team_title or "").split(",")}
            cands = defaultdict(lambda: [None, 0, 0, 0])
            for tid in tm_ids - {None}:
                for pid, st in tm_pool.get((lg, season, tid), {}).items():
                    c = cands[pid]
                    c[0] = st[0]
                    c[1] += st[1]; c[2] += st[2]; c[3] += st[3]
            for pid, (tm_name, apps, tm_mins, tm_goals) in cands.items():
                ns = name_sim(us_name, tm_name)
                ms = 1 - abs((mins or 0) - tm_mins) / max(mins or 0, tm_mins, 90)
                as_ = 1 - abs((games or 0) - apps) / max(games or 0, apps, 1)
                gs = 1 - abs((goals or 0) - tm_goals) / max(goals or 0, tm_goals, 1)
                stats = 0.5 * ms + 0.25 * as_ + 0.25 * gs
                total = 0.55 * ns + 0.45 * stats
                if total >= 0.6 or (ns >= 0.92 and stats >= 0.3):
                    pairs.append((total, us_pid, pid))
        done_us, done_tm = set(), set()
        for total, us_pid, pid in sorted(pairs, reverse=True):   # best pairs first, one-to-one
            if us_pid in done_us or pid in done_tm:
                continue
            done_us.add(us_pid)
            done_tm.add(pid)
            votes[us_pid][pid] += total

    links = {}
    for us_pid, c in votes.items():
        (pid, score), *rest = c.most_common()
        conflict = bool(rest) and rest[0][1] > 0.5 * score
        links[us_pid] = (pid, "season_stats" + ("_conflict" if conflict else ""),
                         round(score, 3))
    return links


def link_new_players(con, player_links, team_map):
    """Players not found via stats (e.g. new to the top 5 leagues): exact name match."""
    tm_by_name = defaultdict(list)
    profiles = {}
    for r in raw_rows("players"):
        pid = to_int(r.get("player_id"))
        if pid:
            tm_by_name[norm(r.get("name"))].append(pid)
            profiles[pid] = r
    if not profiles:
        return 0
    taken = {v[0] for v in player_links.values()}
    tmap = {k: v[0] for k, v in team_map.items()}
    title_to_id = dict(con.execute(
        "SELECT home, home_id FROM us_matches UNION SELECT away, away_id FROM us_matches"))
    def plausible(pid, season):
        """Of playing age in that season. (last_season is not used here: in this dataset it
        means 'last season in a covered league', so second-division players look inactive.)"""
        born = to_int((profiles[pid].get("date_of_birth") or "")[:4])
        return born is None or 16 <= season - born <= 37

    def most_recent(cands):
        """The single candidate with the latest last_season, if there is a clear one."""
        seasons = sorted(((to_int(profiles[p].get("last_season")) or 0, p) for p in cands),
                         reverse=True)
        if len(seasons) == 1 or (seasons and seasons[0][0] > seasons[1][0]):
            return [seasons[0][1]]
        return []

    added = 0
    for us_pid, us_name, team_title, season in con.execute(
            "SELECT player_id, player, team, MAX(season) FROM us_player_season GROUP BY player_id"):
        if us_pid in player_links:
            continue
        clubs = {tmap.get(title_to_id.get(t.strip())) for t in (team_title or "").split(",")} - {None}
        cands = [p for p in tm_by_name.get(norm(us_name), [])
                 if p not in taken and plausible(p, season)]
        same_club = [p for p in cands if to_int(profiles[p].get("current_club_id")) in clubs]
        if len(norm(us_name).split()) == 1:
            cands = same_club              # one-word nicknames ("Rafita"): club must match too
        elif len(cands) > 1:               # namesakes: the one at the right club, else the one
            cands = same_club if len(same_club) == 1 else most_recent(cands)   # seen most recently
        if len(cands) == 1:
            player_links[us_pid] = (cands[0], "name_only", 0.7)
            taken.add(cands[0])
            added += 1
    return added


def add_missing_tm_players(con, player_links):
    """Profiles, values and transfers for linked players not yet in the database."""
    have = {r[0] for r in con.execute("SELECT player_id FROM players")}
    need = {v[0] for v in player_links.values()} - have
    if not need:
        return 0
    for r in raw_rows("players"):
        pid = to_int(r.get("player_id"))
        if pid in need:
            con.execute("INSERT OR IGNORE INTO players VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                pid, r.get("name"), r.get("first_name"), r.get("last_name"),
                (r.get("date_of_birth") or "")[:10] or None, r.get("country_of_citizenship"),
                r.get("position"), r.get("sub_position"), r.get("foot"),
                to_int(r.get("height_in_cm")), r.get("image_url")))
    for r in raw_rows("player_valuations"):
        pid = to_int(r.get("player_id"))
        if pid in need and r.get("date"):
            con.execute("INSERT OR IGNORE INTO market_values VALUES (?,?,?,?,?)", (
                pid, r["date"][:10], to_int(r.get("market_value_in_eur")),
                to_int(r.get("current_club_id")), r.get("current_club_name")))
    for r in raw_rows("transfers"):
        pid = to_int(r.get("player_id"))
        if pid in need:
            con.execute("INSERT INTO transfers VALUES (?,?,?,?,?,?,?,?,?)", (
                pid, r.get("transfer_date"), r.get("transfer_season"),
                to_int(r.get("from_club_id")), to_int(r.get("to_club_id")),
                r.get("from_club_name"), r.get("to_club_name"),
                to_int(r.get("transfer_fee")), to_int(r.get("market_value_in_eur"))))
    return len(need)


def apply_overrides(team_map, player_links):
    if not os.path.exists(OVERRIDES):
        return 0
    n = 0
    with open(OVERRIDES, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            kind, us_id, tm_id = (r.get("kind") or "").strip(), to_int(r.get("us_id")), to_int(r.get("tm_id"))
            target = team_map if kind == "team" else player_links if kind == "player" else None
            if target is None or us_id is None:
                continue
            if tm_id is None:
                target.pop(us_id, None)
            else:
                target[us_id] = (tm_id, "override", 1.0)
            n += 1
    return n


# ---------------------------------------------------------------- views
VIEWS = """
DROP VIEW IF EXISTS us_player_match_full;
CREATE VIEW us_player_match_full AS          -- Understat match rows + started flag + Transfermarkt id
SELECT pm.*, CASE WHEN pm.position = 'Sub' THEN 0 ELSE 1 END AS started,
       lp.tm_player_id, m.datetime, m.league, m.season
FROM us_player_match pm
JOIN us_matches m ON m.match_id = pm.match_id
LEFT JOIN link_players lp ON lp.us_player_id = pm.player_id;

DROP VIEW IF EXISTS v_players_linked;
CREATE VIEW v_players_linked AS              -- every linked player with his Transfermarkt profile
SELECT lp.us_player_id, lp.us_name, lp.tm_player_id, p.name AS tm_name, lp.method, lp.score,
       p.birth_date, p.nationality, p.position, p.sub_position, p.foot, p.height_cm
FROM link_players lp LEFT JOIN players p ON p.player_id = lp.tm_player_id;

DROP VIEW IF EXISTS v_current_season_players;
CREATE VIEW v_current_season_players AS      -- current season (Understat) + profile, age, value, new-signing flag
WITH cur AS (SELECT MAX(season) s FROM us_matches),
last_tm AS (                                  -- last Transfermarkt top-5 club(s) before this season
  SELECT s.player_id, GROUP_CONCAT(DISTINCT s.team_id) AS team_ids,
         GROUP_CONCAT(DISTINCT t.name) AS clubs, MAX(s.season) AS season
  FROM player_season_stats s JOIN teams t USING(team_id)
  WHERE s.season = (SELECT MAX(season) FROM player_season_stats ps WHERE ps.player_id = s.player_id)
  GROUP BY s.player_id),
latest_move AS (
  SELECT player_id, MAX(transfer_date) AS d FROM transfers GROUP BY player_id)
SELECT ups.player_id AS us_player_id, ups.player, ups.team AS club, ups.league,
       ups.games, ups.minutes, ups.goals, ups.npg, ups.assists,
       ROUND(ups.xg, 2) AS xg, ROUND(ups.npxg, 2) AS npxg, ROUND(ups.xa, 2) AS xa, ups.shots,
       lp.tm_player_id, p.birth_date,
       CAST((julianday('now') - julianday(p.birth_date)) / 365.25 AS INTEGER) AS age,
       p.nationality, p.foot, p.height_cm,
       v.latest_value_eur, v.latest_value_date,
       lt.clubs AS last_tm_club, lt.season AS last_tm_season,
       CASE WHEN lp.tm_player_id IS NULL THEN NULL
            WHEN lt.player_id IS NULL THEN 1          -- never played top-5 league 2021-26: new arrival
            WHEN lt.season < (SELECT s FROM cur) - 1 THEN 1
            WHEN EXISTS (SELECT 1 FROM link_teams ltm
                         WHERE ',' || ups.team || ',' LIKE '%,' || ltm.us_team || ',%'
                           AND (',' || lt.team_ids || ',') LIKE '%,' || ltm.tm_team_id || ',%')
                 THEN 0
            ELSE 1 END AS new_signing,
       tr.transfer_date AS latest_transfer_date, tr.from_team AS latest_transfer_from,
       tr.to_team AS latest_transfer_to, tr.fee_eur AS latest_transfer_fee_eur
FROM us_player_season ups
JOIN cur ON ups.season = cur.s
LEFT JOIN link_players lp ON lp.us_player_id = ups.player_id
LEFT JOIN players p ON p.player_id = lp.tm_player_id
LEFT JOIN v_player_values v ON v.player_id = lp.tm_player_id
LEFT JOIN last_tm lt ON lt.player_id = lp.tm_player_id
LEFT JOIN latest_move lm ON lm.player_id = lp.tm_player_id
LEFT JOIN transfers tr ON tr.player_id = lm.player_id AND tr.transfer_date = lm.d;

DROP VIEW IF EXISTS v_season_combined;
CREATE VIEW v_season_combined AS             -- 2021/22-2025/26: Transfermarkt season stats + Understat xG,
WITH s AS (                                    -- one row per player per league-season
  SELECT ps.player_id, ps.league_id, ps.season, GROUP_CONCAT(t.name, ', ') AS clubs,
         SUM(ps.appearances) AS appearances, SUM(ps.starts) AS starts,
         SUM(ps.minutes) AS minutes, SUM(ps.goals) AS goals, SUM(ps.assists) AS assists,
         SUM(ps.penalties_scored) AS penalties_scored
  FROM player_season_stats ps JOIN teams t ON t.team_id = ps.team_id
  GROUP BY ps.player_id, ps.league_id, ps.season)
SELECT s.player_id AS tm_player_id, p.name AS player, s.clubs, l.name AS league, s.season,
       s.appearances, s.starts, s.minutes, s.goals, s.assists, s.penalties_scored,
       ROUND(u.xg, 2) AS xg, ROUND(u.npxg, 2) AS npxg, ROUND(u.xa, 2) AS xa,
       u.shots, u.key_passes
FROM s
JOIN players p ON p.player_id = s.player_id
JOIN leagues l ON l.league_id = s.league_id
LEFT JOIN link_players lp ON lp.tm_player_id = s.player_id
LEFT JOIN us_player_season u ON u.player_id = lp.us_player_id AND u.league = l.name
                             AND u.season = s.season;
"""


# ---------------------------------------------------------------- main
def build():
    if not os.path.exists(DB_FILE):
        sys.exit("football.db not found.")
    con = sqlite3.connect(DB_FILE)
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    for t in ("fixtures", "players", "us_matches", "us_player_season"):
        if t not in have:
            sys.exit(f"Table {t} missing: run scripts/1_transfermarkt.py build and scripts/2_understat.py build first.")
    print("Linking matches...")
    match_links, ambiguous, us = link_matches(con)
    print("Linking clubs...")
    team_map = link_teams(con, match_links)
    resolve_more_matches(con, match_links, ambiguous, us, team_map)
    team_map = link_teams(con, match_links)          # re-vote with the extra matches
    link_teams_by_name(con, team_map)
    print("Linking players...")
    player_links = link_players(con, team_map)
    n_new = link_new_players(con, player_links, team_map)
    n_over = apply_overrides(team_map, player_links)
    n_added = add_missing_tm_players(con, player_links)

    con.executescript("""
    DROP TABLE IF EXISTS link_matches; DROP TABLE IF EXISTS link_teams; DROP TABLE IF EXISTS link_players;
    CREATE TABLE link_matches (us_match_id INTEGER PRIMARY KEY, tm_fixture_id INTEGER, method TEXT);
    CREATE TABLE link_teams (us_team_id INTEGER PRIMARY KEY, us_team TEXT, tm_team_id INTEGER,
                             tm_team TEXT, method TEXT, score REAL);
    CREATE TABLE link_players (us_player_id INTEGER PRIMARY KEY, us_name TEXT, tm_player_id INTEGER,
                               method TEXT, score REAL);
    """)
    con.executemany("INSERT INTO link_matches VALUES (?,?,?)",
                    [(m, f, meth) for m, (f, meth) in match_links.items()])
    us_team_names = dict(con.execute(
        "SELECT home_id, home FROM us_matches UNION SELECT away_id, away FROM us_matches"))
    tm_team_names = dict(con.execute("SELECT team_id, name FROM teams"))
    tm_team_names.update({to_int(r["club_id"]): r["name"] for r in raw_rows("clubs")
                          if to_int(r.get("club_id")) not in tm_team_names})
    con.executemany("INSERT INTO link_teams VALUES (?,?,?,?,?,?)", [
        (u, us_team_names.get(u), t, tm_team_names.get(t), meth, sc)
        for u, (t, meth, sc) in team_map.items()])
    us_player_names = dict(con.execute("SELECT player_id, player FROM us_player_season"))
    con.executemany("INSERT INTO link_players VALUES (?,?,?,?,?)", [
        (u, us_player_names.get(u), t, meth, sc) for u, (t, meth, sc) in player_links.items()])
    con.execute("CREATE INDEX IF NOT EXISTS ix_lp_tm ON link_players(tm_player_id)")
    con.executescript(VIEWS)
    con.commit()
    print(f"  {len(match_links):,} matches, {len(team_map)} clubs, {len(player_links):,} players linked "
          f"({n_new} by name only, {n_over} overrides, {n_added} profiles added from the raw files)")
    report(con)
    con.close()


def report(con=None):
    own = con is None
    con = con or sqlite3.connect(DB_FILE)
    print("\n  matches linked (Understat played matches that also exist in Transfermarkt):")
    for league, season, n, linked in con.execute("""
        SELECT m.league, m.season, COUNT(*), COUNT(lm.us_match_id)
        FROM us_matches m LEFT JOIN link_matches lm ON lm.us_match_id = m.match_id
        WHERE m.is_result = 1 AND m.season <= (SELECT MAX(season) FROM fixtures)
        GROUP BY 1, 2 ORDER BY 1, 2"""):
        print(f"    {league:<16}{season}/{str(season + 1)[2:]}  {linked}/{n}")
    n_teams, linked_teams = con.execute("""
        SELECT COUNT(DISTINCT id), COUNT(DISTINCT lt.us_team_id) FROM
        (SELECT home_id id FROM us_matches UNION SELECT away_id FROM us_matches) x
        LEFT JOIN link_teams lt ON lt.us_team_id = x.id""").fetchone()
    print(f"\n  clubs linked: {linked_teams}/{n_teams}")
    for t, in con.execute("""SELECT DISTINCT home FROM us_matches WHERE home_id NOT IN
                             (SELECT us_team_id FROM link_teams)"""):
        print(f"    not linked: {t}")
    print("\n  players linked, weighted by minutes played:")
    for label, cond in (("2021/22-2025/26", "ups.season <= (SELECT MAX(season) FROM fixtures)"),
                        ("current season", "ups.season = (SELECT MAX(season) FROM us_matches)")):
        mins, linked = con.execute(f"""
            SELECT SUM(ups.minutes), SUM(CASE WHEN lp.us_player_id IS NOT NULL THEN ups.minutes END)
            FROM us_player_season ups LEFT JOIN link_players lp ON lp.us_player_id = ups.player_id
            WHERE {cond}""").fetchone()
        if mins:
            print(f"    {label:<16} {100 * (linked or 0) / mins:.1f}% of minutes")
    print("    by method: " + ", ".join(f"{m} {n:,}" for m, n in con.execute(
        "SELECT method, COUNT(*) FROM link_players GROUP BY 1 ORDER BY 2 DESC")))
    top = con.execute("""
        SELECT ups.player_id, ups.player, ups.team, ups.minutes FROM us_player_season ups
        WHERE ups.season = (SELECT MAX(season) FROM us_matches)
          AND ups.player_id NOT IN (SELECT us_player_id FROM link_players)
        ORDER BY ups.minutes DESC LIMIT 10""").fetchall()
    if top:
        print("\n  biggest unlinked players this season (fix in config/link_overrides.csv: player,us_id,tm_id):")
        for pid, name, team, mins in top:
            print(f"    us_id {pid:<7} {name:<28} {team:<24} {mins} min")
    if own:
        con.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        report()
    else:
        build()
