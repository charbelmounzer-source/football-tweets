# Context for the next session

Read this first. It is the state of the project on 12 November 2026 and what to do next.
`README.md` has the user-facing documentation; this file has the history and the open items.

## What the project is

FotMob data for the top 5 European leagues, from 2020/21 → BigQuery → automatically drafted
tweets. End goal: post the tweets on X automatically. Today it only drafts them.

Owner's wishes: FotMob as the only source, data fresh at least once a day, no CSV/local files,
BigQuery as the home of the data, simple code. He prefers step-by-step changes, full file
rewrites when several things change, and honest feedback over encouragement.

## State

| Piece | State |
|---|---|
| Pipeline (`football/`) | Rebuilt on FotMob only on 12 Nov 2026 (pull request 7). 32 tests pass on a simulated FotMob. |
| Real-data trials | Run on GitHub runners into SQLite, not BigQuery (see below). All five leagues, current season. |
| BigQuery project `football-model-494509` | Billing enabled. Datasets `football_raw`, `football_core`, `football_tweets` exist with the **old Understat tables, empty** except `core.runs` and `core.quality_checks`. The first run replaces the empty ones (see `BigQueryWarehouse.ensure`). |
| Service account | `football-pipeline@football-model-494509.iam.gserviceaccount.com`, roles BigQuery Data Editor + Job User. Key saved as repo secret `GCP_SA_KEY`. |
| GitHub Actions `Daily update` | Was failing since Understat went down (2 Nov 2026); may have been disabled by the owner: check the Actions tab. Not run yet with the FotMob code. |

## Why FotMob

Understat, the first source, went offline on 2 November 2026 with no announcement. FotMob was
the only free alternative with shot-level xG and player-by-match stats for the five leagues
(FBref lost its advanced stats in January 2026; Sofascore blocks datacenter IPs;
football-data.co.uk has no xG and no players). The owner chose FotMob as the single source,
dropping the Understat archive rather than bridging two id systems and two xG models.

## What was verified on real FotMob data (11–12 Nov 2026, from GitHub runners)

- `www.fotmob.com/api/data/leagues?id=<id>&season=2020%2F2021` and `/matchDetails?matchId=<id>`
  answer a plain script: no key, no signed header, no browser. Up to 560 requests in one run
  at 1.5 s, no refusal.
- League ids: 47 Premier League, 87 La Liga, 55 Serie A, 54 Bundesliga, 53 Ligue 1.
- Premier League coverage by season: shots and xG from 2020/21; player stats without xG
  2016/17–2019/20; results only before. **Other leagues: only checked that 2020/21 lists its
  matches, not that its matches carry xG.** The first history load will show it
  (`status`: column "unusable"; rows of `team_stats` with `xg` NULL).
- The whole flow (update → checks → drafts) on the current season of all five leagues, 500
  matches: no error. The score check D2 first failed on 3 matches, which led to the own-goal
  rule below; with it, every check is clean on the three leagues re-run (290 matches). The only
  warning left is X1, a real abandoned match (Rayo Vallecano v Athletic Club).
- A second run right after the first downloads nothing twice and continues the history.
- The league table (FotMob's own count) agrees with the results for all 96 teams.

## What was learned about the data (keep in mind when changing `model.py`)

- **Own goals: trust the shot data, not the event list.** The match event list sometimes
  credits an attacker where the shot data and the player's own `goals` stat record an own goal
  (3 of 500 matches). Own goals are shots with `isOwnGoal`, carrying the scorer's own team.
- **Abandoned matches** stay in the league list with `finished` and `cancelled` both true and
  reason `aborted`; the replay is another match. They go to `fixtures` with status `cancelled`.
- **A stat that is zero is left out** of a player's stats (no shot → no `total_shots`, no xG).
- **Team xG is often lower than the sum of the shots' xG** (7 of 1,000 team rows by more than
  0.5). Probably several shots of one attack counted once; not confirmed. Check D5 only warns
  the other way round.
- **Serie A 2022/23 has 381 matches**: the extra one is presumably the relegation play-off
  (Spezia–Verona). It is counted as a league match. Not looked at.
- Unused substitutes are in `playerStats` with an empty `stats` list.
- The match answer is ~400 kB; `slim_match` keeps ~45 kB. Only the slim copy is stored and
  hashed, so page noise (polls, odds, SEO) does not look like a correction.

## Never verified

Say so plainly when it matters; do not assume these work.

- **Anything on the real BigQuery project with this code.** `replace()` (staging table +
  `DELETE`/`INSERT` in one transaction), the four views, and the replacement of the old empty
  tables are only tested against a stand-in for the client library. A cloud Claude session
  cannot reach the project. The first `Daily update` run is the test: read its log.
- **Past seasons' match details**, beyond 25 matches of 2025/26 loaded in a trial.
- **FotMob's tolerance for 2,000 requests in one run** and for daily runs. 560 worked once.
- **Postponed matches**: none seen. The status is derived from time (kick-off passed 48 h ago,
  no result), not from a FotMob field.
- **`overtake` tweets on real data**: they stay off until every match since 2020/21 has its
  details.
- **FotMob spellings in `config/clubs.csv`** for clubs not in a top league this season
  (West Ham United, Wolverhampton Wanderers and Burnley were seen in 2023/24).

## Next steps, in order

1. **Merge pull request 7, then run `Daily update` by hand** (Actions tab) and read the log.
   Expect: 35 league answers, 500 matches of this season, ~1,465 of last season, then
   "request cap reached". About one hour. If BigQuery refuses something, that is the item
   "never verified" above: fix it first.
2. **Let the history load.** ~10,700 matches of past seasons, 2,000 per run: six runs. The
   schedule does it in six days; running by hand is faster. `python -m football status`.
3. **Look at the history once loaded**: unusable matches, `xg` NULL by league and season,
   check warnings. Decide whether `FIRST_SEASON` holds for all five leagues.
4. **Tweet quality.** The owner wants to work on what makes a tweet interesting. Known weak
   spots seen on real drafts: "their longest since 2020/21" for a club with a handful of
   matches in the data (Hull City, promoted this season); punch lines that do not fit the stat
   ("Wasteful" on a points total); 54 of 106 facts are `start` facts. Patterns and scoring are
   in `football/tweets.py`; rules that keep numbers true are in the README.
5. **New tweet material FotMob gives** that is not used yet: xGOT (finishing and
   goalkeeping), big chances, possession, player ratings, touches in the box, shot situations
   (corners, fast breaks, set pieces).
6. **Auto-posting to X.** `football_tweets.drafts` already has `status`, `posted_at`,
   `tweet_id`. Nothing posts yet. Needs an approval step first.
7. **A fallback for results.** One unofficial site is still the weak point. football-data.co.uk
   (CSV, results and shots, no xG) or football-data.org (official API, results) could keep the
   results-only tweets alive if FotMob closes. Not built; the owner chose one source for now.

## Owner's machine

Windows, Git Bash (MINGW64), repo at `~/OneDrive/Desktop/projects/football-tweets`.

- `pip` is not on the path: use `python -m pip install -r requirements.txt`.
- `gh` is not installed: GitHub actions go through the browser.
- `truststore` is required there (certificate errors without it); it is in `requirements.txt`.
- `gcloud` is installed; application-default credentials are set for the project.
- He pastes several commands at once: avoid commands that prompt (add `--quiet` where it exists).
- FotMob was never tried from his connection, only from GitHub runners.

## How to work in this repo

```bash
python -m pip install pytest && python -m pytest tests -q     # no network, no BigQuery
python -m football <command> --backend sqlite --db try.db      # local file instead of BigQuery
```

- All state is in the warehouse; `football/warehouse.py` has a BigQuery and a SQLite backend
  with the same four operations. Transformations are in Python, so no dialect-specific SQL.
- Schemas are in `football/schemas.py`; adding a column there is enough.
- FotMob field paths are in `football/model.py` (`slim_league`, `slim_match`) and nowhere else.
  `tests/fake_fotmob.py` produces the same shapes: change both together.
- Changes go on a branch with a pull request; the owner merges.
- A cloud Claude session cannot reach fotmob.com, the BigQuery project, or the files a
  workflow run saves. It **can** push a branch and read workflow logs through the GitHub API.
  To see real data: a temporary workflow on the branch (trigger `push`) that runs the pipeline
  with `--backend sqlite` and prints what is needed to the log; remove it before merge.

## Lessons

- A source can vanish overnight (Understat). The slim raw copies in `football_raw` are what
  lets the tables be rebuilt without the source; keep them.
- The first Understat build was never run on real data before merge, and nothing could be
  verified once the site was down. This build was run on real data before the pull request;
  keep doing that.
- The first run retried for two hours without showing why. Fixed: stops after 5 failed
  requests in a row, prints each error, log streams live.
- The rewrite once dropped the `truststore` hook. It is in `football/fotmob.py`.
- BigQuery sandbox (no billing) cannot run this: no DML, tables expire after 60 days.
