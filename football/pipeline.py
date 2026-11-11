"""The daily update: download what changed on Understat and load it into the warehouse.

All state is in the warehouse (raw payloads + modelled tables), so the job can run from
anywhere - GitHub Actions, Cloud Run, a laptop - and resumes by itself after a failure.

Each run:
  1. league data of the current season for every league (results, fixtures, team stats,
     player season totals); past seasons not loaded yet are added once (history from 2014/15)
  2. match details (rosters, shots) of every played match not loaded yet
  3. match details again for matches played in the last 10 days (Understat corrects its data);
     stored only if the content changed
  4. optionally, a slice of the match-level history of past seasons (--backfill-from)
Only the league-seasons and matches touched are rewritten; everything else stays as it is.
"""

import glob
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone

from . import config
from .model import model_league, model_match, validate_league, validate_match
from .understat import BudgetSpent, UnderstatError

DT = "%Y-%m-%d %H:%M:%S"
BATCH = 100                      # matches loaded per write: a crash loses at most one batch
MAX_FAILURES_IN_A_ROW = 5        # then stop asking: Understat is down or blocking us
LEAGUE_TABLES = ("matches", "fixtures", "team_matches", "player_seasons")


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def digest(payload):
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def load_leagues(wh, season, items, now, errors):
    """Replace several leagues of one season in one write per table (BigQuery writes are slow
    one by one). items: [(code, payload, fetched_at or None)]. A payload that lost played
    matches is refused and reported in `errors`. Returns {code: modelled rows} of what was loaded."""
    loaded, raw = {}, []
    stored = {}
    for m in wh.read("core.matches", ["league"], {"season": season}):
        stored[m["league"]] = stored.get(m["league"], 0) + 1
    for code, payload, fetched_at in items:
        league = config.LEAGUES[code]
        rows = model_league(league, season, payload, now)
        if len(rows["matches"]) < stored.get(league, 0) - 3:
            errors.append(f"league {league} {config.season_label(season)}: Understat now lists "
                          f"{len(rows['matches'])} played matches, {stored[league]} are loaded - "
                          f"payload refused")
            continue
        loaded[code] = rows
        raw.append({"league_code": code, "league": league, "season": season,
                    "fetched_at": (fetched_at or now).strftime(DT), "sha": digest(payload),
                    "payload": json.dumps(payload)})
    if loaded:
        names = [config.LEAGUES[c] for c in loaded]
        for t in LEAGUE_TABLES:
            wh.replace(f"core.{t}", [r for rows in loaded.values() for r in rows[t]],
                       {"season": season, "league": names})
        wh.replace("raw.league", raw, {"season": season, "league_code": list(loaded)})
    return loaded


def load_matches(wh, items):
    """items: [(match row, payload, fetched_at datetime)]. Replaces those matches' details."""
    if not items:
        return
    ids = [m["match_id"] for m, _p, _f in items]
    raw, players, shots = [], [], []
    for m, payload, fetched_at in items:
        raw.append({"match_id": m["match_id"], "league": m["league"], "season": m["season"],
                    "fetched_at": fetched_at.strftime(DT), "sha": digest(payload),
                    "payload": json.dumps(payload)})
        rows = model_match(m, payload)
        players += rows["player_matches"]
        shots += rows["shots"]
    wh.replace("core.player_matches", players, {"match_id": ids})
    wh.replace("core.shots", shots, {"match_id": ids})
    wh.replace("raw.match", raw, {"match_id": ids})          # last: marks the matches as done


def record_run(wh, command, s, started, now):
    wh.append("core.runs", [{
        "run_at": now.strftime(DT), "command": command,
        "status": "ok" if not s["errors"] else "errors", "requests": s["requests"],
        "leagues_loaded": s["leagues_loaded"], "matches_new": s["matches_new"],
        "matches_refreshed": s["matches_refreshed"], "matches_backfilled": s["matches_backfilled"],
        "seconds": round(time.time() - started, 1), "errors": "\n".join(s["errors"])[:5000] or None}])


def update(wh, client, today=None, now=None, leagues=None, backfill_from=None,
           window_days=config.REFRESH_WINDOW_DAYS, log=print):
    started = time.time()
    now = now or utcnow()
    today = today or now.date()
    season = config.current_season(today)
    codes = leagues or list(config.LEAGUES)
    names = [config.LEAGUES[c] for c in codes]
    s = {"requests": 0, "leagues_loaded": 0, "matches_new": 0, "matches_refreshed": 0,
         "matches_backfilled": 0, "matches_unchanged": 0, "errors": [], "season": season}
    batch = []
    try:
        # ---- 1. league data: current season always, missing past seasons once
        have = {(r["league_code"], r["season"]) for r in wh.read("raw.league", ["league_code", "season"])}
        todo_seasons = [season] + [y for y in range(season - 1, config.FIRST_SEASON - 1, -1)
                                   if any((c, y) not in have for c in codes)]
        for y in todo_seasons:
            items = []
            for code in codes:
                if y != season and (code, y) in have:
                    continue
                name = f"{config.LEAGUES[code]} {config.season_label(y)}"
                try:
                    payload = client.league(code, y)
                except UnderstatError as e:
                    s["errors"].append(f"league {name}: {e}")
                    continue
                if payload is None:
                    log(f"  {name}: not on Understat (skipped)")
                    continue
                problem = validate_league(payload)
                if problem:
                    s["errors"].append(f"league {name}: {problem}")
                    continue
                items.append((code, payload, None))
            n_err = len(s["errors"])
            for code, rows in load_leagues(wh, y, items, now, s["errors"]).items():
                s["leagues_loaded"] += 1
                log(f"  {config.LEAGUES[code]} {config.season_label(y)}: {len(rows['matches'])} played, "
                    f"{len(rows['fixtures'])} to play, {len(rows['player_seasons'])} players")
            for e in s["errors"][n_err:]:
                log(f"  ERROR {e}")

        # ---- 2-4. which match details to download
        played = wh.read("core.matches", None, {"season": season, "league": names})
        played.sort(key=lambda m: (m["kickoff_utc"] or "", m["match_id"]))
        known = {r["match_id"]: r for r in wh.read("raw.match", ["match_id", "fetched_at", "sha"],
                                                    {"season": season})}
        recent = (today - timedelta(days=window_days)).isoformat()
        stale = (now - timedelta(hours=config.REFRESH_AFTER_HOURS)).strftime(DT)
        todo = [("new", m) for m in played if m["match_id"] not in known]
        todo += [("refreshed", m) for m in played if m["match_id"] in known
                 and (m["date"] or "") >= recent and known[m["match_id"]]["fetched_at"] < stale]
        if backfill_from is not None:
            past = [m for m in wh.read("core.matches", None, {"season": (">=", backfill_from)})
                    if m["season"] < season and m["league"] in names]
            done = {r["match_id"] for r in wh.read("raw.match", ["match_id"],
                                                   {"season": (">=", backfill_from)})}
            past = [m for m in past if m["match_id"] not in done]
            past.sort(key=lambda m: m["kickoff_utc"] or "", reverse=True)      # most recent first
            todo += [("backfilled", m) for m in past]
        log(f"  match details: {sum(k == 'new' for k, _ in todo)} new, "
            f"{sum(k == 'refreshed' for k, _ in todo)} recent to check again, "
            f"{sum(k == 'backfilled' for k, _ in todo)} of past seasons")

        failures = 0
        for kind, m in todo:
            mid = m["match_id"]
            try:
                payload = client.match(mid)
            except UnderstatError as e:
                failures += 1
                s["errors"].append(f"match {mid}: {e}")
                if failures >= MAX_FAILURES_IN_A_ROW:
                    s["errors"].append("stopped: too many failed requests in a row")
                    break
                continue
            failures = 0
            if payload is None or validate_match(payload):
                continue                                     # not published yet: next run retries
            if kind == "refreshed" and known[mid]["sha"] == digest(payload):
                s["matches_unchanged"] += 1
                continue
            batch.append((m, payload, now))
            s[f"matches_{kind}"] += 1
            if len(batch) >= BATCH:
                load_matches(wh, batch)
                batch.clear()
    except BudgetSpent:
        log(f"  request cap reached ({client.max_requests}): the next run continues")
    finally:
        load_matches(wh, batch)
        s["requests"] = client.used
        record_run(wh, "update", s, started, now)
    log(f"  done: {s['requests']} requests, {s['matches_new']} new matches, "
        f"{s['matches_refreshed']} corrected, {s['matches_unchanged']} unchanged, "
        f"{s['matches_backfilled']} backfilled, {len(s['errors'])} errors")
    return s


def import_local(wh, raw_dir, log=print):
    """One-off: load the old local archive (data/raw_understat/) so nothing is downloaded twice."""
    started, now = time.time(), utcnow()
    s = {"requests": 0, "leagues_loaded": 0, "matches_new": 0, "matches_refreshed": 0,
         "matches_backfilled": 0, "errors": []}
    mtime = lambda p: datetime.fromtimestamp(os.path.getmtime(p), timezone.utc).replace(tzinfo=None)
    by_season = {}
    for path in sorted(glob.glob(os.path.join(raw_dir, "league", "*.json"))):
        code, season = os.path.basename(path)[:-5].rsplit("_", 1)
        if code not in config.LEAGUES:
            continue
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        problem = validate_league(payload)
        if problem:
            s["errors"].append(f"{path}: {problem}")
            continue
        by_season.setdefault(int(season), []).append((code, payload, mtime(path)))
    for season, items in sorted(by_season.items()):
        s["leagues_loaded"] += len(load_leagues(wh, season, items, now, s["errors"]))
    matches = {m["match_id"]: m for m in wh.read("core.matches")}
    done = {r["match_id"] for r in wh.read("raw.match", ["match_id"])}
    batch = []
    for path in sorted(glob.glob(os.path.join(raw_dir, "match", "*.json"))):
        try:
            mid = int(os.path.basename(path)[:-5])
        except ValueError:
            continue
        if mid in done or mid not in matches:
            continue
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        if validate_match(payload):
            continue
        batch.append((matches[mid], payload, mtime(path)))
        s["matches_new"] += 1
        if len(batch) >= BATCH:
            load_matches(wh, batch)
            batch.clear()
    load_matches(wh, batch)
    record_run(wh, "import-local", s, started, now)
    log(f"Imported {s['leagues_loaded']} league-seasons and {s['matches_new']} matches "
        f"({len(s['errors'])} skipped).")
    for e in s["errors"]:
        log(f"  skipped: {e}")
    return s
