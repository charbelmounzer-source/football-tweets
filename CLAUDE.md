# Context for the next session

Read this first. It is the state of the project on 13 November 2026 and what to do next.
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
| Pipeline (`football/`) | FotMob only (pull request 8, 12 Nov). Runs every day on BigQuery since 12 Nov, no error. |
| History | **Complete** since 13 Nov: 35 league-seasons 2020/21-2026/27, ~11,800 matches, all with details but 2 that FotMob cannot give (Bologna-Udinese 2020/21, Mallorca-Girona 2023/24, "no player stats"). Every season has its full schedule (teams x (teams-1); Serie A 2022/23 has 381 with the play-off). xG on every detailed match but one (Ligue 1 2020/21). |
| BigQuery project `football-model-494509` | Billing enabled, datasets `football_raw`, `football_core`, `football_tweets`. `replace()`, views and old-table replacement work on the real project. |
| Service account | `football-pipeline@football-model-494509.iam.gserviceaccount.com`, roles BigQuery Data Editor + Job User. Key saved as repo secret `GCP_SA_KEY`. |
| GitHub Actions `Daily update` | Daily 04:23 UTC, ~3 minutes once the history is loaded. Input **rebuild** re-makes core tables from raw (needed once after new columns). |
| Branch `data-checks-and-tweets` (13 Nov) | Coverage checks H1-H4, `rebuild` command, 14 new player / 4 team columns, overtake fix, 4 new tweet patterns. See "Done on 13 Nov". |

## Done on 13 Nov 2026

- **Coverage checks** over every season (`checks.coverage`, H1-H4, `status` table): the owner
  wants to know that (almost) every match of every season is there; 1-2 missing matches are fine.
- **overtake was permanently off**: it waited for every match to have details, and 2 never
  will. "Complete" now means every match downloaded; the 2 unusable matches are holes of
  their two teams in that season only (`Data.holes`): those players' tallies are skipped.
- **Player data was always there, just unused** by the tweets. New columns from the stored
  answers (no new request): big chances created/missed, touches in box, dribbles, tackles,
  interceptions, recoveries, duels, aerials, passes into final third; keepers: saves, goals
  conceded, xGOT faced, goals prevented. Teams: big chances missed, open-play / set-piece xG,
  keeper saves. `rebuild` fills the history from raw.
- **New tweet patterns**: player_streak, pace, performance (hat-tricks, 3+ assists,
  goalkeeping), leader (players and teams, incl. set-piece goals); shot-stopping in `luck`.
- **Quality fixes**: punch lines per meaning; "their longest since" needs 3 seasons in the
  league (promoted Hull City); a promoted team's first clean sheet says "this season, at the
  Nth attempt"; `start` needs 3 past seasons; at most 30% of the drafts from one pattern.

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

- **FotMob's tolerance** over months of daily runs. 2,000 requests in one run worked (12 Nov).
- **Postponed matches**: none seen. The status is derived from time (kick-off passed 48 h ago,
  no result), not from a FotMob field.
- **The cancelled Rayo Vallecano v Athletic Club** is listed with kick-off 2026-12-09 although
  the match was abandoned earlier: FotMob may move the date of a cancelled match to the
  replay date. Not looked at.
- **What FotMob's "tackles" counts** (attempted or won) and "dribbles" (`dribbles_succeeded`,
  first number of [won, attempted]). The tweets say "made N tackles", "completed N dribbles".
- **FotMob spellings in `config/clubs.csv`** for clubs not in a top league this season.

## Next steps, in order

1. **Merge the branch, then run `Daily update` once with rebuild ticked** (~10 minutes more
   than a normal run). Tried on a copy of the real data on 13 Nov: 11,233 matches re-modelled,
   every new column filled, 14 checks, 0 errors.
2. **Tweet quality, with the owner's examples.** He will bring tweets he likes from X; turn
   each into a pattern or a wording rule. Read the drafts of a few days first.
3. **More material from FotMob** if wanted: xG open play vs set play (columns exist, not
   tweeted), per-90 leaders, physical metrics (distance, sprints: only some matches have them),
   shot situations beyond set pieces (fast breaks), player ratings (column exists).
4. **Auto-posting to X.** `football_tweets.drafts` already has `status`, `posted_at`,
   `tweet_id`. Nothing posts yet. Needs an approval step first. The owner said not now.
5. **A fallback for results.** One unofficial site is still the weak point. football-data.co.uk
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
  To see real data: a temporary workflow on the branch (trigger `push`, secret `GCP_SA_KEY`)
  that **reads** BigQuery, copies raw.* into SQLite on the runner, runs `rebuild`, checks and
  tweets there and prints to the log (done on 13 Nov; see the branch history of
  `scripts/probe.py`). Never write to
  the real project from a branch. Remove the workflow before merge.

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
