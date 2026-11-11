#!/usr/bin/env python3
"""
scripts/4_build_sources.py - build one clean database per source, ready for BigQuery.

  python3 scripts/4_build_sources.py

Creates
  football_tm.db   Transfermarkt:  matches, player_matches, events, players, teams,
                                   transfers, market_values
  football_us.db   Understat:      matches (played), fixtures (upcoming / postponed),
                                   team_matches, player_matches, shots, player_seasons,
                                   players, teams

Each source keeps its own ids (Transfermarkt ids in football_tm, Understat ids in
football_us). Nothing is merged across sources here: the link between the two lives in
the index tables (football_index, next step).

Cleaning done here (inside one source only):
  - same column names in both sources (match_id, player_id, team_id, season, date...)
  - dates as YYYY-MM-DD, Understat kick-off times as YYYY-MM-DD HH:MM:SS (UTC)
  - team and player names added next to ids, so most queries need no extra join
  - Transfermarkt: lineups and match stats in one player_matches table
  - Understat: team_matches gets its match_id, shots get the shooter's team from the
    match roster, player_matches gets penalty goals / npxG / shots on target from shots

Run after: scripts/1_transfermarkt.py build, scripts/2_understat.py build.
No third-party packages needed (Python 3.8+).
"""

import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

import paths
RAW_DB = paths.WORK_DB
TM_DB = paths.TM_DB
US_DB = paths.US_DB


# ------------------------------------------------------------------ helpers
def season_label(s):
    return f"{s}/{str(s + 1)[2:]}" if s is not None else None


def iso_date(s):
    if s is None or s == "":
        return None
    s = str(s).replace("T", " ")
    for fmt, n in (("%Y-%m-%d", 10), ("%d/%m/%Y", 10)):
        try:
            return datetime.strptime(s[:n], fmt).date().isoformat()
        except ValueError:
            pass
    return None


def iso_dt(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s).replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S").strftime(
            "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def hda(h, a):
    if h is None or a is None:
        return None
    return "H" if h > a else "D" if h == a else "A"


def wdl(f, a):
    if f is None or a is None:
        return None
    return "W" if f > a else "D" if f == a else "L"


def new_db(path):
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    for name, n, fn in (("season_label", 1, season_label), ("iso_date", 1, iso_date),
                        ("iso_dt", 1, iso_dt), ("hda", 2, hda), ("wdl", 2, wdl)):
        con.create_function(name, n, fn)
    con.execute("ATTACH DATABASE ? AS r", (RAW_DB,))
    return con


def has_table(con, name):
    return con.execute("SELECT 1 FROM r.sqlite_master WHERE type='table' AND name=?",
                       (name,)).fetchone() is not None


def count(con, table):
    return con.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0]


# ------------------------------------------------------------------ Transfermarkt
def build_tm():
    con = new_db(TM_DB)
    missing = [t for t in ("fixtures", "fixture_events", "fixture_player_stats", "fixture_lineups",
                           "players", "teams", "leagues", "transfers", "market_values")
               if not has_table(con, t)]
    if missing:
        sys.exit("football.db is missing Transfermarkt tables (" + ", ".join(missing)
                 + "). Run: python3 scripts/1_transfermarkt.py build")
    print("Building football_tm.db (Transfermarkt)...")
    con.executescript("""
    -- name lookups: profile name, else the name printed in lineups / events
    CREATE TEMP TABLE pname AS
    SELECT player_id, name FROM r.players WHERE name IS NOT NULL AND name <> '';
    INSERT INTO pname SELECT player_id, MAX(player_name) FROM r.fixture_lineups
      WHERE player_name IS NOT NULL AND player_id NOT IN (SELECT player_id FROM pname)
      GROUP BY player_id;
    INSERT INTO pname SELECT player_id, MAX(player_name) FROM r.fixture_events
      WHERE player_id IS NOT NULL AND player_name IS NOT NULL
        AND player_id NOT IN (SELECT player_id FROM pname)
      GROUP BY player_id;
    CREATE UNIQUE INDEX temp.ix_pname ON pname(player_id);

    CREATE TABLE teams AS
    SELECT team_id, name, full_name FROM r.teams ORDER BY team_id;

    CREATE TABLE matches AS
    SELECT f.fixture_id AS match_id,
           f.league_id, l.name AS league, l.country,
           f.season, season_label(f.season) AS season_label, f.round,
           iso_date(f.date_utc) AS date,
           CASE WHEN f.status = 'FT' THEN 'played' ELSE lower(f.status) END AS status,
           f.home_team_id, th.name AS home_team, f.away_team_id, ta.name AS away_team,
           f.home_goals, f.away_goals, hda(f.home_goals, f.away_goals) AS result,
           f.home_position AS home_league_position, f.away_position AS away_league_position,
           f.home_manager, f.away_manager, f.referee, f.venue, f.attendance
    FROM r.fixtures f
    LEFT JOIN r.leagues l ON l.league_id = f.league_id
    LEFT JOIN r.teams th ON th.team_id = f.home_team_id
    LEFT JOIN r.teams ta ON ta.team_id = f.away_team_id
    ORDER BY f.date_utc, f.fixture_id;

    -- one row per player per match: squad (lineups) + match stats
    CREATE TEMP TABLE pm_keys AS
    SELECT fixture_id, player_id FROM r.fixture_lineups
    UNION SELECT fixture_id, player_id FROM r.fixture_player_stats;

    CREATE TABLE player_matches AS
    SELECT k.fixture_id AS match_id, f.league_id, f.season, season_label(f.season) AS season_label,
           iso_date(f.date_utc) AS date,
           k.player_id, n.name AS player,
           COALESCE(s.team_id, l.team_id) AS team_id, t.name AS team,
           CASE WHEN COALESCE(s.team_id, l.team_id) = f.home_team_id THEN 'home'
                WHEN COALESCE(s.team_id, l.team_id) = f.away_team_id THEN 'away' END AS home_away,
           l.role AS lineup_role,
           COALESCE(s.started, CASE l.role WHEN 'starter' THEN 1 WHEN 'bench' THEN 0 END) AS started,
           CASE WHEN s.player_id IS NOT NULL THEN 1 ELSE COALESCE(l.played, 0) END AS played,
           COALESCE(s.minutes, 0) AS minutes,
           COALESCE(s.goals, 0) AS goals, COALESCE(s.assists, 0) AS assists,
           COALESCE(s.penalties_scored, 0) AS penalty_goals,
           COALESCE(s.yellow, 0) AS yellow, COALESCE(s.red, 0) AS red,
           COALESCE(s.captain, l.captain) AS captain,
           COALESCE(s.shirt_number, l.shirt_number) AS shirt_number,
           COALESCE(l.position, s.position) AS match_position
    FROM pm_keys k
    JOIN r.fixtures f ON f.fixture_id = k.fixture_id
    LEFT JOIN r.fixture_player_stats s ON s.fixture_id = k.fixture_id AND s.player_id = k.player_id
    LEFT JOIN r.fixture_lineups l ON l.fixture_id = k.fixture_id AND l.player_id = k.player_id
    LEFT JOIN pname n ON n.player_id = k.player_id
    LEFT JOIN r.teams t ON t.team_id = COALESCE(s.team_id, l.team_id)
    ORDER BY f.date_utc, k.fixture_id, COALESCE(s.team_id, l.team_id), k.player_id;

    CREATE TABLE events AS
    SELECT e.fixture_id AS match_id, f.league_id, f.season, iso_date(f.date_utc) AS date,
           e.minute, e.team_id, t.name AS team,
           CASE e.type WHEN 'subst' THEN 'Substitution' ELSE e.type END AS type, e.detail,
           e.player_id, COALESCE(n1.name, e.player_name) AS player,
           e.assist_id AS assist_player_id, COALESCE(n2.name, e.assist_name) AS assist_player,
           e.player_in_id, n3.name AS player_in,
           e.description
    FROM r.fixture_events e
    JOIN r.fixtures f ON f.fixture_id = e.fixture_id
    LEFT JOIN r.teams t ON t.team_id = e.team_id
    LEFT JOIN pname n1 ON n1.player_id = e.player_id
    LEFT JOIN pname n2 ON n2.player_id = e.assist_id
    LEFT JOIN pname n3 ON n3.player_id = e.player_in_id
    ORDER BY f.date_utc, e.fixture_id, e.minute;

    CREATE TABLE players AS
    SELECT n.player_id, n.name, p.first_name, p.last_name, iso_date(p.birth_date) AS birth_date,
           p.nationality, p.position, p.sub_position, p.foot, p.height_cm, p.photo AS photo_url,
           CASE WHEN p.player_id IS NULL OR p.name IS NULL OR p.name = '' THEN 0 ELSE 1 END
             AS has_profile
    FROM pname n LEFT JOIN r.players p ON p.player_id = n.player_id
    ORDER BY n.player_id;

    CREATE TABLE transfers AS
    SELECT t.player_id, n.name AS player, iso_date(t.transfer_date) AS date, t.transfer_season,
           t.from_team_id, t.from_team, t.to_team_id, t.to_team, t.fee_eur, t.market_value_eur
    FROM r.transfers t LEFT JOIN pname n ON n.player_id = t.player_id
    ORDER BY t.transfer_date, t.player_id;

    CREATE TABLE market_values AS
    SELECT v.player_id, n.name AS player, iso_date(v.date) AS date, v.value_eur,
           v.team_id, v.team_name AS team
    FROM r.market_values v LEFT JOIN pname n ON n.player_id = v.player_id
    ORDER BY v.player_id, v.date;
    """)
    # players referenced by transfers / values but never in a top-5 squad: add their profile
    con.execute("""
    INSERT INTO players
    SELECT p.player_id, p.name, p.first_name, p.last_name, iso_date(p.birth_date), p.nationality,
           p.position, p.sub_position, p.foot, p.height_cm, p.photo, 1
    FROM r.players p
    WHERE p.player_id NOT IN (SELECT player_id FROM players) AND p.name IS NOT NULL AND p.name <> ''
    """)
    con.commit()
    tables = ["matches", "player_matches", "events", "players", "teams", "transfers", "market_values"]
    for t in tables:
        print(f"  {t:<16}{count(con, t):>10,}")
    dup = con.execute("SELECT COUNT(*) FROM (SELECT match_id, player_id FROM player_matches "
                      "GROUP BY 1, 2 HAVING COUNT(*) > 1)").fetchone()[0]
    noname = con.execute("SELECT COUNT(DISTINCT player_id) FROM player_matches "
                         "WHERE player IS NULL").fetchone()[0]
    print(f"  checks: {dup} duplicate player-match rows, {noname} players without a name")
    con.close()


# ------------------------------------------------------------------ Understat
def build_us():
    con = new_db(US_DB)
    missing = [t for t in ("us_matches", "us_team_matches", "us_player_match", "us_shots",
                           "us_player_season") if not has_table(con, t)]
    if missing:
        sys.exit("football.db is missing Understat tables (" + ", ".join(missing)
                 + "). Run: python3 scripts/2_understat.py build")
    print("Building football_us.db (Understat)...")

    # team names: Understat spells a club the same way every season; take the most common
    titles = defaultdict(Counter)
    for tid, title in con.execute("SELECT home_id, home FROM r.us_matches UNION ALL "
                                  "SELECT away_id, away FROM r.us_matches"):
        if tid is not None and title:
            titles[tid][title] += 1
    con.execute("CREATE TABLE teams (team_id INTEGER PRIMARY KEY, name TEXT, leagues TEXT, "
                "first_season INTEGER, last_season INTEGER)")
    span = {tid: (lg, a, b) for tid, lg, a, b in con.execute(
        "SELECT t, GROUP_CONCAT(DISTINCT league), MIN(season), MAX(season) FROM ("
        " SELECT home_id t, league, season FROM r.us_matches UNION ALL"
        " SELECT away_id, league, season FROM r.us_matches) GROUP BY t")}
    con.executemany("INSERT INTO teams VALUES (?,?,?,?,?)", [
        (tid, c.most_common(1)[0][0]) + span.get(tid, (None, None, None))
        for tid, c in sorted(titles.items())])

    now_utc = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    con.executescript(f"""
    -- played matches only (frozen history); upcoming / postponed ones go to fixtures
    CREATE TABLE matches AS
    SELECT m.match_id, m.league, m.season, season_label(m.season) AS season_label,
           substr(iso_dt(m.datetime), 1, 10) AS date, iso_dt(m.datetime) AS kickoff_utc,
           m.home_id AS home_team_id, th.name AS home_team,
           m.away_id AS away_team_id, ta.name AS away_team,
           m.home_goals, m.away_goals, hda(m.home_goals, m.away_goals) AS result,
           m.home_xg, m.away_xg,
           m.forecast_home_win, m.forecast_draw, m.forecast_away_win
    FROM r.us_matches m
    LEFT JOIN teams th ON th.team_id = m.home_id
    LEFT JOIN teams ta ON ta.team_id = m.away_id
    WHERE m.is_result = 1
    ORDER BY kickoff_utc, m.match_id;

    -- matches without a result: upcoming, or postponed (kick-off passed 3+ hours ago)
    CREATE TABLE fixtures AS
    SELECT m.match_id, m.league, m.season, season_label(m.season) AS season_label,
           substr(iso_dt(m.datetime), 1, 10) AS date, iso_dt(m.datetime) AS kickoff_utc,
           CASE WHEN iso_dt(m.datetime) < '{now_utc}' THEN 'postponed' ELSE 'upcoming' END AS status,
           m.home_id AS home_team_id, th.name AS home_team,
           m.away_id AS away_team_id, ta.name AS away_team,
           m.forecast_home_win, m.forecast_draw, m.forecast_away_win
    FROM r.us_matches m
    LEFT JOIN teams th ON th.team_id = m.home_id
    LEFT JOIN teams ta ON ta.team_id = m.away_id
    WHERE COALESCE(m.is_result, 0) = 0
    ORDER BY kickoff_utc, m.match_id;
    CREATE INDEX ix_m_teams ON matches(kickoff_utc, home_team_id, away_team_id);

    -- each team's side of a played match (xG for/against, pressing, expected points)
    CREATE TABLE team_matches AS
    SELECT m.match_id, m.league, m.season, m.date,
           t.team_id, tm.name AS team,
           CASE t.h_a WHEN 'h' THEN 'home' WHEN 'a' THEN 'away' END AS home_away,
           CASE WHEN m.home_team_id = t.team_id THEN m.away_team_id ELSE m.home_team_id END
             AS opponent_id,
           CASE WHEN m.home_team_id = t.team_id THEN m.away_team ELSE m.home_team END AS opponent,
           t.scored AS goals_for, t.conceded AS goals_against, wdl(t.scored, t.conceded) AS result,
           t.xg, t.xga, t.npxg, t.npxga, t.ppda, t.ppda_allowed, t.deep, t.deep_allowed,
           t.xpts, t.pts AS points
    FROM r.us_team_matches t
    LEFT JOIN matches m ON m.kickoff_utc = iso_dt(t.datetime)
                       AND t.team_id IN (m.home_team_id, m.away_team_id)
    LEFT JOIN teams tm ON tm.team_id = t.team_id
    ORDER BY m.kickoff_utc, m.match_id, home_away DESC;

    -- shots summed per player per match (only for matches whose shots were downloaded)
    CREATE TEMP TABLE shot_agg AS
    SELECT match_id, player_id,
           SUM(result IN ('Goal', 'SavedShot')) AS shots_on_target,
           SUM(CASE WHEN situation <> 'Penalty' THEN COALESCE(xg, 0) ELSE 0 END) AS npxg,
           SUM(result = 'Goal' AND situation = 'Penalty') AS penalty_goals
    FROM r.us_shots GROUP BY match_id, player_id;
    CREATE TEMP TABLE shot_matches AS SELECT DISTINCT match_id FROM r.us_shots;

    CREATE TABLE player_matches AS
    SELECT p.match_id, m.league, m.season, m.season_label, m.date,
           p.player_id, p.player, p.team_id, t.name AS team,
           CASE p.h_a WHEN 'h' THEN 'home' WHEN 'a' THEN 'away' END AS home_away,
           p.position AS match_position,
           CASE WHEN p.position = 'Sub' THEN 0 ELSE 1 END AS started,
           p.minutes, p.goals, p.own_goals, p.assists, p.yellow, p.red,
           p.shots, p.xg, p.xa, p.key_passes, p.xg_chain, p.xg_buildup,
           CASE WHEN sm.match_id IS NOT NULL THEN COALESCE(a.shots_on_target, 0) END AS shots_on_target,
           CASE WHEN sm.match_id IS NOT NULL THEN ROUND(COALESCE(a.npxg, 0), 4) END AS npxg,
           CASE WHEN sm.match_id IS NOT NULL THEN COALESCE(a.penalty_goals, 0) END AS penalty_goals
    FROM r.us_player_match p
    LEFT JOIN matches m ON m.match_id = p.match_id
    LEFT JOIN teams t ON t.team_id = p.team_id
    LEFT JOIN shot_agg a ON a.match_id = p.match_id AND a.player_id = p.player_id
    LEFT JOIN shot_matches sm ON sm.match_id = p.match_id
    ORDER BY m.kickoff_utc, p.match_id, p.team_id, p.player_id;

    -- shooter's team from the match roster; the shot's own home/away flag only as fallback
    CREATE TABLE shots AS
    SELECT x.shot_id, x.match_id, x.league, x.season, x.date, x.minute,
           x.player_id, x.player, x.team_id, t.name AS team, x.home_away_flag,
           x.result, x.xg, x.x, x.y, x.situation, x.shot_type, x.last_action, x.assist_player
    FROM (
      SELECT s.shot_id, s.match_id, m.league, m.season, m.date, m.kickoff_utc, s.minute,
             s.player_id, s.player,
             COALESCE(p.team_id, CASE s.h_a WHEN 'h' THEN m.home_team_id
                                            WHEN 'a' THEN m.away_team_id END) AS team_id,
             CASE s.h_a WHEN 'h' THEN 'home' WHEN 'a' THEN 'away' END AS home_away_flag,
             s.result, s.xg, s.x, s.y, s.situation, s.shot_type, s.last_action,
             s.assisted_by AS assist_player
      FROM r.us_shots s
      LEFT JOIN matches m ON m.match_id = s.match_id
      LEFT JOIN r.us_player_match p ON p.match_id = s.match_id AND p.player_id = s.player_id
    ) x
    LEFT JOIN teams t ON t.team_id = x.team_id
    ORDER BY x.kickoff_utc, x.match_id, x.minute, x.shot_id;

    CREATE TABLE player_seasons AS
    SELECT player_id, player, league, season, season_label(season) AS season_label,
           team AS teams, position, games, minutes, goals, npg AS non_penalty_goals,
           penalty_goals, assists, xg, npxg, xa, shots, key_passes, yellow, red,
           xg_chain, xg_buildup
    FROM r.us_player_season
    ORDER BY season, league, player_id;

    CREATE TABLE players AS
    SELECT x.player_id,
           (SELECT player FROM r.us_player_season s WHERE s.player_id = x.player_id
            ORDER BY season DESC LIMIT 1) AS name,
           MIN(x.season) AS first_season, MAX(x.season) AS last_season,
           COUNT(DISTINCT x.league) AS leagues
    FROM (SELECT player_id, league, season FROM r.us_player_season
          UNION SELECT p.player_id, m.league, m.season FROM r.us_player_match p
                JOIN r.us_matches m ON m.match_id = p.match_id) x
    GROUP BY x.player_id ORDER BY x.player_id;
    UPDATE players SET name = (SELECT MAX(player) FROM r.us_player_match p
                               WHERE p.player_id = players.player_id)
    WHERE name IS NULL;
    """)
    con.commit()
    tables = ["matches", "fixtures", "team_matches", "player_matches", "shots", "player_seasons",
              "players", "teams"]
    for t in tables:
        print(f"  {t:<16}{count(con, t):>10,}")
    no_match = con.execute("SELECT COUNT(*) FROM team_matches WHERE match_id IS NULL").fetchone()[0]
    flag_off = con.execute("""
        SELECT COUNT(*) FROM shots s JOIN matches m USING(match_id)
        WHERE s.team_id <> CASE s.home_away_flag WHEN 'home' THEN m.home_team_id
                                                 ELSE m.away_team_id END""").fetchone()[0]
    print(f"  checks: {no_match} team_matches rows without a match, "
          f"{flag_off} shots whose home/away flag disagrees with the roster team")
    con.close()


def main():
    if not os.path.exists(RAW_DB):
        sys.exit("football.db not found. Run scripts/1_transfermarkt.py build and scripts/2_understat.py build first.")
    build_tm()
    build_us()
    print("\nDone. Upload with: python3 scripts/6_to_bigquery.py --project YOUR_PROJECT")


if __name__ == "__main__":
    main()
