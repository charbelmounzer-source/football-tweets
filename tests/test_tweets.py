import json
from datetime import date, timedelta

from football import tweets
from football.tweets import Data, find_facts, team_season_stats, tweet_length
from conftest import NOW

quiet = lambda *_: None
TODAY = NOW.date()
FIRST_EPL = 2026 * 1000


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


def test_every_pattern_finds_something_on_a_full_history(loaded):
    wh, _, _ = loaded
    d = Data(wh, None, TODAY)
    kinds = {f.kind for f in find_facts(d)}
    assert d.history_complete and all(d.complete.values())
    assert {"contrast", "streak", "start", "luck", "preview"} <= kinds


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


def test_season_xg_and_xpts_are_the_league_table_figures(loaded):
    wh, _, _ = loaded
    d = Data(wh, None, TODAY)
    stats, names = team_season_stats(d, "Premier League")
    table = {t["team_id"]: t for t in wh.read("core.standings", None, {"season": 2026, "league": "Premier League"})}
    for tid, s in stats.items():
        assert (s["xg"], s["xpts"], s["points"]) == (table[tid]["xg"], table[tid]["xpts"], table[tid]["points"])
        rows = [r for r in d.team_seq[("Premier League", tid)] if r["season"] == 2026]
        assert s["shots"] == sum(r["shots"] for r in rows) and 0 < s["conversion"] < 100
    # a table that is one match behind the results is not used; goals and points still are
    one = next(iter(table.values()))
    wh.replace("core.standings", [dict(one, played=one["played"] - 1)],
               {"season": 2026, "league": "Premier League", "team_id": one["team_id"]})
    stats, _ = team_season_stats(Data(wh, None, TODAY), "Premier League")
    assert "xg" not in stats[one["team_id"]] and stats[one["team_id"]]["points"] == one["points"]


def test_facts_that_need_every_match_are_off_while_details_are_missing(loaded):
    wh, _, _ = loaded
    for t in ("core.team_stats", "core.player_matches", "raw.match"):   # one EPL match not downloaded
        wh.replace(t, [], {"match_id": FIRST_EPL})
    d = Data(wh, None, TODAY)
    assert d.complete["Premier League"] is False and d.complete["La Liga"] is True
    assert not d.history_complete and d.history_missing == 1
    facts = find_facts(d)
    assert not [f for f in facts if f.kind == "overtake"]
    assert not [f for f in facts if f.fact_id.startswith("finishing|Premier League")]
    assert not [p for p in d.ps if p["league"] == "Premier League"]
    assert not [f for f in facts if f.kind == "contrast" and f.league == "Premier League"
                and "conversion" in f.fact_id]
    lines = []
    tweets.draft(wh, TODAY, log=lines.append)
    assert any("(overtake, pace) are off: 1 played matches since 2023/24" in x for x in lines)
    # a match played yesterday without details is normal, not a hole
    wh2_rows = wh.read("core.team_stats", None, {"match_id": FIRST_EPL + 90})   # last round
    wh.replace("raw.match", [], {"match_id": FIRST_EPL + 90})
    assert wh2_rows and Data(wh, None, TODAY).history_missing == 1


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
def _row(match, team_id, team, goals, pens=0, pid=999001, league=None):
    return {"match_id": match["match_id"], "league": league or match["league"], "season": match["season"],
            "date": match["date"], "kickoff_utc": match["kickoff_utc"], "player_id": pid,
            "player": "Joao Test", "team_id": team_id, "team": team, "minutes": 90, "goals": goals,
            "assists": 0, "shots": 3, "penalty_goals": pens, "npxg": 0.3}


def _add_striker(wh, old_season, old_goals, match_goals, old_pens=0):
    """A striker with 10 current-season matches for 'EPL Team 01' (team 1) and, before that,
    a season at 'Ligue_1 Team 03' (team 403) in which he scored old_goals."""
    played = lambda season, tid: sorted(wh.read("core.team_matches", None, {"season": season, "team_id": tid}),
                                        key=lambda r: r["kickoff_utc"])
    rows = [_row(m, 1, "EPL Team 01", g) for m, g in zip(played(2026, 1), match_goals)]
    old = played(old_season, 403)
    rows += [_row(m, 403, "Ligue_1 Team 03", 1 if i < old_goals else 0, pens=1 if i < old_pens else 0)
             for i, m in enumerate(old)]
    wh.append("core.player_matches", rows)


def test_overtake_exact_tallies(loaded):
    wh, _, _ = loaded
    _add_striker(wh, 2024, 6, [1, 0, 2, 0, 1, 0, 1, 0, 0, 1], old_pens=1)   # 6 now, the last one yesterday
    facts = [f for f in find_facts(Data(wh, None, TODAY)) if f.kind == "overtake"]
    assert len(facts) == 1
    f = facts[0]
    assert "matched his top-flight league tally for Ligue_1 Team 03 (6)" in f.text
    assert "yet to score a penalty" in f.text and "6 G/A in 10 games for EPL Team 01" in f.text
    assert f.evidence["goals_for_club"] == 6 and f.evidence["before_match"] == 5


def _make_unusable(wh, match_id):
    """As if FotMob had no details for this match: raw row with a problem, no detail rows."""
    raw = wh.read("raw.match", None, {"match_id": match_id})[0]
    for t in ("core.team_stats", "core.player_matches", "core.shots"):
        wh.replace(t, [], {"match_id": match_id})
    wh.replace("raw.match", [dict(raw, problem="no player stats")], {"match_id": match_id})


def test_overtake_skipped_while_a_match_of_the_history_is_not_downloaded(loaded):
    wh, _, _ = loaded
    _add_striker(wh, 2024, 6, [1, 0, 2, 0, 1, 0, 1, 0, 0, 1])
    wh.replace("raw.match", [], {"match_id": 3 * 1_000_000 + 2024 * 1000})   # a 2024/25 Bundesliga match
    d = Data(wh, None, TODAY)
    assert not [f for f in find_facts(d) if f.kind == "overtake"] and d.unreliable >= 1


def test_a_match_fotmob_cannot_give_only_blocks_the_two_teams_of_that_season(loaded):
    wh, _, _ = loaded
    _add_striker(wh, 2024, 6, [1, 0, 2, 0, 1, 0, 1, 0, 0, 1])
    _make_unusable(wh, 3 * 1_000_000 + 2024 * 1000)               # Bundesliga: nothing to do with him
    d = Data(wh, None, TODAY)
    assert d.history_complete and d.holes
    assert [f for f in find_facts(d) if f.kind == "overtake"]
    # a match of his old club in the season he played there: his tally there may be short
    old_club = next(r["match_id"] for r in wh.read("core.team_matches", None, {"season": 2024, "team_id": 403}))
    _make_unusable(wh, old_club)
    assert not [f for f in find_facts(Data(wh, None, TODAY)) if f.kind == "overtake"]


def test_overtake_skipped_when_player_was_at_old_club_before_our_data(loaded):
    wh, _, _ = loaded
    # first season at the old club = first season of the data: earlier goals are unknown
    _add_striker(wh, 2023, 6, [1, 0, 2, 0, 1, 0, 1, 0, 0, 1])
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


# ---------------------------------------------------------------- new patterns (13 Nov 2026)
def test_new_player_patterns_find_something_and_numbers_are_true(loaded):
    wh, _, _ = loaded
    d = Data(wh, None, TODAY)
    facts = find_facts(d)
    kinds = {f.kind for f in facts}
    assert {"player_streak", "pace", "performance", "leader"} <= kinds
    rows_of = {}
    for p in d.pm:
        rows_of.setdefault((p["league"], p["player_id"]), []).append(p)
    for f in facts:
        if f.kind == "player_streak":
            pid = int(f.fact_id.split("|")[3])
            rows = rows_of[(f.league, pid)]
            n = f.evidence["appearances"]
            test = (lambda p: (p["goals"] or 0) > 0) if f.evidence["run"] == "scoring" else \
                (lambda p: (p["goals"] or 0) + (p["assists"] or 0) > 0)
            assert all(test(p) for p in rows[-n:])
            assert len(rows) == n or not test(rows[-n - 1])          # the run starts exactly there
            assert f.evidence["goals"] == sum(p["goals"] for p in rows[-n:])
        if f.kind == "pace":
            pid = int(f.fact_id.split("|")[3])
            col = f.evidence["metric"]
            total = sum((p["goals"] if col in ("goals", "involvements") else 0)
                        + (p["assists"] if col in ("assists", "involvements") else 0)
                        for p in rows_of[(f.league, pid)])
            assert total == f.evidence["value"]
            named = int(f.evidence["last_as_many"].split(" in ")[0].split()[-1])
            assert named >= f.evidence["value"] > f.evidence["best_since_then"]
        if f.kind == "performance" and f.fact_id.startswith("hat_trick"):
            assert f.evidence["goals"] >= 3


def test_punch_lines_fit_the_stat(loaded):
    wh, _, _ = loaded
    for f in find_facts(Data(wh, None, TODAY)):
        if "Wasteful" in f.text or "clinical" in f.text.lower() and "Need to be" in f.text:
            assert f.kind in ("contrast", "leader") and ("conversion" in f.fact_id or "goals" in f.fact_id
                                                         or "big_chances_missed" in f.fact_id)
        if f.kind == "streak" and "run" in f.evidence:
            assert any(line in f.text for line in tweets.PUNCH[f.evidence["run"]])


def test_no_pattern_fills_the_drafts(loaded):
    wh, _, _ = loaded
    chosen = tweets.draft(wh, TODAY, top=20, log=quiet)
    per_kind = {}
    for f in chosen:
        per_kind[f.kind] = per_kind.get(f.kind, 0) + 1
    assert max(per_kind.values()) <= 6 and len(per_kind) >= 4


def test_leaders_wait_for_new_columns_to_be_loaded(loaded):
    wh, _, _ = loaded
    leader = lambda: [f for f in find_facts(Data(wh, None, TODAY)) if f.kind == "leader"
                      and f.league == "La Liga" and f.evidence["stat"] == "big_chances_created"]
    assert leader()
    first_la_liga = 1_000_000 + 2026 * 1000
    rows = wh.read("core.player_matches", None, {"match_id": first_la_liga})
    wh.replace("core.player_matches", [dict(r, big_chances_created=None) for r in rows],
               {"match_id": first_la_liga})                  # as before `rebuild` fills the column
    assert not leader()


# ---------------------------------------------------------------- "since" claims (13 Nov 2026)
def test_last_reached():
    entries = [(7, 2023, "a"), (9, 2024, "b"), (8, 2025, "c"), (5, 2026, "d")]
    assert tweets.last_reached(entries, 8, True) == (8, 2025, "c")
    assert tweets.last_reached(entries, 9, True) == (9, 2024, "b")
    assert tweets.last_reached(entries, 10, True) is None          # before our data: unknown
    assert tweets.last_reached(entries, 6, False) == (5, 2026, "d")


def test_since_claims_name_a_season_when_it_really_happened(loaded):
    """No tweet says "since <first season>" only because the data starts there: every
    "since S" names a season in which the same level was reached, and nothing since."""
    import re
    wh, _, _ = loaded
    d = Data(wh, None, TODAY)
    facts = find_facts(d)
    first = tweets.season_label(min(d.first_season.values()))
    for f in facts:
        assert "in our data" not in f.text and "top five leagues since" not in f.text
        for m in re.finditer(r"since (\S.*?) in (\d{4}/\d{2})", f.text):
            assert f.kind in ("streak", "start", "pace"), f.text
        if f.kind == "streak" and "run" in f.evidence and "since" in f.text:
            test, length = tweets.RUNS[f.evidence["run"]][0], f.evidence["length"]
            season = re.search(r"since .*?(\d{4})/\d{2}", f.text).group(1)
            own_only = "their longest since" in f.text
            runs = [(n, s) for (lg, t), rows in d.team_seq.items() if lg == f.league
                    and (t == f.team_id or not own_only)
                    for n, s in tweets.runs_of(rows if t != f.team_id else rows[:-length], test)]
            assert any(n >= length and s == int(season) for n, s in runs), f.text
            assert not any(n >= length and s > int(season) for n, s in runs), f.text
        if f.kind == "start":
            ref_season = int(f.evidence["last_as_good"].split(" in ")[1][:4])
            assert ref_season <= d.season - 3
    assert any("since" in f.text for f in facts if f.kind in ("streak", "start"))


def test_a_record_beyond_the_data_is_not_claimed(loaded):
    wh, _, _ = loaded
    # a striker with far more goals than anyone in the data at this stage: the last time
    # anyone did it is before 2023/24 (or never) - no "most since" tweet
    team = sorted(wh.read("core.team_matches", None, {"season": 2026, "team_id": 1}),
                  key=lambda r: r["kickoff_utc"])
    wh.append("core.player_matches", [_row(m, 1, "EPL Team 01", 5, pid=999777) for m in team])
    facts = find_facts(Data(wh, None, TODAY))
    assert not [f for f in facts if f.kind == "pace" and f.entity == "Joao Test"]
