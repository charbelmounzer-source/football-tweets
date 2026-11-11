#!/usr/bin/env python3
"""
scripts/6_to_bigquery.py - upload the per-source databases to BigQuery, one dataset per source.

  football_tm.db     -> dataset football_tm      (Transfermarkt)
  football_us.db     -> dataset football_us      (Understat)
  football_index.db  -> dataset football_index   (links between sources, when built)

Usage
  python3 scripts/6_to_bigquery.py --project my-gcp-project                  # every database that exists
  python3 scripts/6_to_bigquery.py --project my-gcp-project --sources us     # only football_us
  python3 scripts/6_to_bigquery.py --project my-gcp-project --clean          # also delete leftover tables
  python3 scripts/6_to_bigquery.py --dry-run                                 # export files only

Needs the Google Cloud SDK ("bq" command) and a login (gcloud auth login).
Re-running replaces the tables (no duplicates, nothing to delete first).
No third-party Python packages needed.
"""

import argparse
import csv
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys

import paths
OUT_DIR = paths.BQ_EXPORT_DIR

SOURCES = {   # short name -> (database file, dataset, dataset description)
    "tm": ("football_tm.db", "football_tm", "Transfermarkt: top 5 leagues 2021/22-2025/26, ids as in Transfermarkt"),
    "us": ("football_us.db", "football_us", "Understat: top 5 leagues 2021/22-current season, ids as in Understat"),
    "index": ("football_index.db", "football_index", "Links between sources: one row per source id"),
}

DESCRIPTIONS = {   # shown in BigQuery's table details; full field docs in README.md
    ("tm", "matches"): "One row per match. Score, round, league position, managers, referee, venue.",
    ("tm", "player_matches"): "One row per player per match, squad members included (lineups + match stats).",
    ("tm", "events"): "One row per goal, card, substitution or shootout kick.",
    ("tm", "players"): "One row per player: profile (birth date, nationality, position, foot, height).",
    ("tm", "teams"): "One row per club playing in the loaded matches.",
    ("tm", "transfers"): "One row per transfer, whole careers, with fee and market value at the move.",
    ("tm", "market_values"): "One row per player valuation (Transfermarkt market value history).",
    ("us", "matches"): "One row per played match: score, xG, pre-match forecast.",
    ("us", "fixtures"): "One row per match without a result: upcoming or postponed. Rebuilt each run.",
    ("us", "team_matches"): "One row per team per played match: xG for/against, PPDA, deep, xPts.",
    ("us", "player_matches"): "One row per player who appeared in a match: minutes, goals, xG, xA, shots.",
    ("us", "shots"): "One row per shot: minute, result, xG, position, situation, body part.",
    ("us", "player_seasons"): "One row per player per league-season: Understat season totals.",
    ("us", "players"): "One row per Understat player: name and seasons covered.",
    ("us", "teams"): "One row per Understat club.",
}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?$")
INT64_MAX = 2 ** 63 - 1


def infer_type(values, declared):
    """BigQuery type from the actual values (SQLite types are loose)."""
    vals = [v for v in values if v is not None and v != ""]
    if not vals:
        decl = (declared or "").upper()
        return "INT64" if "INT" in decl else "FLOAT64" if "REAL" in decl else "STRING"
    if all(isinstance(v, int) and not isinstance(v, bool) and abs(v) <= INT64_MAX for v in vals):
        return "INT64"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
        return "FLOAT64"
    if all(isinstance(v, str) for v in vals):
        if all(DATE_RE.match(v) for v in vals):
            return "DATE"
        if all(DATETIME_RE.match(v) for v in vals):
            return "DATETIME"
    return "STRING"


def to_csv_value(v, bq_type):
    if v is None:
        return ""
    if bq_type == "DATETIME":
        return v.replace("T", " ")
    if bq_type == "INT64" and isinstance(v, float):
        return str(int(v))
    return v


def export(con, name, out_dir):
    cols = con.execute(f'PRAGMA table_info("{name}")').fetchall()
    col_names = [c[1] for c in cols]
    rows = con.execute(f'SELECT * FROM "{name}"').fetchall()
    types = [infer_type([r[i] for r in rows], cols[i][2]) for i in range(len(col_names))]
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, f"{name}.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(col_names)
        for r in rows:
            w.writerow([to_csv_value(v, t) for v, t in zip(r, types)])
    schema_path = os.path.join(out_dir, f"{name}.schema.json")
    with open(schema_path, "w", encoding="utf-8") as f:
        json.dump([{"name": n, "type": t, "mode": "NULLABLE"} for n, t in zip(col_names, types)],
                  f, indent=1)
    return csv_path, schema_path, len(rows)


def run(cmd, check=True):
    res = subprocess.run(cmd, capture_output=True, text=True)
    if check and res.returncode != 0:
        print("  command failed:", " ".join(cmd))
        print("  " + (res.stderr or res.stdout).strip().replace("\n", "\n  "))
        sys.exit(1)
    return res


def upload_source(key, args):
    db_name, dataset, ds_desc = SOURCES[key]
    con = sqlite3.connect(os.path.join(paths.DATA_DIR, db_name))
    tables = [n for (n,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    ds = f"{args.project}:{dataset}" if args.project else dataset
    print(f"\n{dataset}  (from {db_name})")
    if not args.dry_run:
        if run([args.bq, "show", "--format=none", ds], check=False).returncode != 0:
            print(f"  creating dataset in {args.location}")
            run([args.bq, "--location", args.location, "mk", "--dataset", "--description", ds_desc, ds])
    for name in tables:
        csv_path, schema_path, n = export(con, name, os.path.join(OUT_DIR, dataset))
        status = "exported"
        if not args.dry_run:
            run([args.bq, "--location", args.location, "load", "--replace",
                 "--source_format=CSV", "--skip_leading_rows=1", "--allow_quoted_newlines",
                 f"--schema={schema_path}", f"{ds}.{name}", csv_path])
            desc = DESCRIPTIONS.get((key, name))
            if desc:
                run([args.bq, "update", "--description", desc, f"{ds}.{name}"], check=False)
            status = "loaded"
        print(f"  {name:<20}{n:>10,}  {status}")
    con.close()
    if args.clean and not args.dry_run:
        listing = run([args.bq, "ls", "--format=json", "--max_results=1000", ds])
        existing = [t["tableReference"]["tableId"] for t in json.loads(listing.stdout or "[]")]
        for t in sorted(set(existing) - set(tables)):
            run([args.bq, "rm", "-f", "-t", f"{ds}.{t}"])
            print(f"  {t:<20}{'':>10}  deleted (not in {db_name})")


def main():
    ap = argparse.ArgumentParser(description="Upload the per-source databases to BigQuery.")
    ap.add_argument("--project", help="Google Cloud project id")
    ap.add_argument("--sources", help="comma-separated: tm, us, index (default: every database that exists)")
    ap.add_argument("--location", default="EU", help="dataset location (default: EU)")
    ap.add_argument("--dry-run", action="store_true", help="write files to output/bq_export/ only")
    ap.add_argument("--clean", action="store_true",
                    help="delete tables in each dataset that are not in its database")
    ap.add_argument("--bq", default="bq", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.sources:
        wanted = [s.strip() for s in args.sources.split(",") if s.strip()]
        unknown = [s for s in wanted if s not in SOURCES]
        if unknown:
            sys.exit(f"Unknown source(s): {', '.join(unknown)}. Use tm, us, index.")
        for s in wanted:
            if not os.path.exists(os.path.join(paths.DATA_DIR, SOURCES[s][0])):
                sys.exit(f"{SOURCES[s][0]} not found. Run scripts/4_build_sources.py first.")
    else:
        wanted = [s for s in SOURCES if os.path.exists(os.path.join(paths.DATA_DIR, SOURCES[s][0]))]
        if not wanted:
            sys.exit("No database to upload. Run scripts/4_build_sources.py first.")
    if not args.dry_run:
        if not args.project:
            sys.exit("Give your project: --project my-gcp-project  (or use --dry-run)")
        if not shutil.which(args.bq):
            sys.exit("The 'bq' command was not found. Install the Google Cloud SDK "
                     "(https://cloud.google.com/sdk/docs/install), then run: gcloud auth login")

    for s in wanted:
        upload_source(s, args)
    if args.dry_run:
        print(f"\nFiles are in {OUT_DIR}/<dataset>/ (CSV + schema JSON per table).")
    else:
        print(f"\nDone. Open https://console.cloud.google.com/bigquery?project={args.project}")


if __name__ == "__main__":
    main()
