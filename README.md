# Football tweets: top 5 leagues, fresh every day

Understat data for the Premier League, La Liga, Serie A, Bundesliga and Ligue 1, loaded into
BigQuery every day, checked, and turned into tweet drafts.

```
Understat ──► football_raw      payloads exactly as downloaded
          └─► football_core     matches, fixtures, team_matches, player_seasons, player_matches, shots
                                + quality_checks, runs
                    └─► football_tweets.drafts     today's drafts (also the "already said" history)
```

Nothing is kept on a computer: no local archive, no SQLite, no CSV export. All state is in
BigQuery, so the job runs from anywhere and picks up where it stopped.

## What runs every day

`python -m football daily` (GitHub Actions, 04:23 UTC — `.github/workflows/daily.yml`):

1. **Update** — the current season's league data for the 5 leagues; match details (rosters,
   shots) for every played match not loaded yet; match details again for matches of the last
   10 days, stored only if Understat changed them. About 100 requests a day in season (~3 min),
   5 when nothing was played in the last 10 days, 1.5 s apart.
2. **Checks** — 9 data-quality checks on the current season, stored in
   `football_core.quality_checks`. An *error* stops the run before any tweet is drafted.
3. **Tweet drafts** — the best 25 facts across the 5 leagues go to `football_tweets.drafts`
   and to the run's summary page on GitHub.

Only what changed is rewritten: one league-season, or the matches touched. A failed request is
retried (15 s, 30 s, 60 s); a league that stays down is skipped and reported, the others load.
A payload that is empty or has lost results is refused and the previous data stays.

## Set up (once)

1. **Billing on the BigQuery project.** The pipeline uses `DELETE`/`INSERT`, which the
   BigQuery sandbox refuses, and sandbox tables expire after 60 days. With billing enabled
   this volume stays inside the free tier (10 GiB storage, 1 TiB queries a month).
2. **Service account** with roles *BigQuery Data Editor* and *BigQuery Job User*; create a
   JSON key and save it as the repository secret `GCP_SA_KEY`
   (Settings → Secrets and variables → Actions).
3. **First load from your laptop** (reuses your old archive, so this season is not downloaded twice):
   ```bash
   pip install -r requirements.txt
   gcloud auth application-default login
   python -m football import-local data/raw_understat    # your existing downloads -> BigQuery
   python -m football daily --md drafts.md               # the rest: history 2014/15-2020/21, anything new
   python -m football status
   ```
   Without the old archive, skip `import-local`: the first `daily` downloads everything
   (about 600 requests and 15 minutes in November, more later in the season).
4. **Turn on the schedule**: merge to `main`; run it once by hand from the Actions tab
   (*Daily update → Run workflow*) to confirm Understat answers GitHub's servers.

The project id is read from `$BQ_PROJECT` (repository variable `BQ_PROJECT`), default in
`football/config.py`.

## Commands

```bash
python -m football daily                      # update + checks + drafts
python -m football update                     # download and load only
python -m football check                      # checks only (exit code 1 on errors)
python -m football tweets --top 25 --md drafts.md
python -m football tweets --date 2026-10-20   # as if today were that day
python -m football status                     # what is loaded per league-season, last runs
python -m football update --backfill-from 2021 --max-requests 1500   # match-level history, in slices
```

Add `--backend sqlite --db try.db` to any command to work in a local file instead of BigQuery.
`--leagues EPL,La_Liga` limits a run to some leagues.

**Match-level history.** League-level data (results, team stats, player season totals) goes back
to 2014/15. Player-by-match rows and shots are loaded for the current season; past seasons come
with `--backfill-from` (most recent first, ~1,800 matches per season, resumable). To spread it
over the daily runs, set the repository variable `BACKFILL_FROM` (e.g. `2021`).

## Tables (`football_core`)

Seasons are stored by start year (`2026` = 2026/27); `season_label` gives `2026/27`. Ids are Understat's.

| Table | One row per | Main columns |
|---|---|---|
| `matches` | played match | league, season, date, kickoff_utc, home/away team, goals, result (H/D/A), xG, pre-match forecast |
| `fixtures` | match without a result | same, plus status: `upcoming`, `awaiting_result` (kicked off < 48 h ago), `postponed` |
| `team_matches` | team × played match | home_away, opponent, goals_for/against, result (W/D/L), xg, xga, npxg, npxga, ppda, deep, xpts, points |
| `player_seasons` | player × league × season | teams, games, minutes, goals, non_penalty_goals, assists, xg, npxg, xa, shots, key_passes, cards |
| `player_matches` | player × match (details loaded) | team, started, minutes, goals, assists, shots, xg, xa, shots_on_target, npxg, penalty_goals |
| `shots` | shot (details loaded) | minute, player, team, result, xg, x, y, situation, shot_type, assist_player |
| `quality_checks` | check × run | level, row_count, status, sample — view `latest_checks` = last run |
| `runs` | pipeline run | requests, matches new / corrected / backfilled, seconds, errors |

Views: `teams`, `players`, `latest_checks`. `football_raw.league` and `football_raw.match` keep the
latest payload of each league-season and match, so any table can be rebuilt without downloading.
`football_tweets.drafts`: run_date, rank, score, type, league, entity, text, length, evidence
(JSON), fact_id, status (`draft` now; `posted`, `tweet_id`, `posted_at` are there for auto-posting).

## Checks

| Id | Level | Catches |
|---|---|---|
| F1 | error | a league not downloaded in the last 36 hours (warning in July–August) |
| M1 | error | a match id present twice, or in both `matches` and `fixtures` |
| M2 | error | a played match without exactly 2 team rows |
| M3 | error | team rows that disagree with the match score |
| D1 | warn | a match played 2+ days ago without player details |
| D2 | warn | score ≠ players' goals + own goals |
| D3 | warn | a player's goals ≠ his goals in the shot data (e.g. Understat match 30804) |
| D4 | warn | a team without exactly 11 starters |
| X1 | warn | postponed fixtures |

## Tweet drafts

Six fact patterns, each run over every team and player: `contrast` (top 3 for one stat, bottom 5
for its partner), `streak` (droughts ended, runs), `start` (best/worst start vs past seasons),
`overtake` (a scorer passes his tally for a previous club), `luck` (goals vs xG, points vs xPts,
finishing), `preview` (head-to-head runs before next week's fixtures).

Score = rarity × context (1.5 played on the last matchday, 1.3 plays next) × club popularity
(`config/clubs.csv`: club as Understat spells it, hashtag, popularity 1–3). At most 2 drafts per
team or player; a fact drafted in the last 14 days is not drafted again; drafts over 280
characters as X counts them (emoji = 2) are dropped.

Rules that keep the numbers true:
- "since 2014/15" comes from the data (first season loaded for that league), never "ever".
- A run that goes back to the first match in the data is not tweeted: its real length is unknown.
- Shot-based stats need every match of the team to have its details.
- `overtake` only when a player's match rows add up to Understat's season totals, and never for
  a previous club he may have joined before the data starts.
- No "last matchday" facts when the latest result is more than 10 days old (summer, breaks).

## Development

```bash
pip install pytest && python -m pytest tests -q
```

The tests run the whole flow on a simulated Understat (`tests/fake_understat.py`) with the SQLite
backend: no network, no BigQuery.

| Path | Role |
|---|---|
| `football/understat.py` | download client: pace, retries, request cap |
| `football/model.py` | payload → table rows (pure functions) |
| `football/schemas.py` | every table and column type |
| `football/warehouse.py` | BigQuery and SQLite backends |
| `football/pipeline.py` | the incremental update, `import-local` |
| `football/checks.py` | data-quality gate |
| `football/tweets.py` | fact patterns, scoring, drafts |
| `config/clubs.csv` | hashtags and popularity — the one file you edit by hand |
| `legacy/` | the previous weekly pipeline (Transfermarkt, cross-source index, old README). Still runs: `bash legacy/weekly_update.sh` |

## Limits

- Understat is not an official API. Keep the 1.5 s delay and one run at a time. If it starts
  refusing GitHub's servers, run the same command from your computer (cron/launchd) — nothing
  else changes.
- Transfermarkt (frozen since July 2026) and the cross-source index are no longer refreshed.
  Their BigQuery datasets (`football_tm`, `football_index`, old `football_us`) are untouched.
- GitHub pauses scheduled workflows after 60 days without repository activity; it emails you first.
