# Context for the next session

Read this first. It is the state of the project on 11 November 2026 and what to do next.
`README.md` has the user-facing documentation; this file has the history and the open items.

## What the project is

Understat data for the top 5 European leagues → BigQuery → automatically drafted tweets.
End goal: post the tweets on X automatically. Today it only drafts them.

Owner's wishes: data fresh at least once a day, as few CSV/local files as possible, BigQuery
as the home of the data. He prefers step-by-step changes, full file rewrites when several
things change, and honest feedback over encouragement.

## State

| Piece | State |
|---|---|
| New pipeline (`football/`) | Merged to `main` (PRs 1–5). 29 tests pass on a simulated Understat. |
| BigQuery project `football-model-494509` | Billing enabled. Datasets `football_raw`, `football_core`, `football_tweets` created by the first run. Only `core.runs` and `core.quality_checks` have rows. |
| Service account | `football-pipeline@football-model-494509.iam.gserviceaccount.com`, roles BigQuery Data Editor + Job User. Key saved as repo secret `GCP_SA_KEY`. |
| GitHub Actions `Daily update` | Authenticates and writes to BigQuery. Fails at the download step (see blocker). May have been disabled by the owner: check the Actions tab. |
| Old pipeline | In `legacy/` (Transfermarkt, cross-source index, old README). Not run any more. |

## Blocker: Understat is down

Since about 2–3 November 2026 every understat.com URL, data endpoints included, answers
HTTP 200 `text/html` with a 26,719-byte "We will come back soon" page. Confirmed from GitHub
runners and from the owner's home connection, and reported by other projects. No return date.
The client reports it as `unreadable response: not JSON - HTTP 200, text/html, 26719 bytes`.

Nothing in the code is known to be wrong. Nothing can be downloaded until the site is back.

## Never verified on real data

Say so plainly when it matters; do not assume these work.

- **Parsing real Understat payloads** with `football/model.py`. The field names were ported from
  the old working script (`legacy/2_understat.py`, `legacy/4_build_sources.py`), not re-checked.
- **BigQuery `replace()`** (staging table + `DELETE`/`INSERT` in one transaction) in
  `football/warehouse.py`. Only `ensure()`, `append()` and `read()` have run for real.
- **The three BigQuery views** (`teams`, `players`, `latest_checks`).
- **Tweets on real data.** Texts were only seen on simulated teams.
- **Understat spellings** of the 14 clubs added to `config/clubs.csv` (other four leagues).

## Next steps, in order

1. **Import the old local archive.** It tests items 1–4 above without Understat. The archive is
   not in the repo: it is the `data/raw_understat` folder of the owner's old working folder
   (probably named `football-db`), with `league/` and `match/` inside, data up to ~18 Oct 2026.
   ```bash
   find ~ -type d -name raw_understat 2>/dev/null
   python -m football import-local "<that path>"
   python -m football tweets --date 2026-10-18 --md drafts.md
   python -m football status
   ```
   `check` will report F1 (leagues not downloaded in 36 h): expected with an old archive.
2. **When Understat is back:** re-enable the workflow, run it by hand once, read the log.
   The first full run is about 600 requests (~15 min).
3. **Second data source.** The outage shows the weak point: one unofficial site. Not researched
   yet. One other project moved to FotMob for shots and xG. Compare options before building.
4. **Tweet quality.** The owner wants to work on what makes a tweet interesting. Patterns and
   scoring are in `football/tweets.py`; rules that keep numbers true are in the README.
5. **Auto-posting to X.** `football_tweets.drafts` already has `status`, `posted_at`,
   `tweet_id`. Nothing posts yet. Needs an approval step first.
6. **Match-level history** (`--backfill-from 2021`, ~1,800 requests per season). Until loaded,
   `overtake` facts are skipped for players with a mid-season move in an unloaded season.

## Owner's machine

Windows, Git Bash (MINGW64), repo at `~/OneDrive/Desktop/projects/football-tweets`.

- `pip` is not on the path: use `python -m pip install -r requirements.txt`.
- `gh` is not installed: GitHub actions go through the browser.
- `truststore` is required there (certificate errors without it); it is in `requirements.txt`.
- `gcloud` is installed; application-default credentials are set for the project.
- He pastes several commands at once: avoid commands that prompt (add `--quiet` where it exists).

## How to work in this repo

```bash
python -m pip install pytest && python -m pytest tests -q     # no network, no BigQuery
python -m football <command> --backend sqlite --db try.db      # local file instead of BigQuery
```

- All state is in the warehouse; `football/warehouse.py` has a BigQuery and a SQLite backend
  with the same four operations. Transformations are in Python, so no dialect-specific SQL.
- Schemas are in `football/schemas.py`; adding a column there is enough.
- Changes go on a branch with a pull request; the owner merges.
- A cloud Claude session cannot reach understat.com or the BigQuery project. It can read
  workflow runs and logs through the GitHub API.

## Lessons from the first day

- The first run retried for two hours without showing why. Fixed: stops after 5 failed league
  requests in a row, prints each error, log streams live.
- The rewrite dropped the `truststore` hook of the old script. Restored.
- `import-local` reported a wrong folder as a success (0 imported). Fixed.
- BigQuery sandbox (no billing) cannot run this: no DML, tables expire after 60 days.
