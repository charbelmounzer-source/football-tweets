"""Every table, with explicit column types. One place to change a schema.

Tables are named "<dataset>.<table>" with the logical dataset names of config.DATASETS.
Types are BigQuery types; the SQLite backend (tests, local runs) maps them to its own.
Adding a column here is enough: Warehouse.ensure() adds it to an existing table.
"""

RAW_LEAGUE = [
    ("league_code", "STRING"), ("league", "STRING"), ("season", "INT64"),
    ("fetched_at", "DATETIME"), ("sha", "STRING"), ("payload", "STRING"),
]
RAW_MATCH = [
    ("match_id", "INT64"), ("league", "STRING"), ("season", "INT64"),
    ("fetched_at", "DATETIME"), ("sha", "STRING"), ("payload", "STRING"),
]
MATCHES = [
    ("match_id", "INT64"), ("league", "STRING"), ("season", "INT64"), ("season_label", "STRING"),
    ("date", "DATE"), ("kickoff_utc", "DATETIME"),
    ("home_team_id", "INT64"), ("home_team", "STRING"),
    ("away_team_id", "INT64"), ("away_team", "STRING"),
    ("home_goals", "INT64"), ("away_goals", "INT64"), ("result", "STRING"),
    ("home_xg", "FLOAT64"), ("away_xg", "FLOAT64"),
    ("forecast_home_win", "FLOAT64"), ("forecast_draw", "FLOAT64"), ("forecast_away_win", "FLOAT64"),
]
FIXTURES = [
    ("match_id", "INT64"), ("league", "STRING"), ("season", "INT64"), ("season_label", "STRING"),
    ("date", "DATE"), ("kickoff_utc", "DATETIME"), ("status", "STRING"),
    ("home_team_id", "INT64"), ("home_team", "STRING"),
    ("away_team_id", "INT64"), ("away_team", "STRING"),
    ("forecast_home_win", "FLOAT64"), ("forecast_draw", "FLOAT64"), ("forecast_away_win", "FLOAT64"),
]
TEAM_MATCHES = [
    ("match_id", "INT64"), ("league", "STRING"), ("season", "INT64"), ("season_label", "STRING"),
    ("date", "DATE"), ("kickoff_utc", "DATETIME"),
    ("team_id", "INT64"), ("team", "STRING"), ("home_away", "STRING"),
    ("opponent_id", "INT64"), ("opponent", "STRING"),
    ("goals_for", "INT64"), ("goals_against", "INT64"), ("result", "STRING"),
    ("xg", "FLOAT64"), ("xga", "FLOAT64"), ("npxg", "FLOAT64"), ("npxga", "FLOAT64"),
    ("ppda", "FLOAT64"), ("ppda_allowed", "FLOAT64"), ("deep", "INT64"), ("deep_allowed", "INT64"),
    ("xpts", "FLOAT64"), ("points", "INT64"),
]
PLAYER_SEASONS = [
    ("player_id", "INT64"), ("player", "STRING"), ("league", "STRING"), ("season", "INT64"),
    ("season_label", "STRING"), ("teams", "STRING"), ("position", "STRING"),
    ("games", "INT64"), ("minutes", "INT64"), ("goals", "INT64"), ("non_penalty_goals", "INT64"),
    ("penalty_goals", "INT64"), ("assists", "INT64"), ("xg", "FLOAT64"), ("npxg", "FLOAT64"),
    ("xa", "FLOAT64"), ("shots", "INT64"), ("key_passes", "INT64"), ("yellow", "INT64"),
    ("red", "INT64"), ("xg_chain", "FLOAT64"), ("xg_buildup", "FLOAT64"),
]
PLAYER_MATCHES = [
    ("match_id", "INT64"), ("league", "STRING"), ("season", "INT64"), ("season_label", "STRING"),
    ("date", "DATE"), ("kickoff_utc", "DATETIME"),
    ("player_id", "INT64"), ("player", "STRING"), ("team_id", "INT64"), ("team", "STRING"),
    ("home_away", "STRING"), ("match_position", "STRING"), ("started", "INT64"),
    ("minutes", "INT64"), ("goals", "INT64"), ("own_goals", "INT64"), ("assists", "INT64"),
    ("yellow", "INT64"), ("red", "INT64"), ("shots", "INT64"), ("xg", "FLOAT64"), ("xa", "FLOAT64"),
    ("key_passes", "INT64"), ("xg_chain", "FLOAT64"), ("xg_buildup", "FLOAT64"),
    ("shots_on_target", "INT64"), ("npxg", "FLOAT64"), ("penalty_goals", "INT64"),
]
SHOTS = [
    ("shot_id", "INT64"), ("match_id", "INT64"), ("league", "STRING"), ("season", "INT64"),
    ("date", "DATE"), ("minute", "INT64"), ("player_id", "INT64"), ("player", "STRING"),
    ("team_id", "INT64"), ("team", "STRING"), ("home_away_flag", "STRING"), ("result", "STRING"),
    ("xg", "FLOAT64"), ("x", "FLOAT64"), ("y", "FLOAT64"), ("situation", "STRING"),
    ("shot_type", "STRING"), ("last_action", "STRING"), ("assist_player", "STRING"),
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
    "core.player_seasons": PLAYER_SEASONS,
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
    "core.player_seasons": ["season", "league", "player_id"],
    "core.player_matches": ["season", "player_id", "match_id"],
    "core.shots": ["season", "match_id"],
    "tweets.drafts": ["run_date"],
}

DESCRIPTIONS = {
    "raw.league": "Understat league-season payload exactly as downloaded (latest copy). Source of the league-level tables.",
    "raw.match": "Understat match payload exactly as downloaded (latest copy): rosters and shots.",
    "core.matches": "One row per played match: score, xG, pre-match forecast.",
    "core.fixtures": "One row per match without a result: upcoming, awaiting_result or postponed.",
    "core.team_matches": "One row per team per played match: xG for/against, PPDA, deep, xPts.",
    "core.player_seasons": "One row per player per league-season: Understat season totals.",
    "core.player_matches": "One row per player who appeared in a match (matches whose details are loaded).",
    "core.shots": "One row per shot (matches whose details are loaded).",
    "core.quality_checks": "Result of every data-quality check, one row per check per run.",
    "core.runs": "One row per pipeline run: what was downloaded, how long, errors.",
    "tweets.drafts": "Tweet drafts, one row per draft per day. Also the history used to avoid repeats.",
}


def columns(table):
    return [c for c, _ in TABLES[table]]


def col_type(table, column):
    return dict(TABLES[table])[column]
