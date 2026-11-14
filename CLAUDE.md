# Context for the next session

Read this first. It is the state of the project on 14 November 2026 and what to do next.
`README.md` has the user-facing documentation; this file has the history and the open items.

## What the project is

FotMob data for the top 5 European leagues, from 2010/11 (full detail from 2020/21, see
"History back to 2010/11") → BigQuery → automatically drafted tweets. End goal: post the tweets on X automatically. Today it only drafts them.

Owner's wishes: FotMob as the only source, data fresh at least once a day, no CSV/local files,
BigQuery as the home of the data, simple code. He prefers step-by-step changes, full file
rewrites when several things change, and honest feedback over encouragement.

## State

| Piece | State |
|---|---|
| Pipeline (`football/`) | FotMob only (pull request 8, 12 Nov). Runs every day on BigQuery since 12 Nov, no error. |
| History 2020/21 → | **Complete** since 13 Nov: 35 league-seasons 2020/21-2026/27, ~11,800 matches, all with details but 2 that FotMob cannot give (Bologna-Udinese 2020/21, Mallorca-Girona 2023/24, "no player stats"). Every season has its full schedule (teams x (teams-1); Serie A 2022/23 has 381 with the play-off). xG on every detailed match but one (Ligue 1 2020/21). |
| BigQuery project `football-model-494509` | Billing enabled, datasets `football_raw`, `football_core`, `football_tweets`. `replace()`, views and old-table replacement work on the real project. |
| Service account | `football-pipeline@football-model-494509.iam.gserviceaccount.com`, roles BigQuery Data Editor + Job User. Key saved as repo secret `GCP_SA_KEY`. |
| GitHub Actions `Daily update` | Cron 04:23 UTC, but GitHub starts it about 8 hours late (12:52 and 12:20 UTC on 12-13 Nov); ~3 minutes once the history is loaded. Input **rebuild** re-makes core tables from raw (needed once after new columns). |
| Pull request 9 (merged 13 Nov) | Coverage checks, `rebuild`, 14 new player / 4 team columns, overtake fix, 4 new tweet patterns, "since" rule. Rebuild run on BigQuery 13 Nov: 11,233 matches, 0 errors. |
| Pull request 11 (merged 13 Nov) | History back to 2010/11 with FotMob's eras; unknown = NULL. See "History back to 2010/11". The older seasons load over the next runs (see "Loading faster"). |
| History 2010/11 – 2019/20 | Results and tables of all 50 league-seasons loaded 13 Nov. Match pages: **2016/17-2019/20 done** (7,191 of 7,203; 12 without player stats on FotMob); 2014/15-2015/16 done (all with lineups, 0 empty); **about 7,250 left** (2010/11-2013/14) on 14 Nov. See "Load log". |
| Pull request 15 (merged 13 Nov) | Failure alerts: an issue assigned to the owner when `Daily update` fails, closed on recovery; checks F2/F3 for silent FotMob changes. See "Failure alerts". |
| Pull request 17 (merged 13 Nov) | H2 asks 95% of matches with player stats in 2016/17-2019/20 (98% from 2020/21): Ligue 1 2019/20 has real gaps. |
| Pull request 20 (merged 13 Nov) | New matches are appended, not deleted first: the load had used up the BigQuery daily query quota. See "Lessons" and "Load log". |
| Safety pull request (14 Nov) | Every command prints what BigQuery billed ("BigQuery: N queries, X GiB billed"); check H6 (error) for a match stored twice; the update step stops at 160 minutes so a hung run still raises the alert ("The run ran out of time"). |

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
- **"Since" claims (owner's request)**: a record names the most recent season *in the data*
  when the same level was reached ("since X had 9 in 2022/23"); if nobody in the data
  reached it, no claim (`tweets.last_reached`). Before, "the most since 2020/21" only meant
  "since our data starts". Also fixed: `overtake` now also needs the *current* club to start
  after the data does (his tally there could be short).
- **Quality fixes**: punch lines per meaning; a run's "since S" needs S 2+ seasons back and a
  start's 3+ (no more "their longest since 2020/21" for promoted Hull City, whose only season
  is this one); a promoted team's first clean sheet says "this season, at the Nth attempt";
  `start` needs 3 past seasons; at most 30% of the drafts from one pattern.

## History back to 2010/11 (pull request 11, merged 13 Nov)

The owner asked for every season FotMob has, with unavailable data NULL and never compared.
Probed on all five leagues (FotMob only, 13 Nov):
- FotMob lists 2010/11 to now, nothing earlier. Every season's match list is complete.
- 2020/21 on: everything. 2016/17-2019/20: player and team stats, no shots/xG.
  2010/11-2015/16: lineups, substitutions, goal and card events; no stats.
- Old events carry **player id 0**, only names: `model_lineups` matches them to the lineup
  (accent-free exact name, then a unique surname); a miss, or goals not adding up to the
  score, makes that match's goals NULL. Real sample: 94/94 matches matched.
- Goal events vs official stats on 34 matches of 2016-2019: goals 34/34, assists 32/34 ->
  assists are NOT taken from events (NULL before 2016/17).
- About 1 match in 4 of 2010/11 has an empty page ("empty page", unusable; results only).
- Ligue 1 2019/20 stopped at 279 matches (COVID): `config.SHORT_SEASONS`.
- Tweets: `plus()` keeps NULL unknown; `pace` counts an unknown tally (or a team-season with
  an empty page) as "maybe reached", so no claim rests on it; `overtake` skips unknown
  tallies and drops the G/A line when assists are unknown.
- Checks H2/H4/H5 know the eras; a match not yet downloaded is H3 (warn), not a hole.
- First load run (13 Nov, 15:54): all 2010/11-2019/20 results and tables arrived (every
  season complete); 1,917 match pages. It **failed on H2**: Ligue 1 2019/20 has 7 of 279
  matches without player stats on FotMob (97.5% < 98%), plus 1 in La Liga 2019/20. Real gaps,
  not a bug: H2 now asks 95% in the 2016/17-2019/20 era (98% from 2020/21).
- About 18,200 requests to load: ~10 runs. `overtake`/`pace` wait until all are in.

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

- **The full 2010/11-2019/20 load**: tried on 150 real match pages only (no BigQuery write
  from a branch). The first runs after merge are the test: read their logs and `status`.
- **Which side `isHome` means on an old own-goal event** (scorer's team or the team that
  benefits). Counted for the team that benefits; if wrong, the score check fails and that
  match's goals become NULL - safe, but read `status` / NULL goals per season once loaded.

- **FotMob's tolerance** over months of daily runs. 5,000 requests in one run worked (13 Nov).
- **How much BigQuery a normal day uses** after the 13 Nov fix (PR 20): read the "BigQuery: ...
  GiB billed" line at the end of the run logs (added 14 Nov). See "Load log" for what is known.
- **Name matching of old goal scorers at full volume** (2010/11-2015/16): 94/94 on a sample.
  Once loaded, count matches with NULL goals per season (expect few).
- **Postponed matches**: none seen. The status is derived from time (kick-off passed 48 h ago,
  no result), not from a FotMob field.
- **The cancelled Rayo Vallecano v Athletic Club** is listed with kick-off 2026-12-09 although
  the match was abandoned earlier: FotMob may move the date of a cancelled match to the
  replay date. Not looked at.
- **What FotMob's "tackles" counts** (attempted or won) and "dribbles" (`dribbles_succeeded`,
  first number of [won, attempted]). The tweets say "made N tackles", "completed N dribbles".
- **FotMob spellings in `config/clubs.csv`** for clubs not in a top league this season.

## Next steps, in order

1. **Finish the history load:** ~7,250 pages left on 14 Nov (2010/11-2013/14): 2 runs by
   hand with a cap of 5,000 (after 08:00 UTC if the quota was used up), or ~4 scheduled days.
   The first successful run on PR 20's code confirms the fix: watch that it does not hit the
   quota. Then read `status`: unusable per season (expect ~25% in 2010/11),
   and the first drafts that use old seasons.
2. **Tweet quality, with the owner's examples.** He will bring tweets he likes from X; turn
   each into a pattern or a wording rule. Read the drafts of a few days first.
3. **More material from FotMob** if wanted: xG open play vs set play (columns exist, not
   tweeted), per-90 leaders, physical metrics (distance, sprints: only some matches have them),
   shot situations beyond set pieces (fast breaks), player ratings (column exists).
4. **Failure alerts: the rest.** The issue alert is built (see "Failure alerts"). Left: the
   dead man's switch (healthchecks.io: needs the owner to create a free account and a check,
   then a repo secret with its ping URL; the daily job pings it on success), and "posting
   paused" in the alert once posting exists.
5. **Auto-posting to X.** `football_tweets.drafts` already has `status`, `posted_at`,
   `tweet_id`. Nothing posts yet. Needs an approval step first. The owner said not now.
6. **Backup source, only if FotMob has a problem.** See "If FotMob breaks" below. Not to be
   built now (owner, 13 Nov): kept as the plan.

## Load log (13-14 Nov)

| Run (13 Nov, UTC) | Cap | Result |
|---|---|---|
| 15:54 | 2,000 | Results/tables 2010/11-2019/20 all in; 1,917 pages. **Failed on H2** (Ligue 1 2019/20 gaps), fixed by PR 17 |
| 15:59 | 2,000 | Same H2 failure (started on the old code); 1,995 pages |
| 17:42 | 2,000 | OK: 1,995 pages; 17 checks, 0 errors; 25 drafts |
| 18:06 | 5,000 | OK: 4,995 pages; 2016/17-2019/20 complete |
| 21:58 | 5,000 | **Failed: BigQuery daily query quota** (QueryUsagePerDay). Alert issue #19 opened (worked as designed; #19 since closed). Fixed by PR 20 |

- The quota resets at midnight Pacific (08:00 UTC in winter). The scheduled run really starts
  around 12:20-12:50 UTC (GitHub's delay), so after the reset.
- **Measured by the owner on 14 Nov (console):** the project's "Query usage per day" quota is
  **20.48 GiB** (a custom value; keep it: 20.48 GiB x 31 days is under the free 1 TiB a
  month, so queries cannot cost anything). Billing > Reports for November up to 13 Nov:
  0.03 TiB of queries, 0.03 GiB-month of storage, **total EUR 0.00**. So 13 Nov used the
  whole 20 GiB: about 0.16 GiB per batch of 100 matches on the old code, far less than a
  full scan of raw.match per DELETE: the clustering did limit what was billed.
- With a 20 GiB day, a full `rebuild` is the heaviest thing: not on the same day as a big load.
- Load jobs are free; storage is far under the free 10 GiB; GitHub Actions is free (public repo).

## Loading faster (learned 13 Nov)

- GitHub keeps **one running and one waiting** run per concurrency group (`daily-update`):
  queueing more cancels the waiting one. The owner queued 10 runs on 13 Nov; 8 were cancelled.
  Cancelled runs do not trigger the failure alert.
- To load faster, start one run at a time with the input "Request cap for this run" at
  **5000** (~2 hours at 1.5 s per request; the update step stops at 160 minutes, the job at
  170). Not more.

## Failure alerts (built 13 Nov, owner's request)

The owner checks about once a week; anything wrong must reach him without looking.
- **Issue alert (done):** when `Daily update` fails, it opens an issue labelled
  `daily-failure`, assigned to the repo owner (GitHub emails him). `football/alert.py` reads the
  run's log (`run.log`, written with `tee`; the steps use `shell: bash` so pipefail keeps the
  failure) and names the cause in plain words. A new failure comments on the open issue; a
  successful run closes it ("Recovered"). Tried for real on 13 Nov with a temporary failing
  workflow: open, comment, close all worked (issue #14, a [TEST]).
- GitHub's own failure emails for *scheduled* runs go to whoever last edited the schedule:
  that is the "claude" account, not the owner. Hence the issue.
- **Silent changes:** checks F2 (recent matches without xG / player minutes) and F3 (more
  than 10 matches without a result 2+ days after kick-off) are errors, so they fail the run
  and trigger the alert.
- **Not covered yet:** a run that never starts (GitHub pauses schedules after 60 days without
  repo activity; Actions outage). Plan: healthchecks.io dead man's switch (see Next steps).
- When posting exists: posting pauses on any failure, and the alert says so.

## If FotMob breaks (risk and backup plan, discussed 13 Nov)

FotMob has no official API: we read the JSON its website loads; its terms forbid automated
retrieval. It changed before (2024: a required `x-fm-req` header broke the soccerdata library
for about a month); data-centre IPs get occasional 403/429. Likely breaks and fixes: a field
moved (fix in `model.py`, under an hour); a new header/token (hours to weeks); GitHub's IPs
blocked (run from another machine); endpoints closed (switch source). The raw copies in
`football_raw` keep the whole history whatever happens.

Backup source if needed: **Sportmonks** (licensed API; on 13 Nov roughly €29/month for five
leagues plus an xG add-on from €15/month: check current prices; try its free trial first:
coverage of the five leagues, xG and player stats, history depth). Cheaper without confirmed xG:
API-Football (~$16-39/month). Free results-only backup: football-data.org.

How to switch without losing the FotMob history (estimated 3-5 days of work):
- Keep FotMob ids as the house ids; link the new source's ids through mapping tables.
- Teams (~170 since 2010): automatic name match plus a short manual table.
- Matches: league + date (±1 day) + both teams, confirmed by the final score.
- Players: matched inside each linked match (~14 per team: name, shirt number, minutes),
  linked once consistent over several matches; doubtful ones listed, never guessed. Only
  players active on both sides of the switch need a link. Unlinked = unknown = no tweet.
- Run both sources side by side for a few weeks (or load 1-2 recent seasons from the new one)
  to build the links, report linked / doubtful, then switch the daily feed.
- A new xG model (Sportmonks vs Opta via FotMob) starts a new xG era: xG records never mix
  the two. Goals, results, cards agree between sources; assists can differ slightly.

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
- **BigQuery bills a DELETE by scanning the table.** On 13 Nov the history load deleted before
  every batch of 100 new matches, scanning raw.match (all stored answers, >1 GB) each time:
  tens of GB per run, and the 5th run of the day failed on the project's daily query quota
  ("Custom quota exceeded ... QueryUsagePerDay"). Fixed: new matches are appended (load jobs
  are free); only matches that already have rows are deleted and rewritten, one season at a
  time (clustering on season limits the scan). Think of the bytes scanned before adding a
  query that runs per batch or per day.
