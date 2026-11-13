"""Every table, with explicit column types. One place to change a schema.

Tables are named "<dataset>.<table>" with the logical dataset names of config.DATASETS.
Types are BigQuery types; the SQLite backend (tests, local runs) maps them to its own.
Adding a column here is enough: Warehouse.ensure() adds it to an existing table.
Ids (match, team, player, shot) are FotMob's.
"""

MATCH_KEYS = [
    ("match_id", "INT64"), ("league", "STRING"), ("season", "INT64"), ("season_label", "STRING"),
    ("date", "DATE"), ("kickoff_utc", "DATETIME"),
]

RAW_LEAGUE = [
    ("league_code", "STRING"), ("league", "STRING"), ("season", "INT64"),
    ("fetched_at", "DATETIME"), ("sha", "STRING"), ("payload", "STRING"),
]
RAW_MATCH = [
    ("match_id", "INT64"), ("league", "STRING"), ("season", "INT64"),
    ("fetched_at", "DATETIME"), ("sha", "STRING"),
    ("problem", "STRING"),                      # why the details could not be used (else NULL)
    ("payload", "STRING"),
]
MATCHES = MATCH_KEYS + [
    ("round", "STRING"),
    ("home_team_id", "INT64"), ("home_team", "STRING"),
    ("away_team_id", "INT64"), ("away_team", "STRING"),
    ("home_goals", "INT64"), ("away_goals", "INT64"), ("result", "STRING"),
]
FIXTURES = MATCH_KEYS + [
    ("round", "STRING"), ("status", "STRING"),
    ("home_team_id", "INT64"), ("home_team", "STRING"),
    ("away_team_id", "INT64"), ("away_team", "STRING"),
]
TEAM_MATCHES = MATCH_KEYS + [
    ("team_id", "INT64"), ("team", "STRING"), ("home_away", "STRING"),
    ("opponent_id", "INT64"), ("opponent", "STRING"),
    ("goals_for", "INT64"), ("goals_against", "INT64"), ("result", "STRING"), ("points", "INT64"),
]
STANDINGS = [
    ("league", "STRING"), ("season", "INT64"), ("season_label", "STRING"),
    ("team_id", "INT64"), ("team", "STRING"), ("position", "INT64"), ("played", "INT64"),
    ("wins", "INT64"), ("draws", "INT64"), ("losses", "INT64"),
    ("goals_for", "INT64"), ("goals_against", "INT64"), ("points", "INT64"), ("deduction", "STRING"),
    ("xg", "FLOAT64"), ("xga", "FLOAT64"), ("xpts", "FLOAT64"),
]
TEAM_STATS = MATCH_KEYS + [
    ("team_id", "INT64"), ("team", "STRING"), ("home_away", "STRING"),
    ("xg", "FLOAT64"), ("xga", "FLOAT64"), ("npxg", "FLOAT64"), ("npxga", "FLOAT64"),
    ("xgot", "FLOAT64"), ("shots", "INT64"), ("shots_on_target", "INT64"), ("shot_goals", "INT64"),
    ("big_chances", "INT64"), ("possession", "INT64"), ("corners", "INT64"),
    ("touches_in_box", "INT64"),
    ("big_chances_missed", "INT64"), ("xg_open_play", "FLOAT64"), ("xg_set_play", "FLOAT64"),
    ("keeper_saves", "INT64"),
]
PLAYER_MATCHES = MATCH_KEYS + [
    ("player_id", "INT64"), ("player", "STRING"), ("team_id", "INT64"), ("team", "STRING"),
    ("home_away", "STRING"), ("started", "INT64"), ("minutes", "INT64"),
    ("goals", "INT64"), ("penalty_goals", "INT64"), ("own_goals", "INT64"), ("assists", "INT64"),
    ("shots", "INT64"), ("shots_on_target", "INT64"),
    ("xg", "FLOAT64"), ("npxg", "FLOAT64"), ("xgot", "FLOAT64"), ("xa", "FLOAT64"),
    ("key_passes", "INT64"), ("touches", "INT64"), ("yellow", "INT64"), ("red", "INT64"),
    ("rating", "FLOAT64"),
    # added 13 Nov 2026; filled for the history by `python -m football rebuild` (no download)
    ("big_chances_created", "INT64"), ("big_chances_missed", "INT64"),
    ("touches_in_box", "INT64"), ("dribbles", "INT64"), ("tackles", "INT64"),
    ("interceptions", "INT64"), ("recoveries", "INT64"), ("duels_won", "INT64"),
    ("aerials_won", "INT64"), ("passes_final_third", "INT64"),
    ("saves", "INT64"), ("goals_conceded", "INT64"), ("xgot_faced", "FLOAT64"),
    ("goals_prevented", "FLOAT64"),
]
SHOTS = [
    ("shot_id", "INT64"), ("match_id", "INT64"), ("league", "STRING"), ("season", "INT64"),
    ("date", "DATE"), ("minute", "INT64"), ("minute_added", "INT64"), ("period", "STRING"),
    ("player_id", "INT64"), ("player", "STRING"), ("team_id", "INT64"), ("team", "STRING"),
    ("home_away", "STRING"), ("result", "STRING"), ("on_target", "INT64"), ("blocked", "INT64"),
    ("own_goal", "INT64"), ("xg", "FLOAT64"), ("xgot", "FLOAT64"),
    ("x", "FLOAT64"), ("y", "FLOAT64"), ("inside_box", "INT64"),
    ("situation", "STRING"), ("shot_type", "STRING"),
]
QUALITY_CHECKS = [
    ("run_at", "DATETIME"), ("check_id", "STRING"), ("level", "STRING"),
    ("description", "STRING"), ("row_count", "INT64"), ("status", "STRING"), ("sample", "STRING"),
]
RUNS = [
    ("run_at", "DATETIME"), ("command", "STRING"), ("status", "STRING"), ("requests", "INT64"),
    ("leagues_loaded", "INT64"), ("matches_new", "INT64"), ("matches_refreshed", "INT64"),
    ("matches_backfilled", "INT64"), ("seconds", "FLOAT64"), ("errors", "STRING"),
]
DRAFTS = [
    ("run_date", "DATE"), ("rank", "INT64"), ("score", "FLOAT64"), ("type", "STRING"),
    ("league", "STRING"), ("entity", "STRING"), ("team", "STRING"), ("text", "STRING"),
    ("length", "INT64"), ("evidence", "STRING"), ("fact_id", "STRING"),
    ("status", "STRING"),                       # draft -> approved / rejected -> posted
    ("created_at", "DATETIME"), ("posted_at", "DATETIME"), ("tweet_id", "STRING"),
]

TABLES = {
    "raw.league": RAW_LEAGUE,
    "raw.match": RAW_MATCH,
    "core.matches": MATCHES,
    "core.fixtures": FIXTURES,
    "core.team_matches": TEAM_MATCHES,
    "core.standings": STANDINGS,
    "core.team_stats": TEAM_STATS,
    "core.player_matches": PLAYER_MATCHES,
    "core.shots": SHOTS,
    "core.quality_checks": QUALITY_CHECKS,
    "core.runs": RUNS,
    "tweets.drafts": DRAFTS,
}

CLUSTERING = {                    # BigQuery only: keeps per-season / per-match queries cheap
    "raw.league": ["season", "league_code"],
    "raw.match": ["season", "match_id"],
    "core.matches": ["season", "league"],
    "core.fixtures": ["season", "league"],
    "core.team_matches": ["season", "league", "team_id"],
    "core.standings": ["season", "league"],
    "core.team_stats": ["season", "match_id"],
    "core.player_matches": ["season", "player_id", "match_id"],
    "core.shots": ["season", "match_id"],
    "tweets.drafts": ["run_date"],
}

DESCRIPTIONS = {
    "raw.league": "FotMob league-season answer, reduced to what is used (latest copy): matches and table.",
    "raw.match": "FotMob match answer, reduced to what is used (latest copy): team stats, players, shots.",
    "core.matches": "One row per played match: teams and score.",
    "core.fixtures": "One row per match without a result: upcoming, awaiting_result, postponed or cancelled.",
    "core.team_matches": "One row per team per played match: result and points.",
    "core.standings": "League table per league-season as FotMob gives it, with season xG, xG against and expected points.",
    "core.team_stats": "One row per team per match whose details are loaded: xG, shots, possession.",
    "core.player_matches": "One row per player who played in a match (matches whose details are loaded).",
    "core.shots": "One row per shot (matches whose details are loaded).",
    "core.quality_checks": "Result of every data-quality check, one row per check per run.",
    "core.runs": "One row per pipeline run: what was downloaded, how long, errors.",
    "tweets.drafts": "Tweet drafts, one row per draft per day. Also the history used to avoid repeats.",
}


def columns(table):
    return [c for c, _ in TABLES[table]]


def col_type(table, column):
    return dict(TABLES[table])[column]
