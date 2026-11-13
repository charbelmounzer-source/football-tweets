"""Command line.

  python -m football daily                  # update + checks + tweet drafts (what the scheduler runs)
  python -m football update                 # download and load only
  python -m football check                  # data-quality checks only
  python -m football tweets --top 25        # draft tweets from what is in the warehouse
  python -m football status                 # what is loaded, what is left, last runs
  python -m football rebuild                # re-make the core tables from raw.* (no download)

Options for every command:
  --backend bigquery|sqlite   (default bigquery; sqlite = a local file, for trying things out)
  --project my-gcp-project    (default: $BQ_PROJECT or the one in football/config.py)
  --db path                   (sqlite file, default football_local.db)
"""

import argparse
import sys
from datetime import date

from . import checks, config, pipeline, tweets
from .fotmob import Client
from .warehouse import open_warehouse


def league_codes(arg):
    if not arg or arg == "all":
        return list(config.LEAGUES)
    codes = [c.strip() for c in arg.split(",") if c.strip()]
    bad = [c for c in codes if c not in config.LEAGUES]
    if bad:
        sys.exit(f"Unknown league(s): {', '.join(bad)}. Use: {', '.join(config.LEAGUES)}")
    return codes


def cmd_update(wh, args):
    client = Client(max_requests=args.max_requests, delay=args.delay)
    return pipeline.update(wh, client, today=args.today, leagues=league_codes(args.leagues))


def cmd_check(wh, args):
    _, errors = checks.run_checks(wh, args.today or date.today(), pipeline.utcnow(),
                                  leagues=league_codes(args.leagues))
    return errors


def cmd_tweets(wh, args):
    names = None if args.leagues in (None, "all") else [config.LEAGUES[c] for c in league_codes(args.leagues)]
    return tweets.draft(wh, args.today or date.today(), leagues=names, top=args.top,
                        include_repeats=args.include_repeats, md_path=args.md)


def cmd_daily(wh, args):
    print("1/3 update")
    summary = cmd_update(wh, args)
    print("2/3 checks")
    errors = cmd_check(wh, args)
    if errors:
        sys.exit(f"{errors} data-quality error(s): no tweets drafted. See core.latest_checks.")
    print("3/3 tweet drafts")
    cmd_tweets(wh, args)
    if summary["errors"]:
        sys.exit(f"Finished with {len(summary['errors'])} download error(s) (see core.runs); "
                 f"data loaded so far is consistent and the next run retries.")


def cmd_rebuild(wh, args):
    seasons = [int(x) for x in args.seasons.split(",")] if args.seasons else None
    return pipeline.rebuild(wh, seasons=seasons)


def cmd_status(wh, args):
    rows = checks.coverage(wh, args.today or date.today())
    print(f"{'league':<16}{'season':<9}{'data':<9}{'teams':>5}{'expected':>9}{'played':>7}{'details':>8}"
          f"{'unusable':>9}{'no xG':>6}{'coverage':>9}{'to load':>8}")
    for r in rows:
        no_xg = r["no_xg"] if r["era"] == "xG" else "-"
        print(f"{r['league']:<16}{config.season_label(r['season']):<9}{r['era']:<9}{r['teams']:>5}"
              f"{r['expected']:>9}{r['played']:>7}{r['details']:>8}{len(r['unusable']):>9}{no_xg:>6}"
              f"{100 * r['share_details']:>8.1f}%{len(r['pending']):>8}" + (f"  ({r['short']})" if r["short"] else ""))
    left = sum(len(r["pending"]) for r in rows)
    print("\ndata: xG = everything; players = player and team stats, no shots/xG; lineups = lineups,"
          "\n  goal scorers and cards from the events (NULL where a name cannot be matched)."
          "\nexpected = teams x (teams - 1); coverage = share of downloaded matches (2+ days old) that have details;\n  to load = played matches whose page is not downloaded yet")
    print(f"match details still to download: {left}"
          + (f" (about {-(-left // config.MAX_REQUESTS)} run(s) of {config.MAX_REQUESTS} requests)" if left else ""))
    fetched = sorted(r["fetched_at"] for r in wh.read("raw.league", ["fetched_at"]))
    if fetched:
        print(f"latest league download (UTC): {fetched[-1]}")
    runs = sorted(wh.read("core.runs"), key=lambda r: r["run_at"])[-5:]
    print("\nlast runs")
    for r in runs:
        print(f"  {r['run_at']}  {r['command']:<8}{r['status']:<7}{r['requests']:>5} requests, "
              f"{r['matches_new']} new, {r['matches_refreshed']} corrected, "
              f"{r['matches_backfilled']} of past seasons, {r['seconds']} s")


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m football", description="Football data pipeline.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--backend", default="bigquery", choices=["bigquery", "sqlite"])
    common.add_argument("--project", help="Google Cloud project id")
    common.add_argument("--db", help="sqlite file (with --backend sqlite)")
    common.add_argument("--leagues", help="comma-separated codes (default all): "
                                          + ", ".join(config.LEAGUES))
    common.add_argument("--date", dest="today", type=date.fromisoformat,
                        help="run as if today were this day (YYYY-MM-DD)")
    dl = argparse.ArgumentParser(add_help=False)
    dl.add_argument("--max-requests", type=int, default=config.MAX_REQUESTS,
                    help=f"request cap for this run (default {config.MAX_REQUESTS})")
    dl.add_argument("--delay", type=float, default=config.REQUEST_DELAY)
    tw = argparse.ArgumentParser(add_help=False)
    tw.add_argument("--top", type=int, default=25, help="number of drafts (default 25)")
    tw.add_argument("--include-repeats", action="store_true")
    tw.add_argument("--md", help="also write the drafts to this markdown file")

    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("daily", parents=[common, dl, tw])
    sub.add_parser("update", parents=[common, dl])
    sub.add_parser("check", parents=[common])
    sub.add_parser("tweets", parents=[common, tw])
    sub.add_parser("status", parents=[common])
    rb = sub.add_parser("rebuild", parents=[common])
    rb.add_argument("--seasons", help="comma-separated start years, e.g. 2025,2026 (default all)")
    args = p.parse_args(argv)
    if getattr(args, "delay", 1.5) < 1:
        sys.exit("Please keep --delay at 1 second or more: FotMob is a free site.")

    wh = open_warehouse(args.backend, args.project, args.db)
    wh.ensure()
    result = {"daily": cmd_daily, "update": cmd_update, "check": cmd_check, "tweets": cmd_tweets,
              "status": cmd_status, "rebuild": cmd_rebuild}[args.cmd](wh, args)
    if args.cmd == "check" and result:
        sys.exit(1)


if __name__ == "__main__":
    main()
