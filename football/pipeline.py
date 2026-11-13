"""The daily update: download what changed on FotMob and load it into the warehouse.

All state is in the warehouse (slim copies of the answers + modelled tables), so the job can
run from anywhere - GitHub Actions, Cloud Run, a laptop - and resumes by itself after a failure.

Each run, in this order and within the request cap:
  1. league data of the current season for every league (results, fixtures, league table);
     past seasons back to config.FIRST_SEASON are added once
  2. match details (team stats, players, shots) of every played match of the current season
     not loaded yet
  3. match details again for matches played in the last 10 days (late corrections);
     stored only if the content changed
  4. match details of past seasons, most recent first, until the history is complete
Only the league-seasons and matches touched are rewritten; everything else stays as it is.

rebuild() re-makes every core table from the slim copies in raw.* without downloading
anything: after a change in model.py (a new column), or if FotMob ever disappears.
"""

import hashlib
import json
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import config
from .fotmob import BudgetSpent, FotMobError
from .model import (model_league, model_match, slim_league, slim_match, validate_league,
                    validate_match)

DT = "%Y-%m-%d %H:%M:%S"
BATCH = 100                      # matches loaded per write: a crash loses at most one batch
MAX_FAILURES_IN_A_ROW = 5        # then stop asking: FotMob is down or blocking us
LEAGUE_TABLES = ("matches", "fixtures", "team_matches", "standings")
MATCH_TABLES = ("team_stats", "player_matches", "shots")


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def digest(slim):
    return hashlib.sha1(json.dumps(slim, sort_keys=True).encode()).hexdigest()


def load_leagues(wh, season, items, now, errors):
    """Replace several leagues of one season in one write per table (BigQuery writes are slow
    one by one). items: [(code, slim answer)]. An answer that lost played matches is refused
    and reported in `errors`. Returns {code: modelled rows} of what was loaded."""
    loaded, raw, stored = {}, [], {}
    for m in wh.read("core.matches", ["league"], {"season": season}):
        stored[m["league"]] = stored.get(m["league"], 0) + 1
    for code, slim in items:
        league = config.LEAGUES[code]
        rows = model_league(league, season, slim, now)
        if len(rows["matches"]) < stored.get(league, 0) - 3:
            errors.append(f"league {league} {config.season_label(season)}: FotMob now lists "
                          f"{len(rows['matches'])} played matches, {stored[league]} are loaded - "
                          f"answer refused")
            continue
        loaded[code] = rows
        raw.append({"league_code": code, "league": league, "season": season,
                    "fetched_at": now.strftime(DT), "sha": digest(slim),
                    "payload": json.dumps(slim, ensure_ascii=False)})
    if loaded:
        names = [config.LEAGUES[c] for c in loaded]
        for t in LEAGUE_TABLES:
            wh.replace(f"core.{t}", [r for rows in loaded.values() for r in rows[t]],
                       {"season": season, "league": names})
        wh.replace("raw.league", raw, {"season": season, "league_code": list(loaded)})
    return loaded


def load_matches(wh, items, now, known=()):
    """items: [(match row, slim answer, problem or None)]. Writes those matches' details.
    A match with a problem keeps its raw row (so it is not asked again) but gets no table rows.

    BigQuery bills a DELETE by scanning the table, and raw.match holds every stored answer
    (over 1 GB): deleting for matches that have no rows yet cost tens of GB per run and hit
    the project's daily query quota on 13 Nov 2026. So:
      - a match with no rows anywhere (new, or loading the history) is appended: load jobs
        are free;
      - a match that has rows (known: re-checked recent matches; or rows left by a run that
        stopped half-way) is deleted and rewritten, one season at a time, so the clustering
        on season limits what BigQuery scans."""
    if not items:
        return
    ids = [m["match_id"] for m, _s, _p in items]
    # team_stats is written first, so a run that stopped half-way left rows there if anywhere
    leftovers = {r["match_id"] for r in wh.read("core.team_stats", ["match_id"], {"match_id": ids})}
    existing = (set(known) & set(ids)) | leftovers
    raw, rows = {}, {}
    for m, slim, problem in items:
        key = (m["match_id"] in existing, m["season"])
        raw.setdefault(key, []).append({
            "match_id": m["match_id"], "league": m["league"], "season": m["season"],
            "fetched_at": now.strftime(DT), "sha": digest(slim), "problem": problem,
            "payload": json.dumps(slim, ensure_ascii=False)})
        group = rows.setdefault(key, {t: [] for t in MATCH_TABLES})
        if not problem:
            for t, new in model_match(m, slim).items():
                group[t] += new
    for (replace, season), group in rows.items():
        group_ids = [r["match_id"] for r in raw[(replace, season)]]
        for t in MATCH_TABLES:
            if replace:
                wh.replace(f"core.{t}", group[t], {"season": season, "match_id": group_ids})
            else:
                wh.append(f"core.{t}", group[t])
    for (replace, season), new in raw.items():                # last: marks the matches as done
        if replace:
            wh.replace("raw.match", new, {"season": season, "match_id": [r["match_id"] for r in new]})
        else:
            wh.append("raw.match", new)


def record_run(wh, command, s, started, now):
    wh.append("core.runs", [{
        "run_at": now.strftime(DT), "command": command,
        "status": "ok" if not s["errors"] else "errors", "requests": s["requests"],
        "leagues_loaded": s["leagues_loaded"], "matches_new": s["matches_new"],
        "matches_refreshed": s["matches_refreshed"], "matches_backfilled": s["matches_backfilled"],
        "seconds": round(time.time() - started, 1), "errors": "\n".join(s["errors"])[:5000] or None}])


def update(wh, client, today=None, now=None, leagues=None,
           window_days=config.REFRESH_WINDOW_DAYS, log=print):
    started = time.time()
    now = now or utcnow()
    today = today or now.date()
    season = config.current_season(today)
    codes = leagues or list(config.LEAGUES)
    names = [config.LEAGUES[c] for c in codes]
    s = {"requests": 0, "leagues_loaded": 0, "matches_new": 0, "matches_refreshed": 0,
         "matches_backfilled": 0, "matches_unchanged": 0, "matches_waiting": 0,
         "history_left": 0, "errors": [], "season": season}
    batch, known = [], {}
    try:
        # ---- 1. league data: current season always, missing past seasons once
        have = {(r["league_code"], r["season"]) for r in wh.read("raw.league", ["league_code", "season"])}
        todo_seasons = [season] + [y for y in range(season - 1, config.FIRST_SEASON - 1, -1)
                                   if any((c, y) not in have for c in codes)]
        down = 0                                             # failed league requests in a row
        for y in todo_seasons:
            if down >= MAX_FAILURES_IN_A_ROW:
                break
            items = []
            n_err = len(s["errors"])
            for code in codes:
                if y != season and (code, y) in have:
                    continue
                name = f"{config.LEAGUES[code]} {config.season_label(y)}"
                try:
                    payload = client.league(code, y)
                except FotMobError as e:
                    s["errors"].append(f"league {name}: {e}")
                    down += 1
                    if down >= MAX_FAILURES_IN_A_ROW:
                        s["errors"].append("stopped: FotMob is not answering (too many failed "
                                           "requests in a row)")
                        break
                    continue
                down = 0
                if payload is None:
                    s["errors"].append(f"league {name}: not found on FotMob (HTTP 404)")
                    continue
                slim = slim_league(payload)
                problem = validate_league(slim, config.FOTMOB_IDS[code], y)
                if problem:
                    s["errors"].append(f"league {name}: {problem}")
                    continue
                items.append((code, slim))
            for code, rows in load_leagues(wh, y, items, now, s["errors"]).items():
                s["leagues_loaded"] += 1
                log(f"  {config.LEAGUES[code]} {config.season_label(y)}: {len(rows['matches'])} played, "
                    f"{len(rows['fixtures'])} without a result")
            for e in s["errors"][n_err:]:
                log(f"  ERROR {e}")

        # ---- 2-4. which match details to download
        played = [m for m in wh.read("core.matches", None, {"season": (">=", config.FIRST_SEASON)})
                  if m["league"] in names]
        known = {r["match_id"]: r for r in wh.read("raw.match", ["match_id", "fetched_at", "sha", "problem"])}
        current = sorted((m for m in played if m["season"] == season),
                         key=lambda m: (m["kickoff_utc"] or "", m["match_id"]))
        past = sorted((m for m in played if m["season"] < season and m["match_id"] not in known),
                      key=lambda m: (m["kickoff_utc"] or "", m["match_id"]), reverse=True)
        recent = (today - timedelta(days=window_days)).isoformat()
        stale = (now - timedelta(hours=config.REFRESH_AFTER_HOURS)).strftime(DT)
        todo = [("new", m) for m in current if m["match_id"] not in known]
        todo += [("refreshed", m) for m in current if m["match_id"] in known
                 and (m["date"] or "") >= recent and known[m["match_id"]]["fetched_at"] < stale]
        todo += [("backfilled", m) for m in past]             # most recent first
        s["history_left"] = len(past)
        log(f"  match details: {sum(k == 'new' for k, _ in todo)} new, "
            f"{sum(k == 'refreshed' for k, _ in todo)} recent to check again, "
            f"{len(past)} of past seasons still to load")

        give_up = (today - timedelta(days=config.GIVE_UP_AFTER_DAYS)).isoformat()
        failures = 0
        for kind, m in todo:
            mid = m["match_id"]
            try:
                payload = client.match(mid)
            except FotMobError as e:
                failures += 1
                s["errors"].append(f"match {mid}: {e}")
                log(f"  ERROR match {mid}: {e}")
                if failures >= MAX_FAILURES_IN_A_ROW:
                    s["errors"].append("stopped: too many failed requests in a row")
                    break
                continue
            failures = 0
            slim = slim_match(payload)
            problem = "not found on FotMob (HTTP 404)" if payload is None else validate_match(slim, m)
            if problem and kind == "refreshed":
                continue                                     # keep the good copy we have
            if problem and (m["date"] or "") > give_up:
                s["matches_waiting"] += 1                    # not published yet: next run retries
                continue
            if problem:
                log(f"  match {mid} ({m['home_team']} - {m['away_team']}, {m['date']}): {problem}; "
                    f"not asked again")
            elif kind == "refreshed" and known[mid]["sha"] == digest(slim):
                s["matches_unchanged"] += 1
                continue
            batch.append((m, slim, problem))
            s[f"matches_{kind}"] += 1
            s["history_left"] -= kind == "backfilled"
            if len(batch) >= BATCH:
                load_matches(wh, batch, now, known)
                batch.clear()
    except BudgetSpent:
        log(f"  request cap reached ({client.max_requests}): the next run continues")
    finally:
        load_matches(wh, batch, now, known)
        s["requests"] = client.used
        record_run(wh, "update", s, started, now)
    log(f"  done: {s['requests']} requests, {s['matches_new']} new matches, "
        f"{s['matches_refreshed']} corrected, {s['matches_unchanged']} unchanged, "
        f"{s['matches_backfilled']} of past seasons ({s['history_left']} left), "
        f"{s['matches_waiting']} not published yet, {len(s['errors'])} errors")
    return s


def rebuild(wh, now=None, seasons=None, log=print):
    """Re-model the core tables from raw.* (no request to FotMob), one season at a time.
    Raw rows are not touched, so the freshness check and the update keep working as before."""
    now = now or utcnow()
    have = sorted({r["season"] for r in wh.read("raw.league", ["season"])})
    totals = defaultdict(int)
    for y in (seasons or have):
        leagues = wh.read("raw.league", ["league", "payload"], {"season": y})
        rows = {t: [] for t in LEAGUE_TABLES}
        for r in leagues:
            for t, new in model_league(r["league"], y, json.loads(r["payload"]), now).items():
                rows[t] += new
        if leagues:
            names = [r["league"] for r in leagues]
            for t in LEAGUE_TABLES:
                wh.replace(f"core.{t}", rows[t], {"season": y, "league": names})
        played = {m["match_id"]: m for m in wh.read("core.matches", None, {"season": y})}
        rows = {t: [] for t in MATCH_TABLES}
        n = 0
        for r in wh.read("raw.match", ["match_id", "problem", "payload"], {"season": y}):
            m = played.get(r["match_id"])
            if r["problem"] or m is None:
                continue
            for t, new in model_match(m, json.loads(r["payload"])).items():
                rows[t] += new
            n += 1
        for t in MATCH_TABLES:
            wh.replace(f"core.{t}", rows[t], {"season": y})
        totals["leagues"] += len(leagues)
        totals["matches"] += n
        log(f"  {config.season_label(y)}: {len(leagues)} league answers, {len(played)} played matches, "
            f"{n} with details re-modelled")
    log(f"  rebuilt from raw: {totals['leagues']} league-seasons, {totals['matches']} matches "
        f"(no download)")
    return dict(totals)
