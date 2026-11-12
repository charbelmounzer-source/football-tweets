"""Settings shared by the whole package. Everything can be overridden by environment variables."""

import os
from datetime import date

LEAGUES = {                       # our code -> league name used in every table
    "EPL": "Premier League",
    "La_Liga": "La Liga",
    "Serie_A": "Serie A",
    "Bundesliga": "Bundesliga",
    "Ligue_1": "Ligue 1",
}
FOTMOB_IDS = {"EPL": 47, "La_Liga": 87, "Serie_A": 55, "Bundesliga": 54, "Ligue_1": 53}

# First season loaded. FotMob has shots and xG from 2020/21 (checked on the Premier League);
# before that it only has results (and player stats without xG from 2016/17).
FIRST_SEASON = int(os.environ.get("FOOTBALL_FIRST_SEASON", "2020"))

PROJECT = os.environ.get("BQ_PROJECT", "football-model-494509")
LOCATION = os.environ.get("BQ_LOCATION", "EU")
DATASETS = {                      # logical name -> BigQuery dataset
    "raw": os.environ.get("BQ_DATASET_RAW", "football_raw"),
    "core": os.environ.get("BQ_DATASET_CORE", "football_core"),
    "tweets": os.environ.get("BQ_DATASET_TWEETS", "football_tweets"),
}

REQUEST_DELAY = 1.5          # seconds between FotMob requests (keep >= 1: it is a free site)
REFRESH_WINDOW_DAYS = 10     # played matches this recent are downloaded again (late corrections)
REFRESH_AFTER_HOURS = 20     # ...unless they were downloaded more recently than this
GIVE_UP_AFTER_DAYS = 14      # a played match still without usable details after this is not asked again
MAX_REQUESTS = 2000          # per run. What is left of the history is continued by the next run


def current_season(today=None):
    """Season start year: from July the new season (2026 = 2026/27)."""
    today = today or date.today()
    return today.year if today.month >= 7 else today.year - 1


def season_label(season):
    return f"{season}/{str(season + 1)[2:]}" if season is not None else None


def fotmob_season(season):
    """2026 -> '2026/2027', the way FotMob names a season."""
    return f"{season}/{season + 1}"
