#!/usr/bin/env python3
"""
scripts/7_sanity_checks.py - check the BigQuery data: index links, matches, players, and each source.

  python3 scripts/7_sanity_checks.py --project football-model-494509 --validate   # syntax only, free
  python3 scripts/7_sanity_checks.py --project football-model-494509              # run every check
  python3 scripts/7_sanity_checks.py --project football-model-494509 --only P1,M1

Every check is a query that returns the rows that look WRONG:
  error  should return 0 rows - anything else is a bug to fix
  warn   rows to review - some are real data quirks (awarded matches, nicknames...)
Results: a scoreboard on screen, and output/sanity_report/<check>.csv with the rows (up to 5,000)
plus output/sanity_report/summary.csv.

Run after scripts/6_to_bigquery.py. Cost: about 30 small queries, well under 1 GB per run.
Needs the Google Cloud SDK ("bq") and a login, like scripts/6_to_bigquery.py.
"""

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys

import paths
REPORT_DIR = paths.SANITY_REPORT_DIR


# ------------------------------------------------------------------ SQL helpers
def norm(x):
    """Lower case, accents removed, only letters/digits separated by single spaces."""
    return ("TRIM(REGEXP_REPLACE(LOWER(REGEXP_REPLACE(NORMALIZE(COALESCE(" + x + ", ''), NFD), "
            "r'\\p{M}', '')), r'[^a-z0-9]+', ' '))")


def no_common_word(a, b, min_len=2):
    """True when two names share no word of min_len+ letters."""
    return (f"NOT EXISTS (SELECT 1 FROM UNNEST(SPLIT({norm(a)}, ' ')) w1, "
            f"UNNEST(SPLIT({norm(b)}, ' ')) w2 WHERE w1 = w2 AND LENGTH(w1) >= {min_len})")


LEAGUE = ("CASE {col} WHEN 'GB1' THEN 'Premier League' WHEN 'ES1' THEN 'La Liga' "
          "WHEN 'IT1' THEN 'Serie A' WHEN 'L1' THEN 'Bundesliga' WHEN 'FR1' THEN 'Ligue 1' END")

PLAYER_PAIRS = """pairs AS (
  SELECT t.player_key, t.source_id AS tm_id, u.source_id AS us_id
  FROM {ix}.players t
  JOIN {ix}.players u ON u.player_key = t.player_key AND u.source = 'understat'
  WHERE t.source = 'transfermarkt')"""

MATCH_PAIRS = """mpairs AS (
  SELECT t.match_key, t.source_id AS tm_id, u.source_id AS us_id
  FROM {ix}.matches t
  JOIN {ix}.matches u ON u.match_key = t.match_key AND u.source = 'understat'
  WHERE t.source = 'transfermarkt')"""

TEAM_MAP = """team_map AS (
  SELECT t.source_id AS tm_team, u.source_id AS us_team, ut.name AS us_name
  FROM {ix}.teams t
  JOIN {ix}.teams u ON u.team_key = t.team_key AND u.source = 'understat'
  JOIN {us}.teams ut ON ut.team_id = u.source_id
  WHERE t.source = 'transfermarkt')"""

# league-season totals per Transfermarkt player, with his clubs' Understat names
TM_PLAYER_SEASONS = """tm_ps AS (
  SELECT pm.player_id, pm.season, """ + LEAGUE.format(col="pm.league_id") + """ AS league,
         SUM(pm.minutes) AS minutes, SUM(pm.goals) AS goals,
         STRING_AGG(DISTINCT pm.team, ', ') AS tm_teams,
         ARRAY_AGG(DISTINCT tmap.us_name IGNORE NULLS) AS us_names
  FROM {tm}.player_matches pm
  LEFT JOIN team_map tmap ON tmap.tm_team = pm.team_id
  WHERE pm.played = 1
  GROUP BY 1, 2, 3)"""

KINDS = [("players", "player_key"), ("teams", "team_key"), ("matches", "match_key")]
SOURCE_TABLES = [   # (source, kind, table, id column) - every id here must be in the index
    ("transfermarkt", "players", "{tm}.players", "player_id"),
    ("understat", "players", "{us}.players", "player_id"),
    ("transfermarkt", "teams", "{tm}.teams", "team_id"),
    ("understat", "teams", "{us}.teams", "team_id"),
    ("transfermarkt", "matches", "{tm}.matches", "match_id"),
    ("understat", "matches", "{us}.matches", "match_id"),
    ("understat", "matches", "{us}.fixtures", "match_id"),
]


def union(parts):
    return "\nUNION ALL\n".join(parts)


# ------------------------------------------------------------------ the checks
CHECKS = []   # (id, severity, area, description, sql)


def check(cid, severity, area, description, sql):
    CHECKS.append((cid, severity, area, description, sql))


# ---- index structure
check("I1", "error", "index", "A source id sits under more than one key", union([
    f"SELECT '{k}' AS kind, source, source_id, COUNT(DISTINCT {key}) AS n_keys "
    f"FROM {{ix}}.{k} GROUP BY source, source_id HAVING COUNT(DISTINCT {key}) > 1"
    for k, key in KINDS]))

check("I2", "error", "index", "Two Transfermarkt ids share one key (two people/clubs/matches merged)", union([
    f"SELECT '{k}' AS kind, {key} AS key_id, STRING_AGG(CAST(source_id AS STRING), ', ') AS ids, "
    f"STRING_AGG(name, ' | ') AS names FROM {{ix}}.{k} WHERE source = 'transfermarkt' "
    f"GROUP BY {key} HAVING COUNT(*) > 1" for k, key in KINDS]))

check("I3", "warn", "index", "Two Understat ids share one key (Understat lists it twice, or a bad link)", union([
    f"SELECT '{k}' AS kind, {key} AS key_id, STRING_AGG(CAST(source_id AS STRING), ', ') AS ids, "
    f"STRING_AGG(name, ' | ') AS names FROM {{ix}}.{k} WHERE source = 'understat' "
    f"GROUP BY {key} HAVING COUNT(*) > 1" for k, key in KINDS]))

check("I4", "error", "index", "Source ids missing from the index", union([
    f"SELECT '{src}' AS source, '{kind}' AS kind, {col} AS source_id FROM {table} "
    f"WHERE {col} NOT IN (SELECT source_id FROM {{ix}}.{kind} WHERE source = '{src}')"
    for src, kind, table, col in SOURCE_TABLES]))

check("I5", "warn", "index", "Index rows whose id is no longer in the source tables", union([
    f"SELECT '{src}' AS source, '{kind}' AS kind, i.source_id, i.name FROM {{ix}}.{kind} i "
    f"WHERE i.source = '{src}' AND i.source_id NOT IN ("
    + " UNION ALL ".join(f"SELECT {c} FROM {t}" for s2, k2, t, c in SOURCE_TABLES
                         if s2 == src and k2 == kind) + ")"
    for src, kind in sorted({(s, k) for s, k, _, _ in SOURCE_TABLES})]))

# ---- player links
check("P1", "warn", "players", "Linked players whose two names share no word", f"""
WITH {PLAYER_PAIRS}
SELECT p.player_key, p.tm_id, tp.name AS tm_name, p.us_id, up.name AS us_name,
       up.first_season, up.last_season
FROM pairs p
JOIN {{tm}}.players tp ON tp.player_id = p.tm_id
JOIN {{us}}.players up ON up.player_id = p.us_id
WHERE {no_common_word('tp.name', 'up.name')}
ORDER BY up.last_season DESC""")

check("P2", "error", "players", "Linked players with an impossible age (under 15 or over 43 in an Understat season)", f"""
WITH {PLAYER_PAIRS},
ages AS (
  SELECT p.player_key, tp.name AS tm_name, up.name AS us_name, tp.birth_date,
         up.first_season, up.last_season,
         ROUND(DATE_DIFF(DATE(up.first_season, 8, 1), tp.birth_date, DAY) / 365.25, 1) AS age_first_season,
         ROUND(DATE_DIFF(DATE(up.last_season, 8, 1), tp.birth_date, DAY) / 365.25, 1) AS age_last_season
  FROM pairs p
  JOIN {{tm}}.players tp ON tp.player_id = p.tm_id
  JOIN {{us}}.players up ON up.player_id = p.us_id
  WHERE tp.birth_date IS NOT NULL)
SELECT * FROM ages WHERE age_first_season < 15 OR age_last_season > 43""")

check("P3", "warn", "players", "Linked player-seasons where the two sources name different clubs", f"""
WITH {PLAYER_PAIRS}, {TEAM_MAP}, {TM_PLAYER_SEASONS}
SELECT p.player_key, tp.name, s.league, s.season_label, t.tm_teams, s.teams AS us_teams,
       t.minutes AS tm_minutes, s.minutes AS us_minutes
FROM pairs p
JOIN tm_ps t ON t.player_id = p.tm_id
JOIN {{us}}.player_seasons s ON s.player_id = p.us_id AND s.season = t.season AND s.league = t.league
JOIN {{tm}}.players tp ON tp.player_id = p.tm_id
WHERE NOT EXISTS (SELECT 1 FROM UNNEST(t.us_names) n WHERE STRPOS(s.teams, n) > 0)
ORDER BY t.minutes DESC""")

check("P4", "warn", "players", "Linked player-seasons (900+ min) where minutes differ by 20%+ or goals by 3+", f"""
WITH {PLAYER_PAIRS}, {TEAM_MAP}, {TM_PLAYER_SEASONS}
SELECT p.player_key, tp.name, s.league, s.season_label, t.tm_teams, s.teams AS us_teams,
       t.minutes AS tm_minutes, s.minutes AS us_minutes, t.goals AS tm_goals, s.goals AS us_goals
FROM pairs p
JOIN tm_ps t ON t.player_id = p.tm_id
JOIN {{us}}.player_seasons s ON s.player_id = p.us_id AND s.season = t.season AND s.league = t.league
JOIN {{tm}}.players tp ON tp.player_id = p.tm_id
WHERE GREATEST(t.minutes, s.minutes) >= 900
  AND (ABS(t.minutes - s.minutes) > 0.2 * GREATEST(t.minutes, s.minutes)
       OR ABS(t.goals - s.goals) >= 3)
ORDER BY ABS(t.goals - s.goals) DESC, ABS(t.minutes - s.minutes) DESC""")

check("P5", "warn", "players", "Linked players with 900+ min in a season one source has and the other lacks", f"""
WITH {PLAYER_PAIRS}, {TEAM_MAP}, {TM_PLAYER_SEASONS},
max_tm AS (SELECT MAX(season) AS max_season FROM {{tm}}.matches)
SELECT p.player_key, tp.name, t.league, t.season, t.tm_teams, t.minutes AS tm_minutes,
       'missing in Understat' AS issue
FROM pairs p
JOIN tm_ps t ON t.player_id = p.tm_id
JOIN {{tm}}.players tp ON tp.player_id = p.tm_id
WHERE t.minutes >= 900 AND NOT EXISTS (
  SELECT 1 FROM {{us}}.player_seasons s
  WHERE s.player_id = p.us_id AND s.season = t.season AND s.league = t.league)
UNION ALL
SELECT p.player_key, up.name, s.league, s.season, s.teams, s.minutes, 'missing in Transfermarkt'
FROM pairs p
JOIN {{us}}.player_seasons s ON s.player_id = p.us_id
JOIN {{us}}.players up ON up.player_id = p.us_id
WHERE s.minutes >= 900 AND s.season <= (SELECT max_season FROM max_tm) AND NOT EXISTS (
  SELECT 1 FROM tm_ps t WHERE t.player_id = p.tm_id AND t.season = s.season AND t.league = s.league)""")

check("P6", "warn", "players", "Possible missed links: an Understat-only player with the same name as an unlinked Transfermarkt player", f"""
WITH key_sources AS (
  SELECT player_key, LOGICAL_AND(source = 'understat') AS us_only,
         LOGICAL_AND(source = 'transfermarkt') AS tm_only
  FROM {{ix}}.players GROUP BY player_key),
us_only AS (
  SELECT i.player_key, i.source_id AS us_id FROM {{ix}}.players i
  JOIN key_sources k USING (player_key) WHERE k.us_only),
tm_only AS (
  SELECT i.player_key, i.source_id AS tm_id FROM {{ix}}.players i
  JOIN key_sources k USING (player_key) WHERE k.tm_only),
tm_last AS (
  SELECT player_id, MAX(season) AS last_season,
         ARRAY_AGG(team IGNORE NULLS ORDER BY date DESC LIMIT 1)[SAFE_OFFSET(0)] AS last_team
  FROM {{tm}}.player_matches WHERE played = 1 GROUP BY player_id)
SELECT u.player_key AS us_key, up.name AS us_name, up.last_season AS us_last_season,
       o.player_key AS tm_key, tp.name AS tm_name, tp.birth_date, l.last_team AS tm_last_team,
       l.last_season AS tm_last_season
FROM us_only u
JOIN {{us}}.players up ON up.player_id = u.us_id
JOIN {{tm}}.players tp ON {norm('tp.name')} = {norm('up.name')}
JOIN tm_only o ON o.tm_id = tp.player_id
LEFT JOIN tm_last l ON l.player_id = tp.player_id
ORDER BY up.last_season DESC, up.name""")

check("P7", "warn", "players", "Transfermarkt players with the same name and birth date under different ids", f"""
SELECT {norm('name')} AS name_normalised, birth_date,
       STRING_AGG(CAST(player_id AS STRING), ', ') AS ids, STRING_AGG(name, ' | ') AS names
FROM {{tm}}.players
WHERE birth_date IS NOT NULL
GROUP BY 1, 2 HAVING COUNT(*) > 1""")

# ---- team links
check("T1", "warn", "teams", "Linked clubs whose two names share no word", f"""
WITH {TEAM_MAP}
SELECT m.tm_team, tt.name AS tm_name, tt.full_name AS tm_full_name, m.us_team, m.us_name
FROM team_map m JOIN {{tm}}.teams tt ON tt.team_id = m.tm_team
WHERE {no_common_word('tt.name', 'm.us_name', 3)} AND {no_common_word('tt.full_name', 'm.us_name', 3)}""")

# ---- match links
MATCH_JOIN = """FROM mpairs mp
JOIN {tm}.matches tm ON tm.match_id = mp.tm_id
JOIN {us}.matches um ON um.match_id = mp.us_id
LEFT JOIN {ix}.teams th ON th.source = 'transfermarkt' AND th.source_id = tm.home_team_id
LEFT JOIN {ix}.teams ta ON ta.source = 'transfermarkt' AND ta.source_id = tm.away_team_id
LEFT JOIN {ix}.teams uh ON uh.source = 'understat' AND uh.source_id = um.home_team_id
LEFT JOIN {ix}.teams ua ON ua.source = 'understat' AND ua.source_id = um.away_team_id"""
MATCH_COLS = """mp.match_key, tm.date AS tm_date, tm.home_team AS tm_home, tm.home_goals AS tm_hg,
       tm.away_goals AS tm_ag, tm.away_team AS tm_away, um.date AS us_date, um.home_team AS us_home,
       um.home_goals AS us_hg, um.away_goals AS us_ag, um.away_team AS us_away"""

check("M1", "error", "matches", "Linked matches whose clubs differ between sources",
      f"WITH {MATCH_PAIRS}\nSELECT {MATCH_COLS}\n{MATCH_JOIN}\n"
      "WHERE th.team_key IS DISTINCT FROM uh.team_key OR ta.team_key IS DISTINCT FROM ua.team_key")

check("M2", "warn", "matches", "Linked matches more than 1 day apart (expected: abandoned or rescheduled matches)",
      f"WITH {MATCH_PAIRS}\nSELECT {MATCH_COLS}\n{MATCH_JOIN}\n"
      "WHERE ABS(DATE_DIFF(tm.date, um.date, DAY)) > 1")

check("M3", "warn", "matches", "Linked matches with a different score (expected: awarded matches)",
      f"WITH {MATCH_PAIRS}\nSELECT {MATCH_COLS}\n{MATCH_JOIN}\n"
      "WHERE tm.home_goals IS DISTINCT FROM um.home_goals OR tm.away_goals IS DISTINCT FROM um.away_goals")

check("M4", "error", "matches", "Played matches with no partner in the other source (seasons both cover)", """
WITH linked AS (SELECT match_key FROM {ix}.matches GROUP BY match_key HAVING COUNT(DISTINCT source) = 2),
max_tm AS (SELECT MAX(season) AS max_season FROM {tm}.matches)
SELECT 'transfermarkt' AS source, m.match_id, m.league, m.date, m.home_team, m.away_team,
       m.home_goals, m.away_goals
FROM {tm}.matches m
JOIN {ix}.matches i ON i.source = 'transfermarkt' AND i.source_id = m.match_id
WHERE m.status = 'played' AND i.match_key NOT IN (SELECT match_key FROM linked)
UNION ALL
SELECT 'understat', m.match_id, m.league, m.date, m.home_team, m.away_team, m.home_goals, m.away_goals
FROM {us}.matches m
JOIN {ix}.matches i ON i.source = 'understat' AND i.source_id = m.match_id
WHERE m.season <= (SELECT max_season FROM max_tm) AND i.match_key NOT IN (SELECT match_key FROM linked)""")

# ---- Transfermarkt data
check("D1", "error", "transfermarkt", "Duplicate rows", """
SELECT 'matches' AS tbl, FORMAT('%d', match_id) AS id, COUNT(*) AS n FROM {tm}.matches
GROUP BY match_id HAVING COUNT(*) > 1
UNION ALL
SELECT 'player_matches', FORMAT('%d-%d', match_id, player_id), COUNT(*) FROM {tm}.player_matches
GROUP BY match_id, player_id HAVING COUNT(*) > 1
UNION ALL
SELECT 'players', FORMAT('%d', player_id), COUNT(*) FROM {tm}.players
GROUP BY player_id HAVING COUNT(*) > 1""")

check("D6", "warn", "transfermarkt", "Played (in the lineup) but no match stats: minutes, goals, cards missing in the source", """
SELECT season_label, league_id, COUNT(*) AS player_matches, COUNT(DISTINCT player_id) AS players,
       COUNT(DISTINCT match_id) AS matches
FROM {tm}.player_matches
WHERE played = 1 AND minutes = 0
GROUP BY 1, 2 ORDER BY 1, 2""")

check("D2", "warn", "transfermarkt", "Match score differs from players' goals + own goals", """
WITH pg AS (SELECT match_id, SUM(goals) AS goals FROM {tm}.player_matches GROUP BY match_id),
og AS (SELECT match_id, COUNT(*) AS own_goals FROM {tm}.events
       WHERE type = 'Goal' AND detail = 'Own Goal' GROUP BY match_id)
SELECT m.match_id, m.league, m.date, m.home_team, m.home_goals, m.away_goals, m.away_team,
       COALESCE(pg.goals, 0) AS player_goals, COALESCE(og.own_goals, 0) AS own_goals
FROM {tm}.matches m
LEFT JOIN pg USING (match_id)
LEFT JOIN og USING (match_id)
WHERE m.status = 'played'
  AND m.home_goals + m.away_goals != COALESCE(pg.goals, 0) + COALESCE(og.own_goals, 0)""")

check("D3", "warn", "transfermarkt", "Teams with lineup data but not exactly 11 starters", """
SELECT match_id, date, team, COUNTIF(lineup_role = 'starter') AS starters
FROM {tm}.player_matches
GROUP BY match_id, date, team
HAVING COUNTIF(lineup_role IS NOT NULL) > 0 AND COUNTIF(lineup_role = 'starter') != 11""")

check("D4", "error", "transfermarkt", "Impossible minutes (negative or over 130)", """
SELECT match_id, date, player, team, minutes FROM {tm}.player_matches
WHERE minutes < 0 OR minutes > 130""")

check("D5", "error", "transfermarkt", "Player listed for a club that isn't in the match", """
SELECT pm.match_id, pm.date, pm.player, pm.team, m.home_team, m.away_team
FROM {tm}.player_matches pm JOIN {tm}.matches m USING (match_id)
WHERE pm.team_id IS NULL OR pm.team_id NOT IN (m.home_team_id, m.away_team_id)""")

# ---- Understat data
check("U1", "error", "understat", "Duplicate rows", """
SELECT 'matches' AS tbl, FORMAT('%d', match_id) AS id, COUNT(*) AS n FROM {us}.matches
GROUP BY match_id HAVING COUNT(*) > 1
UNION ALL
SELECT 'player_matches', FORMAT('%d-%d', match_id, player_id), COUNT(*) FROM {us}.player_matches
GROUP BY match_id, player_id HAVING COUNT(*) > 1
UNION ALL
SELECT 'team_matches', FORMAT('%d-%d', match_id, team_id), COUNT(*) FROM {us}.team_matches
GROUP BY match_id, team_id HAVING COUNT(*) > 1
UNION ALL
SELECT 'shots', FORMAT('%d', shot_id), COUNT(*) FROM {us}.shots
GROUP BY shot_id HAVING COUNT(*) > 1""")

check("U2", "error", "understat", "A match in both matches and fixtures", """
SELECT f.match_id, f.date, f.home_team, f.away_team, f.status
FROM {us}.fixtures f JOIN {us}.matches m USING (match_id)""")

check("U3", "error", "understat", "Played matches without exactly 2 team_matches rows", """
SELECT m.match_id, m.league, m.date, m.home_team, m.away_team, COUNT(t.team_id) AS team_rows
FROM {us}.matches m LEFT JOIN {us}.team_matches t USING (match_id)
GROUP BY 1, 2, 3, 4, 5 HAVING COUNT(t.team_id) != 2""")

check("U4", "error", "understat", "Player listed for a club that isn't in the match", """
SELECT pm.match_id, pm.date, pm.player, pm.team, m.home_team, m.away_team
FROM {us}.player_matches pm JOIN {us}.matches m USING (match_id)
WHERE pm.team_id IS NULL OR pm.team_id NOT IN (m.home_team_id, m.away_team_id)""")

check("U5", "warn", "understat", "Match score differs from players' goals + own goals", """
WITH pg AS (SELECT match_id, SUM(goals) AS goals, SUM(own_goals) AS own_goals
            FROM {us}.player_matches GROUP BY match_id)
SELECT m.match_id, m.league, m.date, m.home_team, m.home_goals, m.away_goals, m.away_team,
       pg.goals AS player_goals, pg.own_goals
FROM {us}.matches m JOIN pg USING (match_id)
WHERE m.home_goals + m.away_goals != pg.goals + pg.own_goals""")

check("U6", "warn", "understat", "Matches where 5+ players appear for a club that isn't theirs that season (swapped rosters)", """
SELECT pm.match_id, m.date, m.home_team, m.away_team, COUNT(*) AS players,
       COUNTIF(STRPOS(ps.teams, pm.team) = 0) AS players_not_at_that_club
FROM {us}.player_matches pm
JOIN {us}.matches m USING (match_id)
JOIN {us}.player_seasons ps
  ON ps.player_id = pm.player_id AND ps.season = pm.season AND ps.league = pm.league
GROUP BY 1, 2, 3, 4
HAVING COUNTIF(STRPOS(ps.teams, pm.team) = 0) >= 5""")

check("U7", "warn", "understat", "Players whose goals differ from their goals in the shot data", """
WITH sg AS (SELECT match_id, player_id, COUNTIF(result = 'Goal') AS shot_goals
            FROM {us}.shots GROUP BY match_id, player_id)
SELECT pm.match_id, pm.date, pm.player, pm.team, pm.goals, COALESCE(sg.shot_goals, 0) AS shot_goals
FROM {us}.player_matches pm
LEFT JOIN sg USING (match_id, player_id)
WHERE pm.match_id IN (SELECT DISTINCT match_id FROM {us}.shots)
  AND pm.goals != COALESCE(sg.shot_goals, 0)""")

check("U8", "warn", "understat", "Team xG differs from the sum of its players' xG by more than 0.1", """
WITH px AS (SELECT match_id, home_away, SUM(xg) AS xg FROM {us}.player_matches
            GROUP BY match_id, home_away)
SELECT m.match_id, m.date, m.home_team, m.away_team, m.home_xg, h.xg AS home_players_xg,
       m.away_xg, a.xg AS away_players_xg
FROM {us}.matches m
JOIN px h ON h.match_id = m.match_id AND h.home_away = 'home'
JOIN px a ON a.match_id = m.match_id AND a.home_away = 'away'
WHERE ABS(m.home_xg - h.xg) > 0.1 OR ABS(m.away_xg - a.xg) > 0.1""")


# ------------------------------------------------------------------ runner
def run_bq(args, sql, dry):
    cmd = [args.bq, "--location", args.location, "--project_id", args.project, "--format=json",
           "query", "--use_legacy_sql=false", f"--max_rows={args.max_rows}"]
    if dry:
        cmd.append("--dry_run")
    res = subprocess.run(cmd + [sql], capture_output=True, text=True)
    if res.returncode != 0:
        return None, (res.stderr or res.stdout).strip()
    if dry:
        return [], None
    out = res.stdout
    start = out.find("[")
    return (json.loads(out[start:]) if start >= 0 else []), None


def main():
    ap = argparse.ArgumentParser(description="Sanity checks on the BigQuery football datasets.")
    ap.add_argument("--project", required=True, help="Google Cloud project id")
    ap.add_argument("--location", default="EU")
    ap.add_argument("--only", help="comma-separated check ids, e.g. P1,M1")
    ap.add_argument("--validate", action="store_true", help="check the SQL only (dry run, free)")
    ap.add_argument("--max-rows", type=int, default=5000, help="rows saved per check (default 5000)")
    ap.add_argument("--bq", default="bq", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if not shutil.which(args.bq):
        sys.exit("The 'bq' command was not found. Install the Google Cloud SDK, then: gcloud auth login")

    ds = {"tm": f"`{args.project}.football_tm`", "us": f"`{args.project}.football_us`",
          "ix": f"`{args.project}.football_index`"}
    wanted = {c.strip().upper() for c in args.only.split(",")} if args.only else None
    checks = [c for c in CHECKS if not wanted or c[0] in wanted]
    if not checks:
        sys.exit("No check matches --only. Ids: " + ", ".join(c[0] for c in CHECKS))
    if not args.validate:
        os.makedirs(REPORT_DIR, exist_ok=True)

    summary, n_err, n_warn, n_fail = [], 0, 0, 0
    area = None
    print(f"{'id':<5}{'level':<7}{'rows':>7}  check")
    for cid, severity, ar, desc, sql in checks:
        if ar != area:
            area = ar
            print(f"--- {area}")
        sql = sql.replace("{tm}", ds["tm"]).replace("{us}", ds["us"]).replace("{ix}", ds["ix"])
        rows, err = run_bq(args, sql, args.validate)
        if err:
            n_fail += 1
            print(f"{cid:<5}{severity:<7}{'ERROR':>7}  {desc}\n       query failed: "
                  + err.replace("\n", " ")[:300])
            summary.append({"id": cid, "level": severity, "area": ar, "check": desc,
                            "rows": "query failed", "status": "query failed"})
            continue
        if args.validate:
            print(f"{cid:<5}{severity:<7}{'ok':>7}  {desc}")
            continue
        n = len(rows)
        more = "+" if n >= args.max_rows else ""
        status = "pass" if n == 0 else severity
        n_err += status == "error"
        n_warn += status == "warn"
        mark = "ok" if n == 0 else ("FAIL" if severity == "error" else "check")
        print(f"{cid:<5}{severity:<7}{str(n) + more:>7}  {desc}  [{mark}]")
        path = os.path.join(REPORT_DIR, f"{cid}.csv")
        if rows:
            cols = list(rows[0].keys())
            with open(path, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=cols)
                w.writeheader()
                w.writerows(rows)
            for r in rows[:3]:
                print("         " + " | ".join(f"{k}={v}" for k, v in list(r.items())[:7])[:160])
        elif os.path.exists(path):
            os.remove(path)
        summary.append({"id": cid, "level": severity, "area": ar, "check": desc,
                        "rows": f"{n}{more}", "status": status})

    if args.validate:
        print(f"\n{len(checks) - n_fail}/{len(checks)} queries valid."
              + (" Paste the failures to Claude." if n_fail else " Run without --validate next."))
        return
    with open(os.path.join(REPORT_DIR, "summary.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["id", "level", "area", "check", "rows", "status"])
        w.writeheader()
        w.writerows(summary)
    print(f"\n{n_err} errors, {n_warn} checks with rows to review, {n_fail} queries failed. "
          f"Details: {REPORT_DIR}/")


if __name__ == "__main__":
    main()
