#!/bin/bash
# Weekly refresh: new Understat matches -> links -> databases -> index -> BigQuery -> checks -> tweet drafts.
# Run from anywhere:  bash legacy/weekly_update.sh
set -e
cd "$(dirname "$0")/.."
PROJECT=football-model-494509

python3 legacy/2_understat.py update
python3 legacy/2_understat.py build
python3 legacy/3_link.py
python3 legacy/4_build_sources.py
python3 legacy/5_build_index.py
python3 legacy/6_to_bigquery.py --project "$PROJECT" --sources us,index
python3 legacy/7_sanity_checks.py --project "$PROJECT"
python3 legacy/8_tweets.py
echo "Weekly update done. Check index/review.csv, output/sanity_report/summary.csv and output/tweets/."
