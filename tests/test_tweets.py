import json
from datetime import date, datetime, timedelta

from football import pipeline, tweets
from football.tweets import Data, find_facts, tweet_length
from conftest import NOW
from fake_understat import FakeUnderstat

quiet = lambda *_: None
TODAY = NOW.date()


def test_tweet_length_counts_emoji_twice():
    assert tweet_length("Arsenal have 21 points.") == 23
    assert tweet_length("🔥") == 2 and tweet_length("é–") == 2
    assert tweet_length("a" * 279 + "🔥") == 281


def test_drafts_are_stored_valid_and_not_repeated(loaded, tmp_path):
    wh, _, _ = loaded
    md = tmp_path / "drafts.md"
    chosen = tweets.draft(wh, TODAY, top=25, md_path=str(md), log=quiet)
    rows = wh.read("tweets.drafts")
    assert chosen and len(rows) == len(chosen)
    assert all(r["length"] <= 280 and r["length"] == tweet_length(r["text"]) for r in rows)
    assert all(r["status"] == "draft" and json.loads(r["evidence"]) for r in rows)
    assert len({r["league"] for r in rows}) > 1                 # all five leagues by default
    assert "Tweet drafts" in md.read_text(encoding="utf-8")
    per_entity = {}
    for r in rows:
        per_entity[r["entity"]] = per_entity.get(r["entity"], 0) + 1
    assert max(per_entity.values()) <= 2

    # same day again: same drafts, replaced not duplicated
    again = tweets.draft(wh, TODAY, top=25, log=quiet)
    assert [f.fact_id for f in again] == [f.fact_id for f in chosen]
    assert len(wh.read("tweets.drafts")) == len(chosen)

    # next day, no new match: nothing drafted yesterday comes back
    nxt = tweets.draft(wh, TODAY + timedelta(days=1), top=25, log=quiet)
    assert not {f.fact_id for f in nxt} & {f.fact_id for f in chosen}


def test_every_number_in_a_streak_fact_is_true(loaded):
    wh, _, _ = loaded
    d = Data(wh, None, TODAY)
    facts = [f for f in find_facts(d) if f.kind == "streak" and "run" in f.evidence]
    assert facts
    for f in facts:
        rows = d.team_seq[(f.league, f.team_id)]
        test = tweets.RUNS[f.evidence["run"]][0]
        n = f.evidence["length"]
        assert all(test(r) for r in rows[-n:]) and (len(rows) == n or not test(rows[-n - 1]))
        assert f"{n}-match" in f.text


def test_no_matchday_facts_in_the_summer_break(loaded):
    wh, _, _ = loaded
    d = Data(wh, None, date(2026, 11, 20))          # a month after the last result loaded
    assert all(not ids for ids in d.recent_matches.values())
    assert not [f for f in find_facts(d) if f.kind in ("streak", "start", "overtake")]


def test_date_in_the_past_does_not_see_the_future(loaded):
    wh, _, _ = loaded
    d = Data(wh, None, date(2026, 9, 20))
    assert max(r["date"] for r in d.tm) <= "2026-09-20"
    assert max(p["date"] for p in d.pm) <= "2026-09-20"


# ---------------------------------------------------------------- overtake, on hand-made rows
def _season(pid, season, team, goals, games, league="Premier League", pens=0):
    return {"player_id": pid, "player": "Joao Test", "league": league, "season": season,
            "teams": team, "goals": goals, "assists": 2, "penalty_goals": pens, "games": games}


def _add_striker(wh, seasons, match_goals):
    """A striker with past seasons and current-season match rows for 'EPL Team 01' (team 1)."""
    wh.append("core.player_seasons", seasons)
    played = sorted((m for m in wh.read("core.team_matches", None, {"season": 2026, "team_id": 1})),
                    key=lambda r: r["kickoff_utc"])
    rows = [{"match_id": m["match_id"], "league": "Premier League", "season": 2026, "date": m["date"],
             "kickoff_utc": m["kickoff_utc"], "player_id": 999001, "player": "Joao Test",
             "team_id": 1, "team": "EPL Team 01", "goals": g, "assists": 0, "shots": 3,
             "penalty_goals": 0} for m, g in zip(played, match_goals)]
    wh.append("core.player_matches", rows)


def test_overtake_exact_tallies(loaded):
    wh, _, _ = loaded
    goals = [1, 0, 2, 0, 1, 0, 1, 0, 0, 1]                       # 6 this season, the last one yesterday
    _add_striker(wh, [_season(999001, 2024, "Old Club", 3, 30, "Ligue 1", pens=1),
                      _season(999001, 2025, "Old Club", 3, 31, "Ligue 1"),
                      _season(999001, 2026, "EPL Team 01", 6, 10)], goals)
    wh.append("core.team_matches", [{"match_id": -5, "league": "Ligue 1", "season": 2023, "date": "2023-08-20",
                                     "kickoff_utc": "2023-08-20 15:00:00", "team_id": 9, "team": "Old Club"}])
    facts = [f for f in find_facts(Data(wh, None, TODAY)) if f.kind == "overtake"]
    assert len(facts) == 1
    f = facts[0]
    assert "matched his top-flight league tally for Old Club (6)" in f.text
    assert "yet to score a penalty" in f.text and "8 G/A in 10 games" not in f.text   # 6 goals + 0 assists
    assert f.evidence["goals_for_club"] == 6 and f.evidence["before_match"] == 5


def test_overtake_skipped_when_history_does_not_add_up(loaded):
    wh, _, _ = loaded
    # Understat says 7 goals this season but the match rows only show 6: a match is missing
    _add_striker(wh, [_season(999001, 2025, "Old Club", 6, 31, "Ligue 1"),
                      _season(999001, 2026, "EPL Team 01", 7, 10)], [1, 0, 2, 0, 1, 0, 1, 0, 0, 1])
    d = Data(wh, None, TODAY)
    assert not [f for f in find_facts(d) if f.kind == "overtake"] and d.unreliable == 1


def test_overtake_skipped_for_a_mid_season_move_without_match_rows(loaded):
    wh, _, _ = loaded
    # 2025/26 was split between two clubs and its match details are not loaded
    _add_striker(wh, [_season(999001, 2025, "Old Club,EPL Team 01", 6, 31),
                      _season(999001, 2026, "EPL Team 01", 6, 10)], [1, 0, 2, 0, 1, 0, 1, 0, 0, 1])
    d = Data(wh, None, TODAY)
    assert not [f for f in find_facts(d) if f.kind == "overtake"] and d.unreliable == 1


def test_overtake_skipped_when_player_was_at_old_club_before_our_data(loaded):
    wh, _, _ = loaded
    # first season with Old Club = first season of the data: earlier goals are unknown
    _add_striker(wh, [_season(999001, 2023, "EPL Team 05", 6, 31),
                      _season(999001, 2026, "EPL Team 01", 6, 10)], [1, 0, 2, 0, 1, 0, 1, 0, 0, 1])
    assert not [f for f in find_facts(Data(wh, None, TODAY)) if f.kind == "overtake"]


def test_league_articles_and_runs_older_than_the_data(loaded):
    wh, _, _ = loaded
    assert tweets.the("Premier League") == "the Premier League" and tweets.the("La Liga") == "La Liga"
    d = Data(wh, None, TODAY)
    facts = find_facts(d)
    assert not [f for f in facts if "in the La Liga" in f.text or "in the Serie A" in f.text
                or "in the Ligue 1" in f.text]
    for f in facts:
        if f.kind == "streak" and "run" in f.evidence:
            assert f.evidence["length"] < len(d.team_seq[(f.league, f.team_id)])
