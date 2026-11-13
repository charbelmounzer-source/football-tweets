# Football tweets: top 5 leagues, fresh every day

FotMob data for the Premier League, La Liga, Serie A, Bundesliga and Ligue 1, from 2020/21,
loaded into BigQuery every day, checked, and turned into tweet drafts.

```
FotMob ──► football_raw      the answers, reduced to what is used
       └─► football_core     matches, fixtures, team_matches, standings,
                             team_stats, player_matches, shots  + quality_checks, runs
                 └─► football_tweets.drafts     today's drafts (also the "already said" history)
```

One source, no local files: all state is in BigQuery, so the job runs from anywhere and picks
up where it stopped.

## What runs every day

`python -m football daily` (GitHub Actions, 04:23 UTC — `.github/workflows/daily.yml`):

1. **Update**, within a cap of 2,000 requests, 1.5 s apart:
   - the current season's league data for the 5 leagues (results, fixtures, league table);
     past seasons back to 2020/21 once;
   - match details (team stats, players, shots) for every played match of this season not
     loaded yet;
   - match details again for matches of the last 10 days, stored only if FotMob changed them;
   - match details of past seasons, most recent first, until the history is complete.
2. **Checks** — 14 data-quality checks (coverage of every season, details of the current one), stored in
   `football_core.quality_checks`. An *error* stops the run before any tweet is drafted.
3. **Tweet drafts** — the best 25 facts across the 5 leagues go to `football_tweets.drafts`
   and to the run's summary page on GitHub.

Once the history is loaded a day costs about 100 requests (3 minutes) in season. Only what
changed is rewritten: one league-season, or the matches touched. A failed request is retried
(15 s, 30 s, 60 s); a league that stays down is skipped and reported, the others load. An
answer that is empty, for another season, or has lost results is refused and the previous
data stays.

**The first load is long.** About 11,200 matches (500 this season, 10,700 from 2020/21 to
2025/26), one request each: six runs of 2,000 requests, about an hour each. The daily
schedule does it by itself in six days; running the workflow by hand a few times does it
sooner. `python -m football status` shows what is left. Until the history is complete,
tweets that need a player's full history (`overtake`) are switched off; everything else works
from the first run.

## Set up (once)

1. **Billing on the BigQuery project.** The pipeline uses `DELETE`/`INSERT`, which the
   BigQuery sandbox refuses, and sandbox tables expire after 60 days. With billing enabled
   this volume (about 1 GB) stays inside the free tier (10 GiB storage, 1 TiB queries a month).
2. **Service account** with roles *BigQuery Data Editor* and *BigQuery Job User*; create a
   JSON key and save it as the repository secret `GCP_SA_KEY`
   (Settings → Secrets and variables → Actions).
3. **Run it**: Actions tab → *Daily update* → *Run workflow*. Or from a laptop:
   ```bash
   python -m pip install -r requirements.txt
   gcloud auth application-default login
   python -m football daily --md drafts.md
   python -m football status
   ```

The project id is read from `$BQ_PROJECT` (repository variable `BQ_PROJECT`), default in
`football/config.py`. Tables of the previous (Understat) version that are empty are replaced
by the new ones automatically; a table that has rows is never dropped.

## Commands

```bash
python -m football daily                      # update + checks + drafts
python -m football update                     # download and load only
python -m football update --max-requests 500  # a smaller slice
python -m football check                      # checks only (exit code 1 on errors)
python -m football tweets --top 25 --md drafts.md
python -m football tweets --date 2026-10-20   # as if today were that day
python -m football status                     # coverage per league-season, what is left
python -m football rebuild                    # re-make core tables from raw.* (no download)
```

`rebuild` is needed once after a new column is added to `player_matches` or `team_stats`:
the stored FotMob answers already hold the value, so the history is filled without asking
FotMob again (about 10 minutes on BigQuery). On GitHub: *Daily update* → *Run workflow* →
tick **rebuild**.

Add `--backend sqlite --db try.db` to any command to work in a local file instead of BigQuery.
`--leagues EPL,La_Liga` limits a run to some leagues (codes: `EPL`, `La_Liga`, `Serie_A`,
`Bundesliga`, `Ligue_1`). The first season is `FIRST_SEASON` in `football/config.py`.

## Tables (`football_core`)

Seasons are stored by start year (`2026` = 2026/27); `season_label` gives `2026/27`. Ids
(match, team, player, shot) are FotMob's. Times are UTC.

| Table | One row per | Main columns | Comes from |
|---|---|---|---|
| `matches` | played match | league, season, date, kickoff_utc, round, home/away team, goals, result (H/D/A) | league data |
| `fixtures` | match without a result | same, plus status: `upcoming`, `awaiting_result` (kicked off < 48 h ago), `postponed`, `cancelled` (abandoned) | league data |
| `team_matches` | team × played match | home_away, opponent, goals_for/against, result (W/D/L), points | league data |
| `standings` | team × league-season | position, played, wins, draws, losses, goals, points, and FotMob's season **xg, xga, xpts** | league data |
| `team_stats` | team × match | xg, xga, npxg, npxga, xgot, shots, shots_on_target, shot_goals, big_chances, big_chances_missed, xg_open_play, xg_set_play, possession, corners, touches_in_box, keeper_saves | match details |
| `player_matches` | player × match he played in | team, started, minutes, goals, penalty_goals, own_goals, assists, shots, shots_on_target, xg, npxg, xgot, xa, key_passes (chances created), big_chances_created, big_chances_missed, touches, touches_in_box, dribbles, tackles, interceptions, recoveries, duels_won, aerials_won, passes_final_third, yellow, red, rating; goalkeepers also saves, goals_conceded, xgot_faced, goals_prevented (NULL for outfield players) | match details |
| `shots` | shot | minute, period, player, team, result (`Goal`, `AttemptSaved`, `Miss`, `Post`), on_target, blocked, own_goal, xg, xgot, x, y, inside_box, situation, shot_type | match details |
| `quality_checks` | check × run | level, row_count, status, sample — view `latest_checks` = last run | |
| `runs` | pipeline run | requests, matches new / corrected / of past seasons, seconds, errors | |

Views: `teams`, `players`, `player_seasons` (season totals added up from `player_matches`),
`latest_checks`.

`football_raw.league` and `football_raw.match` keep the latest answer for each league-season
and match, reduced to the parts that are used (a match goes from about 400 kB to 45 kB), so
any table can be rebuilt without downloading. `football_tweets.drafts`: run_date, rank, score,
type, league, entity, text, length, evidence (JSON), fact_id, status (`draft` now; `posted`,
`tweet_id`, `posted_at` are there for auto-posting).

Things to know about the data:
- **xG is Opta's model** (FotMob's supplier). Shot positions `x`, `y` appear to be in metres on a
  105 × 68 pitch, `x` towards the goal attacked (FotMob does not document them).
- **Results are complete from the first run; xG and players arrive match by match.** A match
  has `team_stats`, `player_matches` and `shots` rows only once its details are loaded.
- **Own goals** are in `shots` with `own_goal = 1` and the team of the player who scored it;
  they are not counted in his `goals` or in `shot_goals`. Goals follow the shot data: now and
  then FotMob's match page credits an attacker where the shot data records an own goal.
- A team's xG can be lower than the sum of its shots' xG (several shots of one attack).
- FotMob leaves a player stat out when it is zero; the pipeline stores 0 for shots, xG, xA
  and key passes when the match has shot data.
- No PPDA, no "deep" passes, no pre-match forecast, no per-match expected points: FotMob does
  not publish them. Expected points exist per season (`standings.xpts`).

## Checks

| Id | Level | Catches |
|---|---|---|
| F1 | error | a league not downloaded in the last 36 hours (warning in July–August) |
| M1 | error | a match id present twice, or in both `matches` and `fixtures` |
| M2 | error | a played match without exactly 2 team rows |
| S1 | warn | FotMob's league table disagrees with the results (played, points) |
| H1 | error | a league-season (any season) lists fewer matches than teams × (teams − 1) |
| H2 | error | a finished season with less than 98% of its matches played, or less than 98% of them with details |
| H3 | warn | a played match (2+ days old) whose details were never downloaded |
| H4 | warn | a league-season with more than 2% of detailed matches without xG |
| D1 | warn | a match played 2+ days ago without player details |
| D2 | warn | score ≠ players' goals + own goals |
| D3 | warn | a player's goals ≠ his goals in the shot data |
| D4 | warn | a team without exactly 11 starters |
| D5 | warn | a team's xG is higher than the sum of its shots' xG by more than 0.3 (shots missing) |
| X1 | warn | postponed or cancelled fixtures |

The H checks cover every season loaded; the others the current one. Each run also prints one
line: league-seasons, played matches, how many have details. A match FotMob has no details
for (two since 2020/21 on 13 Nov 2026) is accepted: below 2% of a season it is not a hole.
`python -m football status` shows the same per league-season (teams, expected, played,
details, unusable, no xG, coverage).

## Tweet drafts

Ten fact patterns, each run over every team and player:

| Pattern | About | Example |
|---|---|---|
| `contrast` | team | top 3 for one stat, bottom 5 for its partner (shots vs conversion, xG vs goals...) |
| `streak` | team | droughts that just ended; unbeaten / winning / winless / losing / scoring runs |
| `start` | team | best or worst start after N matches since a season 3+ years ago |
| `luck` | team, player | goals vs xG, points vs xPts, shot-stopping (conceded vs xGOT against), finishing |
| `preview` | fixture | head-to-head runs before next week's matches |
| `overtake` | player | a scorer passes his league tally for a previous club |
| `player_streak` | player | scored (or scored/assisted) in N appearances in a row |
| `pace` | player | most goals / assists / goal involvements after N team matches since someone last had as many |
| `performance` | player | last matchday: hat-tricks, 3+ assists, goalkeeping (goals prevented on xGOT) |
| `leader` | player, team | season leader with a clear gap (25% and 3 ahead): chances created, big chances, xA, dribbles, tackles, interceptions, goals prevented; teams: big chances missed, set-piece goals |

Score = rarity × context (1.5 played on the last matchday, 1.3 plays next) × club popularity
(`config/clubs.csv`: club as FotMob spells it, hashtag, popularity 1–3). At most 2 drafts per
team or player, at most 30% of the drafts from one pattern; a fact drafted in the last 14
days is not drafted again; drafts over 280
characters as X counts them (emoji = 2) are dropped.

Rules that keep the numbers true:
- **A "since" claim names a season when it really happened.** "The most since X had 9 in
  2022/23", "their best start since 2021/22 (20 then)", "their longest since 2023/24": the
  named season is the most recent one in the data in which the same level was reached, and
  nobody has reached it since. If nobody in the data reached it, the last time is before
  2020/21 (or never) and is unknown: no claim is made. A tweet never says "since 2020/21"
  only because the data starts there; counts bounded by the data start ("his 5th hat-trick
  since 2020/21") and "for the first time in our data" are not used either.
- A run that goes back to the first match in the data is not tweeted: its real length is unknown.
- Season xG, xG against and expected points are FotMob's league-table figures, used only when
  the table counts as many matches as the results do.
- Shot-based stats need every match of the team to have its details.
- Player season totals (`finishing`, runs, leaders) need every match of the league this season
  downloaded; `overtake` and `pace` need every match of every loaded season, and `overtake`
  never names a previous club the player may have joined before the data starts.
- A match FotMob has no details for leaves a hole only in its two teams' player totals of
  that season: those players are left out of season totals and `overtake`, and a `pace`
  record must beat a total from such a team-season by 2, not 1.
- A leader stat waits until its column is loaded for every match of the season (`rebuild`).
- "Their best/worst start since S" needs S at least 3 seasons ago. A promoted team's first
  clean sheet or win says "for the first time this season, at the Nth attempt".
- Each punch line belongs to one meaning ("Hard to beat" only on unbeaten runs, "Wasteful"
  only where chances were missed).
- An xG "start" compares only seasons whose first matches all have their details.
- No "last matchday" facts when the latest result is more than 10 days old (summer, breaks).

## Development

```bash
python -m pip install pytest && python -m pytest tests -q
```

The tests run the whole flow on a simulated FotMob (`tests/fake_fotmob.py`, same JSON shapes
as the real answers) with the SQLite backend: no network, no BigQuery.

| Path | Role |
|---|---|
| `football/fotmob.py` | download client: pace, retries, request cap |
| `football/model.py` | FotMob answer → reduced copy → table rows (pure functions) |
| `football/schemas.py` | every table and column type |
| `football/warehouse.py` | BigQuery and SQLite backends, views |
| `football/pipeline.py` | the incremental update |
| `football/checks.py` | data-quality gate |
| `football/tweets.py` | fact patterns, scoring, drafts |
| `football/cli.py` | the commands |
| `config/clubs.csv` | hashtags and popularity — the one file you edit by hand |

## Limits

- **FotMob has no official API.** The pipeline reads the JSON its website loads
  (`www.fotmob.com/api/data/leagues` and `/matchDetails`). It can change or be closed without
  notice, and its terms do not grant this use. Keep the 1.5 s delay and one run at a time. If
  it starts refusing GitHub's servers, run the same command from a computer — nothing else
  changes.
- History starts in 2020/21: FotMob has no shots or xG before that (Premier League checked).
- The old datasets `football_tm`, `football_index` and `football_us` in BigQuery are not used
  any more and can be deleted by hand.
- GitHub pauses scheduled workflows after 60 days without repository activity; it emails you first.
