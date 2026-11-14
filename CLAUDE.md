# Context for the next session

State on 14 November 2026. `README.md` documents commands, tables, checks and tweet patterns;
this file has only what a new session needs on top: state, data facts, open items, rules.

## The project and the owner

FotMob data for the top 5 European leagues since 2010/11 → BigQuery → automatically drafted
tweets. End goal: post on X automatically. Today it only drafts.

Owner's wishes: FotMob as the only source, data fresh at least once a day, no CSV/local files,
BigQuery as the home of the data, simple code. Step-by-step changes, full file rewrites when
several things change, honest feedback over encouragement. He checks about once a week.

## State

- **History complete** (14 Nov): 85 league-seasons 2010/11-2026/27, 29,394 played matches,
  28,735 with details; the other 659 FotMob cannot give (probed again 14 Nov: answer
  `{"error":true,"message":"Data not found"}`). Nothing left to load.
- **Daily update** (GitHub Actions, cron 04:23 UTC, really starts ~11:30-12:50 UTC because of
  GitHub's delay): a few minutes, ~100 requests, **~0.66 GiB of BigQuery a day**. All tweet
  patterns are on, incl. `overtake` / `pace` (they wait until no played match lacks a download).
- BigQuery project `football-model-494509`, datasets `football_raw`, `football_core`,
  `football_tweets`. Service account `football-pipeline@...` (Data Editor + Job User), key in
  repo secret `GCP_SA_KEY`.
- Failure alert: a failing `Daily update` opens an issue labelled `daily-failure` assigned to
  the owner, comments on repeat failures, closes on recovery (`football/alert.py`, reads `run.log`).
- Leftover probe branches `probe-empty-pages-2010` and `probe-old-goals`: the owner deletes them
  (a cloud session cannot delete branches).

## Coverage by era (what FotMob has)

| Seasons | Data | Coverage |
|---|---|---|
| 2020/21 → | everything: player + team stats, shots, xG | all but 2 matches ("no player stats") |
| 2016/17-2019/20 | player + team stats, no shots/xG | 7,191/7,203 (Ligue 1 2019/20 has real gaps; COVID: 279 matches) |
| 2011/12-2015/16 | lineups, subs, goal and card events | complete (1 Ligue 1 2011/12 missing) |
| 2010/11 | same | 55-72% per league: ~6 in 10 matches of Jan-May 2011 have no page |

Before 2016/17 events carry player id 0 and only names; `model_lineups` matches them to the
lineup. Probe of 14 Nov over 10,311 matches: goals NULL in 30 (0.3%: scorer not in FotMob's
lineup, or events not adding up, mostly Juventus home games 2011-13), cards NULL in 103.
Known wrong values (owner: no fix wanted): Ulloa's 3 goals in 2015/16 missing (no player row),
one phantom own goal (Nancy 0-4 Monaco 2010/11). Some pre-2016 matches do have player stats:
there own goals are NULL (no shot data). Assists are NULL before 2016/17.
Unknown = NULL, never 0, never compared (`plus()`, `pace`, `overtake` respect this).

## How the daily refresh works (`football/pipeline.py`)

1. 5 league pages (current season) replace that league-season: new results come from here.
2. Current-season matches whose `match_id` is not in `raw.match` are downloaded and **appended**.
   No date window: a missed run is caught up by the next one.
3. Matches of the last 10 days, if our copy is older than 20 h, are downloaded again; stored only
   if the hash changed (then delete + rewrite of those matches, one season at a time).
4. Past-season matches not in `raw.match` (none left).
- No duplicates: append only for ids without rows; `raw.match` is written last, so a half-done
  run's leftovers (rows in `team_stats`) are rewritten, not appended; check H6 fails on a duplicate.
- An unusable page keeps a `raw.match` row (`problem`), so it is not asked again; within 14 days
  of kick-off it is retried (FotMob may not have published yet).
- More frequent runs need no code change. Cost scales with runs (most of the 0.66 GiB is the
  checks and drafts reading history, not the download, not measured exactly): hourly would be
  ~16 GiB/day, too close to the quota; split a light update-only run from checks/drafts first.
  GitHub cron is late and unreliable; exact times would need e.g. Cloud Scheduler.

## BigQuery quota and cost

- Custom quota "Query usage per day" **20.48 GiB** (owner set it; keep it): 31 x 20.48 GiB is under
  the free 1 TiB/month, so queries cannot cost anything. Over the quota BigQuery refuses (resets
  08:00 UTC in winter), it does not bill. Billing so far: EUR 0.00.
- Storage a few GiB (free tier 10 GiB). Load jobs are free. GitHub Actions free (public repo).
- Owner's question: 50 GiB/day would be ~1.5 TiB/month → ~$3-4/month (needs the quota raised).
- Every command prints "BigQuery: N queries, X GiB billed": read it after a change.
- **BigQuery bills a DELETE by scanning the table**: never DELETE per batch on `raw.match`
  (13 Nov: hit the quota). Think of bytes scanned before adding a per-batch or per-day query.
  A full `rebuild` is the heaviest command: not on a day with other heavy work.

## Data facts (keep in mind when changing `model.py`)

- Own goals: trust the shot data (`isOwnGoal`, scorer's own team), not the event list.
- Abandoned matches: `finished` and `cancelled` both true, reason `aborted`; → `fixtures` as
  `cancelled`. Rayo Vallecano v Athletic Club is one (warning X1, listed with a 2026-12-09 date).
- A zero stat is left out of a player's stats. Unused subs have an empty `stats` list.
- Team xG is often lower than the sum of shot xG (D5 only warns the other way).
- Serie A 2022/23 has 381 matches (relegation play-off counted as a league match).
- The match answer is ~400 kB; `slim_match` keeps ~45 kB and every player stat key. Only the
  slim copy is stored and hashed.
- Already stored, not tweeted yet (no new download needed): `team_stats.xg_open_play` /
  `xg_set_play`, `player_matches.rating` and `minutes` (per-90 leaders), `shots.situation`
  (fast breaks). Physical stats (distance, sprints) are in `raw.match` payloads for some
  matches: key names and coverage not probed; would need a column + `rebuild`.

## Never verified

- FotMob's tolerance over months of daily runs (5,000 requests in one run worked).
- Which side `isHome` means on an old own-goal event (counted for the team that benefits).
- Postponed matches: none seen; status derived from time (kick-off 48 h ago, no result).
- What FotMob's "tackles" (attempted or won) and "dribbles" count.
- FotMob spellings in `config/clubs.csv` for clubs not in a top league this season.

## Next steps

1. **Tweet quality and content** (owner: later). He brings tweets he likes from X; turn each into
   a pattern or wording rule. Read a few days of drafts first. Then the stored material above.
2. **Dead man's switch** for a run that never starts (GitHub pauses schedules after 60 days
   without repo activity): owner creates a free healthchecks.io check, saves its ping URL as a
   repo secret; the daily job pings it on success.
3. **Auto-posting to X**: `football_tweets.drafts` has `status`, `posted_at`, `tweet_id`. Needs an
   approval step first; posting pauses on any failure. Owner: not now.
4. **Backup source only if FotMob breaks** (not to build now). FotMob has no official API (its
   terms forbid automated retrieval; it broke tools in 2024 with a new header). Plan: Sportmonks
   (licensed; ~EUR 29/month + xG add-on, check prices, try the free trial), keep FotMob ids as
   house ids with mapping tables (teams by name, matches by league + date ±1 + teams + score,
   players inside linked matches; doubtful = unlinked = no tweet), a new xG model = new xG era.
   ~3-5 days of work. `football_raw` keeps the whole history whatever happens.

## How to work in this repo

```bash
python -m pip install pytest && python -m pytest tests -q     # no network, no BigQuery
python -m football <command> --backend sqlite --db try.db      # local file instead of BigQuery
```

- All state is in the warehouse; `football/warehouse.py` has a BigQuery and a SQLite backend
  with the same operations. Transformations are in Python, no dialect-specific SQL.
- Schemas in `football/schemas.py` (adding a column there is enough; `rebuild` fills history).
- FotMob field paths only in `football/model.py` (`slim_league`, `slim_match`);
  `tests/fake_fotmob.py` produces the same shapes: change both together.
- Changes go on a branch with a pull request; the owner merges.
- A cloud session cannot reach fotmob.com or BigQuery directly. To see real data: a temporary
  workflow on a branch (trigger `push`, secret `GCP_SA_KEY` if BigQuery is needed) that only
  **reads** and prints to the log; read it with `gh run view --log`. Never write to the real
  project from a branch. Examples: branch `probe-old-goals` (`scripts/probe_old_goals.py`).
- Test on real data before a pull request (the first Understat build was not, and broke).
- Workflow runs: one running + one waiting per concurrency group `daily-update`; queueing more
  cancels the waiting one. Manual runs: input "Request cap" up to 5,000 (~2 h); the update step
  stops at 160 min so a hung run still raises the alert.

## Owner's machine

Windows, Git Bash, repo at `~/OneDrive/Desktop/projects/football-tweets`. Use
`python -m pip ...` (pip not on path); `gh` not installed (GitHub through the browser);
`truststore` needed (in `requirements.txt`, hooked in `football/fotmob.py`); `gcloud` installed
with application-default credentials. He pastes several commands at once: avoid prompts
(`--quiet`). FotMob never tried from his connection.
