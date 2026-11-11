"""Where everything lives. All scripts import this, so the folder layout is defined once.

football-db/
  scripts/   the pipeline, numbered in run order (+ this file)
  config/    your manual settings: link_overrides.csv, club_names.csv (both optional)
  index/     master copy of the cross-source keys - BACK IT UP
  data/      downloads and databases (all can be recreated)
  output/    files written for you: bq_export/, sanity_report/
"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONFIG_DIR = os.path.join(ROOT, "config")
LINK_OVERRIDES = os.path.join(CONFIG_DIR, "link_overrides.csv")
CLUB_NAMES = os.path.join(CONFIG_DIR, "club_names.csv")

INDEX_DIR = os.path.join(ROOT, "index")

DATA_DIR = os.path.join(ROOT, "data")
RAW_TM_DIR = os.path.join(DATA_DIR, "raw_transfermarkt")
RAW_US_DIR = os.path.join(DATA_DIR, "raw_understat")
WORK_DB = os.path.join(DATA_DIR, "football.db")          # working db: both sources + links
TM_DB = os.path.join(DATA_DIR, "football_tm.db")
US_DB = os.path.join(DATA_DIR, "football_us.db")
INDEX_DB = os.path.join(DATA_DIR, "football_index.db")

OUTPUT_DIR = os.path.join(ROOT, "output")
BQ_EXPORT_DIR = os.path.join(OUTPUT_DIR, "bq_export")
SANITY_REPORT_DIR = os.path.join(OUTPUT_DIR, "sanity_report")

os.makedirs(DATA_DIR, exist_ok=True)
