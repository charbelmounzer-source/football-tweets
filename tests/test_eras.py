"""FotMob's eras (13 Nov 2026): what an older season does not have is NULL (unknown), never 0,
and no check or tweet treats it as known. In the simulated data: 2023/24 lineups and events
only, 2024/25 player stats without shots or xG, 2025/26 on everything."""

from football import checks, config, tweets
from football.model import model_league, model_match, slim_league, slim_match, validate_match
from football.tweets import Data, find_facts
from conftest import NOW
from fake_fotmob import FakeFotMob

quiet = lambda *_: None
TODAY = NOW.date()


def _match(client, code, season, i=0):
    payload = client.league(code, season)
    m = model_league(config.LEAGUES[code], season, slim_league(payload), NOW)["matches"][i]
    return m, slim_match(client.match(m["match_id"]))


def test_old_match_lineups_and_name_matched_goals(monkeypatch):
    monkeypatch.setattr(config, "PLAYER_STATS_FROM", 2024)
    monkeypatch.setattr(config, "XG_FROM", 2025)
    client = FakeFotMob(NOW)
    m, slim = _match(client, "EPL", 2023)
    assert validate_match(slim, m) is None
    rows = model_match(m, slim)
    players = rows["player_matches"]
    assert len(players) == 24                                    # 11 + 1 sub who came on, per side
    assert all(p["minutes"] is None and p["assists"] is None and p["xg"] is None
               and p["rating"] is None and p["tackles"] is None for p in players)
    by_team = {}
    for p in players:
        by_team[p["team_id"]] = by_team.get(p["team_id"], 0) + p["goals"]
    assert by_team == {m["home_team_id"]: m["home_goals"], m["away_team_id"]: m["away_goals"]}
    assert sum(p["yellow"] for p in players) == 2
    assert all(t["xg"] is None and t["shots"] is None for t in rows["team_stats"]) and not rows["shots"]


def test_old_match_goals_unknown_when_a_scorer_name_cannot_be_matched(monkeypatch):
    monkeypatch.setattr(config, "PLAYER_STATS_FROM", 2024)
    client = FakeFotMob(NOW)
    payload = client.league("EPL", 2023)
    ms = model_league("Premier League", 2023, slim_league(payload), NOW)["matches"]
    m = next(x for x in ms if x["home_goals"] + x["away_goals"] > 0)
    raw = client.match(m["match_id"])
    for e in raw["content"]["matchFacts"]["events"]["events"]:
        if e["type"] == "Goal":
            e["nameStr"] = "Somebody Else"
            break
    players = model_match(m, slim_match(raw))["player_matches"]
    assert players and all(p["goals"] is None for p in players)  # unknown, not 0


def test_old_match_without_lineup_is_unusable_and_new_ones_need_player_stats(monkeypatch):
    monkeypatch.setattr(config, "PLAYER_STATS_FROM", 2024)
    client = FakeFotMob(NOW)
    m, slim = _match(client, "EPL", 2023)
    for side in ("home", "away"):
        slim["lineup"][side]["starters"] = []
    assert validate_match(slim, m) == "no lineup"
    m2, slim2 = _match(client, "EPL", 2025)
    slim2["players"] = []
    assert validate_match(slim2, m2) == "no player stats"


def test_middle_era_has_player_stats_but_no_shot_data(eras):
    wh, _, _ = eras
    rows = wh.read("core.player_matches", None, {"season": 2024})
    assert rows and all(r["goals"] is not None and r["assists"] is not None and r["minutes"] is not None
                        for r in rows)
    # no shot data: xG and penalty goals unknown; a shot count FotMob leaves out (zero?) is unknown too
    assert all(r["xg"] is None and r["npxg"] is None and r["penalty_goals"] is None for r in rows)
    assert any(r["shots"] is None for r in rows) and not any(r["shots"] == 0 for r in rows)
    assert not wh.read("core.shots", None, {"season": 2024})
    assert all(t["xg"] is None for t in wh.read("core.team_stats", None, {"season": 2024}))
    assert all(t["xg"] is not None for t in wh.read("core.team_stats", None, {"season": 2025}))


def test_checks_know_the_eras(eras):
    wh, _, _ = eras
    results, errors = checks.run_checks(wh, TODAY, NOW, log=quiet)
    assert errors == 0, [r for r in results if r["status"] == "error"]
    status = {r["check_id"]: r["status"] for r in results}
    assert status["H4"] == "ok" and status["H5"] == "ok"           # no xG before 2025: not a warning
    cov = {(r["league"], r["season"]): r for r in checks.coverage(wh, TODAY)}
    assert [cov[("Premier League", y)]["era"] for y in (2023, 2024, 2025)] == ["lineups", "players", "xG"]


def test_a_short_season_is_not_a_hole(eras, monkeypatch):
    wh, _, _ = eras
    gone = [m["match_id"] for m in wh.read("core.matches", None, {"season": 2024, "league": "Serie A"})][:20]
    wh.replace("core.matches", [], {"match_id": gone})
    status = lambda: {r["check_id"]: r["status"] for r in checks.run_checks(wh, TODAY, NOW, log=quiet)[0]}
    assert status()["H2"] == "error"
    monkeypatch.setattr(config, "SHORT_SEASONS", {("Serie A", 2024): "stopped early"})
    assert status()["H2"] == "ok"


def test_tweets_never_use_unknown_older_data(eras):
    wh, _, _ = eras
    d = Data(wh, None, TODAY)
    assert d.history_complete
    facts = find_facts(d)
    assert facts
    for f in facts:
        if f.kind == "pace":
            # assists and involvements are unknown in 2023/24: a claim may only name a later season
            season = int(f.evidence["last_as_many"].split(" in ")[-1][:4])
            if f.evidence["metric"] != "goals":
                assert season >= 2024, f.text
        if f.kind == "start" and f.evidence["stat"] == "xg":
            assert int(f.evidence["last_as_good"].split(" in ")[1][:4]) >= 2025
        if f.kind == "overtake":
            assert "G/A" not in f.text or f.evidence["assists_for_club"] is not None
    # results go back to the first season for every team fact
    assert d.first_season["Premier League"] == 2023


def test_pace_treats_an_unknown_season_as_maybe_reached(eras, monkeypatch):
    """With assists unknown in 2023/24, "the most assists since <season>" can only be said
    when a later season holds the reference; a 2023/24 player may have had more."""
    wh, _, _ = eras
    d = Data(wh, None, TODAY)
    entries = [(float("inf"), 2023, (None, 1)), (5, 2024, (5, 2))]
    ref = tweets.last_reached(entries, 6, True)
    assert ref[1] == 2023 and ref[2][0] is None                   # -> no claim
    ref = tweets.last_reached(entries, 5, True)
    assert ref[1] == 2024 and ref[2][0] == 5                       # -> "since X had 5 in 2024/25"
