from collections import Counter
from datetime import datetime, timedelta

import pytest

from football import checks, config, pipeline
from football.model import model_league
from football.warehouse import SQLiteWarehouse
from conftest import NOW
from fake_understat import FakeUnderstat

quiet = lambda *_: None
N_LEAGUES, PER_ROUND, ROUNDS_PLAYED = 5, 5, 10


def counts(wh):
    return {t: len(wh.read(t)) for t in ("raw.league", "raw.match", "core.matches", "core.fixtures",
                                         "core.team_matches", "core.player_seasons",
                                         "core.player_matches", "core.shots")}


def test_model_league_links_team_rows_and_statuses():
    payload = FakeUnderstat(NOW).league("EPL", 2026)
    rows = model_league("Premier League", 2026, payload, NOW)
    assert len(rows["matches"]) == PER_ROUND * ROUNDS_PLAYED
    assert len(rows["matches"]) + len(rows["fixtures"]) == 90
    assert len(rows["team_matches"]) == 2 * len(rows["matches"])
    assert all(r["match_id"] is not None and r["opponent"] for r in rows["team_matches"])
    assert {f["status"] for f in rows["fixtures"]} == {"upcoming"}
    m = rows["matches"][0]
    sides = [r for r in rows["team_matches"] if r["match_id"] == m["match_id"]]
    home = next(r for r in sides if r["home_away"] == "home")
    assert (home["goals_for"], home["goals_against"]) == (m["home_goals"], m["away_goals"])
    assert home["team_id"] == m["home_team_id"] and home["ppda"] == 10.0
    # a match that kicked off 5 hours ago without a result is not "postponed"
    later = model_league("Premier League", 2026, payload, datetime(2026, 10, 24, 20, 0))
    assert Counter(f["status"] for f in later["fixtures"])["awaiting_result"] == PER_ROUND


def test_first_run_loads_history_and_current_details(loaded):
    wh, client, s = loaded
    c = counts(wh)
    assert c["raw.league"] == N_LEAGUES * 4                              # 2023..2026
    assert c["core.matches"] == N_LEAGUES * (3 * 90 + PER_ROUND * ROUNDS_PLAYED)
    assert c["raw.match"] == N_LEAGUES * PER_ROUND * ROUNDS_PLAYED       # current season only
    assert c["core.player_matches"] == c["raw.match"] * 24
    assert s["matches_new"] == c["raw.match"] and not s["errors"]
    seasons = {m["season"] for m in wh.read("core.player_matches", ["season"])}
    assert seasons == {2026}


def test_rerun_is_idempotent_and_cheap(loaded):
    wh, _, _ = loaded
    before = counts(wh)
    client = FakeUnderstat(NOW)
    s = pipeline.update(wh, client, now=NOW, log=quiet)
    assert counts(wh) == before
    assert client.used == N_LEAGUES                 # only the 5 current league files
    assert s["matches_new"] == 0


def test_next_day_checks_recent_matches_again_and_stores_only_changes(loaded):
    wh, _, _ = loaded
    before = counts(wh)
    tomorrow = NOW + timedelta(days=1)
    client = FakeUnderstat(tomorrow)
    changed = 0 * 1_000_000 + 2026 * 1000 + 9 * 10        # an EPL match of the last round

    def correct(payload):                                  # Understat fixes a shot's xG
        payload["shots"]["h"][0]["xG"] = "0.999"
        return payload
    client.tweak[("match", changed)] = correct
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
    s = pipeline.update(wh, FakeUnderstat(later), now=later, log=quiet)
    after = counts(wh)
    assert s["matches_new"] == N_LEAGUES * PER_ROUND
    assert after["core.matches"] - before["core.matches"] == N_LEAGUES * PER_ROUND
    assert before["core.fixtures"] - after["core.fixtures"] == N_LEAGUES * PER_ROUND
    assert after["core.matches"] + after["core.fixtures"] == before["core.matches"] + before["core.fixtures"]
    ids = [m["match_id"] for m in wh.read("core.matches", ["match_id"])]
    assert len(ids) == len(set(ids))


def test_request_cap_resumes_next_run(wh):
    first = FakeUnderstat(NOW, max_requests=60)
    pipeline.update(wh, first, now=NOW, log=quiet)
    assert first.used == 60 and len(wh.read("raw.match")) == 40          # 20 league files, 40 matches
    pipeline.update(wh, FakeUnderstat(NOW), now=NOW, log=quiet)
    assert len(wh.read("raw.match")) == N_LEAGUES * PER_ROUND * ROUNDS_PLAYED
    assert len(wh.read("core.runs")) == 2


def test_one_league_down_does_not_block_the_others(wh):
    client = FakeUnderstat(NOW)
    client.fail_leagues.add(("La_Liga", 2026))
    client.fail_matches.add(2026 * 1000)                   # first EPL match
    s = pipeline.update(wh, client, now=NOW, log=quiet)
    loaded = {r["league"] for r in wh.read("core.matches", ["league"], {"season": 2026})}
    assert loaded == set(config.LEAGUES.values()) - {"La Liga"}
    assert len(s["errors"]) == 2
    assert wh.read("core.runs")[0]["status"] == "errors"
    _, errors = checks.run_checks(wh, NOW.date(), NOW, log=quiet)
    assert errors == 1                                     # F1: La Liga not downloaded
    # next run: everything recovers
    pipeline.update(wh, FakeUnderstat(NOW), now=NOW, log=quiet)
    assert len(wh.read("raw.match")) == N_LEAGUES * PER_ROUND * ROUNDS_PLAYED


def test_broken_payload_never_replaces_good_data(loaded):
    wh, _, _ = loaded
    before = counts(wh)
    client = FakeUnderstat(NOW)
    client.tweak[("league", "EPL", 2026)] = lambda p: {"dates": [], "teams": {}, "players": []}
    client.tweak[("league", "Serie_A", 2026)] = lambda p: dict(p, dates=p["dates"][40:])   # lost results
    s = pipeline.update(wh, client, now=NOW, log=quiet)
    assert len(s["errors"]) == 2 and counts(wh) == before


def test_backfill_past_seasons_most_recent_first(loaded):
    wh, _, _ = loaded
    client = FakeUnderstat(NOW, max_requests=N_LEAGUES + 30)
    s = pipeline.update(wh, client, now=NOW, backfill_from=2024, leagues=["EPL"], log=quiet)
    assert s["matches_backfilled"] == 30 + N_LEAGUES - 1   # one league file only for EPL
    got = wh.read("core.player_matches", ["season", "date"], {"season": 2025})
    assert got and min(r["date"] for r in got) > "2025-10-15"


def test_checks_pass_on_clean_data_and_catch_damage(loaded):
    wh, _, _ = loaded
    results, errors = checks.run_checks(wh, NOW.date(), NOW, log=quiet)
    assert errors == 0 and all(r["status"] == "ok" for r in results), results
    mid = 2026 * 1000
    row = wh.read("core.team_matches", None, {"match_id": mid})[0]
    wh.replace("core.team_matches", [dict(row, goals_for=row["goals_for"] + 1)],
               {"match_id": mid, "team_id": row["team_id"]})
    wh.replace("core.shots", [], {"match_id": mid + 1})
    wh.replace("core.player_matches", [], {"match_id": mid + 2})
    results, errors = checks.run_checks(wh, NOW.date(), NOW, log=quiet)
    status = {r["check_id"]: (r["status"], r["row_count"]) for r in results}
    assert errors == 1 and status["M3"] == ("error", 1)
    assert status["D1"] == ("warn", 1)
    assert len(wh.read("core.quality_checks")) == 2 * len(results)


def test_import_local_archive(tmp_path, wh):
    import json
    client = FakeUnderstat(NOW)
    raw = tmp_path / "raw_understat"
    (raw / "league").mkdir(parents=True)
    (raw / "match").mkdir()
    payload = client.league("EPL", 2026)
    (raw / "league" / "EPL_2026.json").write_text(json.dumps(payload))
    for m in payload["dates"][:7]:
        (raw / "match" / f"{m['id']}.json").write_text(json.dumps(client.match(int(m["id"]))))
    s = pipeline.import_local(wh, str(raw), log=quiet)
    assert (s["leagues_loaded"], s["matches_new"]) == (1, 7)
    assert len(wh.read("core.player_matches")) == 7 * 24
    again = pipeline.import_local(wh, str(raw), log=quiet)
    assert again["matches_new"] == 0


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
    monkeypatch.setattr(cli, "Client", lambda **_: FakeUnderstat(NOW))
    monkeypatch.setattr(pipeline, "utcnow", lambda: NOW)
    db, md = str(tmp_path / "cli.db"), str(tmp_path / "drafts.md")
    common = ["--backend", "sqlite", "--db", db, "--date", "2026-10-18"]
    cli.main(["daily", *common, "--md", md, "--top", "10"])
    w = SQLiteWarehouse(db)
    assert len(w.read("tweets.drafts")) == 10 and open(md, encoding="utf-8").read().count("###") == 10
    cli.main(["status", *common])
    out = capsys.readouterr().out
    assert "Premier League  2026/27       50       50" in out and "update" in out
    # a data-quality error stops the run before drafting
    w.replace("core.team_matches", [], {"match_id": 2026 * 1000})
    monkeypatch.setattr(cli, "cmd_update", lambda wh, args: {"errors": []})
    with pytest.raises(SystemExit, match="data-quality error"):
        cli.main(["daily", *common, "--date", "2026-10-19"])
    assert not w.read("tweets.drafts", None, {"run_date": "2026-10-19"})


def test_understat_down_stops_early_and_says_why(wh):
    client = FakeUnderstat(NOW)
    client.fail_leagues.update((c, y) for c in config.LEAGUES for y in range(2023, 2027))
    lines = []
    s = pipeline.update(wh, client, now=NOW, log=lines.append)
    assert client.used == 5                                  # not 20: gave up after 5 in a row
    assert any("ERROR league Premier League 2026/27" in x and "HTTP 503" in x for x in lines)
    assert "stopped: Understat is not answering" in s["errors"][-1]
    assert "HTTP 503" in wh.read("core.runs")[0]["errors"]


def test_non_json_answer_is_described(monkeypatch):
    import io
    import urllib.request
    from football.understat import Client, UnderstatError

    class Resp(io.BytesIO):
        status, headers = 200, {"Content-Type": "text/html"}
        __enter__ = lambda self: self
        __exit__ = lambda self, *a: None
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: Resp(b"<html>\nJust a moment...</html>"))
    with pytest.raises(UnderstatError, match=r"not JSON - HTTP 200, text/html, 30 bytes, starts with: <html> Just a moment"):
        Client(delay=0, retries=2, sleep=lambda s: None).league("EPL", 2026)


def test_import_local_says_when_the_folder_is_wrong(tmp_path, wh):
    with pytest.raises(SystemExit, match="No league files in"):
        pipeline.import_local(wh, str(tmp_path / "nowhere"), log=quiet)
    assert wh.read("core.runs") == []
