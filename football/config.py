"""Settings shared by the whole package. Everything can be overridden by environment variables."""

import os
from datetime import date

LEAGUES = {                       # Understat code -> league name used in every table
    "EPL": "Premier League",
    "La_Liga": "La Liga",
    "Serie_A": "Serie A",
    "Bundesliga": "Bundesliga",
    "Ligue_1": "Ligue 1",
}

# First season loaded at league level (results, team stats, player season totals).
# Understat starts in 2014/15. Costs one request per league-season, once.
FIRST_SEASON = int(os.environ.get("FOOTBALL_FIRST_SEASON", "2014"))

PROJECT = os.environ.get("BQ_PROJECT", "football-model-494509")
LOCATION = os.environ.get("BQ_LOCATION", "EU")
DATASETS = {                      # logical name -> BigQuery dataset
    "raw": os.environ.get("BQ_DATASET_RAW", "football_raw"),
    "core": os.environ.get("BQ_DATASET_CORE", "football_core"),
    "tweets": os.environ.get("BQ_DATASET_TWEETS", "football_tweets"),
}

REQUEST_DELAY = 1.5          # seconds between Understat requests (keep >= 1: it is a free site)
REFRESH_WINDOW_DAYS = 10     # played matches this recent are downloaded again (Understat corrections)
REFRESH_AFTER_HOURS = 20     # ...unless they were downloaded more recently than this
MAX_REQUESTS = 3000          # safety cap per run


def current_season(today=None):
    """Season start year: from July the new season (2026 = 2026/27)."""
    today = today or date.today()
    return today.year if today.month >= 7 else today.year - 1


def season_label(season):
    return f"{season}/{str(season + 1)[2:]}" if season is not None else None
