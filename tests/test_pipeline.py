from collections import Counter
from datetime import datetime, timedelta

import pytest

from football import checks, config, pipeline
from football.model import model_league, model_match, slim_league, slim_match, validate_match
from football.warehouse import SQLiteWarehouse
from conftest import NOW
from fake_fotmob import FakeFotMob

quiet = lambda *_: None
N_LEAGUES, PER_ROUND, ROUNDS_PLAYED, SEASON_MATCHES = 5, 5, 10, 90
CURRENT = N_LEAGUES * PER_ROUND * ROUNDS_PLAYED                  # played matches of 2026/27
HISTORY = N_LEAGUES * 3 * SEASON_MATCHES                         # 2023/24 to 2025/26
FIRST_EPL = 2026 * 1000                                          # id of the first EPL match of 2026/27


def counts(wh):
    return {t: len(wh.read(t)) for t in ("raw.league", "raw.match", "core.matches", "core.fixtures",
                                         "core.team_matches", "core.standings", "core.team_stats",
                                         "core.player_matches", "core.shots")}


def test_model_league_results_statuses_and_table():
    slim = slim_league(FakeFotMob(NOW).league("EPL", 2026))
    rows = model_league("Premier League", 2026, slim, NOW)
    assert len(rows["matches"]) == PER_ROUND * ROUNDS_PLAYED
    assert len(rows["matches"]) + len(rows["fixtures"]) == SEASON_MATCHES
    assert len(rows["team_matches"]) == 2 * len(rows["matches"])
    assert {f["status"] for f in rows["fixtures"]} == {"upcoming"}
    m = rows["matches"][0]
    sides = [r for r in rows["team_matches"] if r["match_id"] == m["match_id"]]
    home = next(r for r in sides if r["home_away"] == "home")
    assert (home["goals_for"], home["goals_against"]) == (m["home_goals"], m["away_goals"])
    assert home["team_id"] == m["home_team_id"] and home["opponent_id"] == m["away_team_id"]
    assert home["points"] == {"H": 3, "D": 1, "A": 0}[m["result"]]
    # the league table is FotMob's own count: it agrees with the results
    for t in rows["standings"]:
        mine = [r for r in rows["team_matches"] if r["team_id"] == t["team_id"]]
        assert (t["played"], t["points"]) == (len(mine), sum(r["points"] for r in mine))
        assert t["goals_for"] == sum(r["goals_for"] for r in mine) and t["xpts"] is not None
    # a match that kicked off 5 hours ago without a result is not "postponed"
    later = model_league("Premier League", 2026, slim, datetime(2026, 10, 24, 20, 0))
    assert Counter(f["status"] for f in later["fixtures"])["awaiting_result"] == PER_ROUND


def test_abandoned_match_is_not_a_result():
    payload = FakeFotMob(NOW).league("EPL", 2026)
    st = payload["fixtures"]["allMatches"][0]["status"]           # as FotMob marks an abandoned match
    st.update(cancelled=True, reason={"longKey": "aborted"})
    rows = model_league("Premier League", 2026, slim_league(payload), NOW)
    assert len(rows["matches"]) == PER_ROUND * ROUNDS_PLAYED - 1
    assert Counter(f["status"] for f in rows["fixtures"])["cancelled"] == 1


def test_model_match_penalty_own_goal_cards_and_missing_zeros():
    fake = FakeFotMob(NOW)
    league = model_league("Premier League", 2026, slim_league(fake.league("EPL", 2026)), NOW)
    match = league["matches"][0]
    payload = fake.match(match["match_id"])
    home, away = match["home_team_id"], match["away_team_id"]
    shooter = payload["content"]["shotmap"]["shots"][0]["playerId"]
    taker, defender = home * 100 + 3, away * 100 + 2
    payload["content"]["shotmap"]["shots"] += [
        {"id": 1, "eventType": "Goal", "teamId": home, "playerId": taker, "playerName": "Taker",
         "situation": "Penalty", "expectedGoals": 0.79, "isOwnGoal": False, "isOnTarget": True},
        {"id": 2, "eventType": "Goal", "teamId": away, "playerId": defender, "playerName": "Defender",
         "situation": "RegularPlay", "expectedGoals": None, "isOwnGoal": True, "isOnTarget": True}]
    payload["content"]["matchFacts"]["events"]["events"] += [
        # the event list credits the own goal to an attacker: the shot data is what counts
        {"type": "Goal", "isHome": True, "ownGoal": None, "playerId": shooter, "nameStr": "Attacker"},
        {"type": "Card", "card": "Yellow", "playerId": taker}, {"type": "Card", "card": "YellowRed", "playerId": taker},
        {"type": "Card", "card": "Red", "playerId": defender}]
    slim = slim_match(payload)
    assert validate_match(slim, match) is None
    assert "poll" not in str(slim) and "h2h" not in str(slim)       # only what is used is kept
    rows = model_match(match, slim)
    p = {r["player_id"]: r for r in rows["player_matches"]}
    assert len(p) == 24 and all(r["minutes"] for r in p.values())    # unused substitutes left out
    assert (p[taker]["penalty_goals"], p[taker]["yellow"], p[taker]["red"]) == (1, 1, 1)
    assert (p[defender]["own_goals"], p[defender]["red"], p[shooter]["own_goals"]) == (1, 1, 0)
    assert p[shooter]["goals"] == sum(x["playerId"] == shooter and x["eventType"] == "Goal"
                                      for x in fake.match(match["match_id"])["content"]["shotmap"]["shots"])
    assert p[taker]["shots"] == 0 and p[taker]["xg"] == 0.0          # stat left out by FotMob = zero
    assert sum(r["started"] for r in p.values()) == 22
    t = {r["team_id"]: r for r in rows["team_stats"]}
    assert t[home]["xga"] == t[away]["xg"] and t[home]["possession"] == 55
    fake_goals = sum(x["eventType"] == "Goal" for x in fake.match(match["match_id"])["content"]["shotmap"]["shots"]
                     if x["teamId"] == home)
    assert t[home]["shot_goals"] == fake_goals + 1 and t[away]["shot_goals"] == match["away_goals"]
    assert {s["shot_id"]: s["own_goal"] for s in rows["shots"]}[2] == 1


def test_first_run_loads_results_table_and_every_match(loaded):
    wh, client, s = loaded
    c = counts(wh)
    assert c["raw.league"] == N_LEAGUES * 4                              # 2023/24 to 2026/27
    assert c["core.matches"] == CURRENT + HISTORY == c["raw.match"]
    assert c["core.team_stats"] == 2 * c["raw.match"] and c["core.player_matches"] == 24 * c["raw.match"]
    assert c["core.standings"] == N_LEAGUES * 4 * 10
    assert (s["matches_new"], s["matches_backfilled"], s["history_left"]) == (CURRENT, HISTORY, 0)
    assert not s["errors"]
    # current season first, then the history from the most recent match back
    order = [c[1] for c in client.calls if c[0] == "match"]
    seasons = [mid % 1_000_000 // 1000 for mid in order]
    assert seasons == sorted(seasons, key=lambda y: (y != 2026, -y))


def test_rerun_is_idempotent_and_cheap(loaded):
    wh, _, _ = loaded
    before = counts(wh)
    client = FakeFotMob(NOW)
    s = pipeline.update(wh, client, now=NOW, log=quiet)
    assert counts(wh) == before
    assert client.used == N_LEAGUES                 # only the 5 current league answers
    assert s["matches_new"] == 0


def test_next_day_checks_recent_matches_again_and_stores_only_changes(loaded):
    wh, _, _ = loaded
    before = counts(wh)
    tomorrow = NOW + timedelta(days=1)
    client = FakeFotMob(tomorrow)
    changed = FIRST_EPL + 9 * 10                           # an EPL match of the last round

    def correct(payload):                                  # FotMob fixes a shot's xG
        payload["content"]["shotmap"]["shots"][0]["expectedGoals"] = 0.999
        payload["content"]["matchFacts"]["poll"] = {"votes": 99999}     # noise that is not stored
        return payload
    client.tweak[("match", changed)] = correct
    for mid in (FIRST_EPL + 9 * 10 + 1, FIRST_EPL + 9 * 10 + 2):        # noise alone changes nothing
        client.tweak[("match", mid)] = lambda p: dict(p, seo={"path": "moved"})
    s = pipeline.update(wh, client, now=tomorrow, log=quiet)
    recent_rounds = 2                                      # 10-day window: rounds of 10 and 17 October
    assert client.used == N_LEAGUES + N_LEAGUES * PER_ROUND * recent_rounds
    assert s["matches_refreshed"] == 1 and s["matches_unchanged"] == client.used - N_LEAGUES - 1
    assert counts(wh) == before
    assert max(x["xg"] for x in wh.read("core.shots", ["xg"], {"match_id": changed})) == 0.999


def test_new_round_adds_only_new_matches(loaded):
    wh, _, _ = loaded
    before = counts(wh)
    later = datetime(2026, 10, 25, 6, 0)
    s = pipeline.update(wh, FakeFotMob(later), now=later, log=quiet)
    after = counts(wh)
    assert s["matches_new"] == N_LEAGUES * PER_ROUND
    assert after["core.matches"] - before["core.matches"] == N_LEAGUES * PER_ROUND
    assert before["core.fixtures"] - after["core.fixtures"] == N_LEAGUES * PER_ROUND
    ids = [m["match_id"] for m in wh.read("core.matches", ["match_id"])]
    assert len(ids) == len(set(ids))


def test_request_cap_resumes_next_run_current_season_first(wh):
    first = FakeFotMob(NOW, max_requests=20 + CURRENT + 30)
    s = pipeline.update(wh, first, now=NOW, log=quiet)
    assert (s["matches_new"], s["matches_backfilled"], s["history_left"]) == (CURRENT, 30, HISTORY - 30)
    got = wh.read("core.player_matches", ["season", "date"], {"season": ("<", 2026)})
    assert {r["season"] for r in got} == {2025} and min(r["date"] for r in got) > "2025-11-01"
    second = FakeFotMob(NOW)
    pipeline.update(wh, second, now=NOW, log=quiet)
    assert second.used == N_LEAGUES + HISTORY - 30                       # nothing downloaded twice
    assert len(wh.read("raw.match")) == CURRENT + HISTORY and len(wh.read("core.runs")) == 2


def test_one_league_down_does_not_block_the_others(wh):
    client = FakeFotMob(NOW)
    client.fail_leagues.add(("La_Liga", 2026))
    client.fail_matches.add(FIRST_EPL)
    s = pipeline.update(wh, client, now=NOW, leagues=None, log=quiet)
    loaded = {r["league"] for r in wh.read("core.matches", ["league"], {"season": 2026})}
    assert loaded == set(config.LEAGUES.values()) - {"La Liga"}
    assert len(s["errors"]) == 2
    assert wh.read("core.runs")[0]["status"] == "errors"
    _, errors = checks.run_checks(wh, NOW.date(), NOW, log=quiet)
    assert errors == 1                                     # F1: La Liga not downloaded
    # next run: everything recovers
    pipeline.update(wh, FakeFotMob(NOW), now=NOW, log=quiet)
    assert len(wh.read("raw.match")) == CURRENT + HISTORY


def test_bad_league_answer_never_replaces_good_data(loaded):
    wh, _, _ = loaded
    before = counts(wh)
    client = FakeFotMob(NOW)
    client.tweak[("league", "EPL", 2026)] = lambda p: dict(p, fixtures={"allMatches": []})
    lost = lambda p: dict(p, fixtures={"allMatches": p["fixtures"]["allMatches"][40:]})
    client.tweak[("league", "Serie_A", 2026)] = lost
    # the season parameter ignored: the answer is for another season
    client.tweak[("league", "Ligue_1", 2026)] = lambda p: dict(p, details=dict(p["details"], selectedSeason="2025/2026"))
    s = pipeline.update(wh, client, now=NOW, log=quiet)
    assert len(s["errors"]) == 3 and counts(wh) == before
    assert any("answer is for season '2025/2026'" in e for e in s["errors"])


def test_match_without_usable_details_waits_then_is_given_up(wh):
    recent, old = FIRST_EPL + 9 * 10, FIRST_EPL            # played yesterday / two months ago
    empty = lambda p: dict(p, content=dict(p["content"], playerStats={}))
    client = FakeFotMob(NOW, max_requests=20 + CURRENT)
    client.tweak[("match", recent)] = client.tweak[("match", old)] = empty
    lines = []
    s = pipeline.update(wh, client, now=NOW, log=lines.append)
    raw = {r["match_id"]: r["problem"] for r in wh.read("raw.match", ["match_id", "problem"])}
    assert recent not in raw and s["matches_waiting"] == 1            # asked again by the next run
    assert raw[old] == "no player stats" and any("not asked again" in x for x in lines)
    assert not wh.read("core.player_matches", None, {"match_id": old})
    results, _ = checks.run_checks(wh, NOW.date(), NOW, log=quiet)
    d1 = next(r for r in results if r["check_id"] == "D1")
    assert d1["row_count"] == 1 and "(unusable)" in d1["sample"]
    again = FakeFotMob(NOW, max_requests=N_LEAGUES + 1)
    pipeline.update(wh, again, now=NOW, log=quiet)
    assert ("match", recent) in again.calls and ("match", old) not in again.calls


def test_checks_pass_on_clean_data_and_catch_damage(loaded):
    wh, _, _ = loaded
    results, errors = checks.run_checks(wh, NOW.date(), NOW, log=quiet)
    assert errors == 0 and all(r["status"] == "ok" for r in results), results
    row = wh.read("core.team_matches", None, {"match_id": FIRST_EPL})[0]
    wh.replace("core.team_matches", [dict(row, points=row["points"] + 1)],
               {"match_id": FIRST_EPL, "team_id": row["team_id"]})
    wh.replace("core.team_matches", [], {"match_id": FIRST_EPL + 3})
    some_team = wh.read("core.shots", ["team_id"], {"match_id": FIRST_EPL + 1})[0]["team_id"]
    wh.replace("core.shots", [], {"match_id": FIRST_EPL + 1, "team_id": some_team})   # its shots are lost
    wh.replace("core.player_matches", [], {"match_id": FIRST_EPL + 2})
    results, errors = checks.run_checks(wh, NOW.date(), NOW, log=quiet)
    status = {r["check_id"]: (r["status"], r["row_count"]) for r in results}
    assert errors == 1 and status["M2"] == ("error", 1)
    assert status["S1"] == ("warn", 3)                      # 1 wrong points + 2 teams of the lost match
    assert status["D1"] == ("warn", 1) and status["D5"] == ("warn", 1)
    assert len(wh.read("core.quality_checks")) == 2 * len(results)


def test_sqlite_where_forms(tmp_path):
    w = SQLiteWarehouse(str(tmp_path / "w.db"))
    w.ensure()
    w.append("core.matches", [{"match_id": i, "season": 2020 + i % 3, "league": "X"} for i in range(1500)])
    assert len(w.read("core.matches", ["match_id"], {"match_id": list(range(1200))})) == 1200
    assert len(w.read("core.matches", None, {"season": (">=", 2021), "league": "X"})) == 1000
    assert w.read("core.matches", None, {"match_id": []}) == []
    w.replace("core.matches", [], {"match_id": list(range(1000))})
    assert len(w.read("core.matches")) == 500
    w.replace("core.matches", [{"match_id": -1}], {})
    assert [r["match_id"] for r in w.read("core.matches")] == [-1]
    with pytest.raises(ValueError):
        w.read("core.matches", None, {"season": ("LIKE", 1)})


def test_cli_daily_end_to_end(tmp_path, monkeypatch, capsys):
    from football import cli
    monkeypatch.setattr(cli, "Client", lambda **_: FakeFotMob(NOW))
    monkeypatch.setattr(pipeline, "utcnow", lambda: NOW)
    db, md = str(tmp_path / "cli.db"), str(tmp_path / "drafts.md")
    common = ["--backend", "sqlite", "--db", db, "--date", "2026-10-18"]
    cli.main(["daily", *common, "--md", md, "--top", "10"])
    w = SQLiteWarehouse(db)
    assert len(w.read("tweets.drafts")) == 10 and open(md, encoding="utf-8").read().count("###") == 10
    cli.main(["status", *common])
    out = capsys.readouterr().out
    epl = next(line.split() for line in out.splitlines() if line.startswith("Premier League  2026/27"))
    assert epl[3:10] == ["xG", "10", "90", "50", "50", "0", "0"] and epl[-1] == "0"   # ..., to load
    assert "match details still to download: 0" in out
    # a data-quality error stops the run before drafting
    w.replace("core.team_matches", [], {"match_id": FIRST_EPL})
    monkeypatch.setattr(cli, "cmd_update", lambda wh, args: {"errors": []})
    with pytest.raises(SystemExit, match="data-quality error"):
        cli.main(["daily", *common, "--date", "2026-10-19"])
    assert not w.read("tweets.drafts", None, {"run_date": "2026-10-19"})


def test_fotmob_down_stops_early_and_says_why(wh):
    client = FakeFotMob(NOW)
    client.fail_leagues.update((c, y) for c in config.LEAGUES for y in range(2023, 2027))
    lines = []
    s = pipeline.update(wh, client, now=NOW, log=lines.append)
    assert client.used == 5                                  # not 20: gave up after 5 in a row
    assert any("ERROR league Premier League 2026/27" in x and "HTTP 503" in x for x in lines)
    assert "stopped: FotMob is not answering" in s["errors"][-1]
    assert "HTTP 503" in wh.read("core.runs")[0]["errors"]


def test_non_json_answer_is_described_and_the_season_is_asked_for(monkeypatch):
    import io
    import urllib.request
    from football.fotmob import Client, FotMobError
    asked = []

    class Resp(io.BytesIO):
        status, headers = 200, {"Content-Type": "text/html"}
        __enter__ = lambda self: self
        __exit__ = lambda self, *a: None

    def urlopen(req, timeout):
        asked.append(req.full_url)
        return Resp(b"<html>\nJust a moment...</html>")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(FotMobError, match=r"not JSON - HTTP 200, text/html, 30 bytes, starts with: <html> Just a moment"):
        Client(delay=0, retries=2, sleep=lambda s: None).league("La_Liga", 2020)
    assert asked[0] == "https://www.fotmob.com/api/data/leagues?id=87&season=2020%2F2021"


def test_history_coverage_checks(loaded):
    wh, _, _ = loaded
    rows = {(r["league"], r["season"]): r for r in checks.coverage(wh, NOW.date())}
    epl = rows[("Premier League", 2025)]
    assert (epl["teams"], epl["expected"], epl["played"], epl["details"]) == (10, 90, 90, 90)
    assert epl["finished"] and epl["share_details"] == 1 and not rows[("Premier League", 2026)]["finished"]
    past = 2025 * 1000                                              # Premier League 2025/26 match ids
    # one match FotMob cannot give: 89/90 = 98.9% with details, accepted
    raw = wh.read("raw.match", None, {"match_id": past})[0]
    wh.replace("raw.match", [dict(raw, problem="no player stats")], {"match_id": past})
    for t in ("core.team_stats", "core.player_matches", "core.shots"):
        wh.replace(t, [], {"match_id": past})
    results, errors = checks.run_checks(wh, NOW.date(), NOW, log=quiet)
    assert errors == 0
    # a second one: 88/90 = 97.8% with details -> a hole, the run stops
    wh.replace("raw.match", [dict(raw, match_id=past + 1, problem="no player stats")], {"match_id": past + 1})
    results, errors = checks.run_checks(wh, NOW.date(), NOW, log=quiet)
    status = {r["check_id"]: (r["status"], r["row_count"]) for r in results}
    assert status["H2"] == ("error", 1) and errors == 1
    # a match not yet downloaded is a warning; a match missing from the league list an error
    wh.replace("raw.match", [], {"match_id": past + 1})
    wh.replace("core.matches", [], {"match_id": past + 2})
    results, errors = checks.run_checks(wh, NOW.date(), NOW, log=quiet)
    status = {r["check_id"]: (r["status"], r["row_count"]) for r in results}
    assert status["H1"] == ("error", 1) and status["H3"] == ("warn", 1)


def test_rebuild_remakes_core_tables_from_raw_without_downloading(loaded):
    wh, _, _ = loaded
    key = lambda r: (r["match_id"], r.get("player_id") or r.get("team_id") or r.get("shot_id"))
    before = {t: sorted(wh.read(f"core.{t}"), key=key)
              for t in ("player_matches", "team_stats", "shots", "matches", "fixtures")}
    # as after a new column: the history has NULLs in it
    old = wh.read("core.player_matches", None, {"season": 2024})
    wh.replace("core.player_matches", [dict(r, tackles=None) for r in old], {"season": 2024})
    wh.replace("core.team_stats", [], {"season": 2025})
    totals = pipeline.rebuild(wh, now=NOW, log=quiet)
    assert totals["matches"] == CURRENT + HISTORY and totals["leagues"] == N_LEAGUES * 4
    for t, rows in before.items():
        assert sorted(wh.read(f"core.{t}"), key=key) == rows, t


def test_model_match_new_player_and_keeper_columns():
    client = FakeFotMob(NOW)
    payload = client.league("EPL", 2026)
    m = model_league("Premier League", 2026, slim_league(payload), NOW)["matches"][0]
    rows = model_match(m, slim_match(client.match(m["match_id"])))
    players = rows["player_matches"]
    keepers = [p for p in players if p["saves"] is not None]
    assert len(keepers) == 2 and all(p["xgot_faced"] is not None for p in keepers)
    assert all(p["goals_conceded"] == (m["away_goals"] if p["home_away"] == "home" else m["home_goals"])
               for p in keepers)
    outfield = [p for p in players if p["saves"] is None]
    assert all(p[c] is not None for p in outfield for c in ("tackles", "dribbles", "big_chances_created"))
    assert sum(p["assists"] for p in players) > 0
    assert all(t["keeper_saves"] == 3 and t["big_chances_missed"] == 1 for t in rows["team_stats"])
