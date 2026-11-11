# Football database: top 5 leagues, 2021/22 to the current season

Built on your computer, uploaded to BigQuery (project `football-model-494509`) as one dataset per source plus an index that links them.

| | |
|---|---|
| **Leagues** | Premier League, La Liga, Serie A, Bundesliga, Ligue 1 (league matches only) |
| **Sources** | [Transfermarkt open dataset](https://github.com/dcaribou/transfermarkt-datasets) (CC0): 2021/22–2025/26 · [Understat](https://understat.com): 2021/22–current season |
| **BigQuery** | `football_tm` (Transfermarkt) · `football_us` (Understat) · `football_index` (links) |

Seasons are stored by start year (`2024` = 2024/25); `season_label` gives `2024/25`. Python 3.8+, no extra packages (`pip3 install truststore` if you get an SSL error).

---

## 1. Flow

### Folder layout

```
football-db/
├── README.md
├── weekly_update.sh      the weekly refresh in one command
├── scripts/              the pipeline, numbered in run order
│   ├── 1_transfermarkt.py
│   ├── 2_understat.py
│   ├── 3_link.py
│   ├── 4_build_sources.py
│   ├── 5_build_index.py
│   ├── 6_to_bigquery.py
│   ├── 7_sanity_checks.py
│   ├── 8_tweets.py       draft tweets from the latest matchday
│   └── paths.py          where every folder and file lives (used by all scripts)
├── config/               your settings: clubs.csv (hashtags, popularity), link_overrides.csv, club_names.csv
├── index/                master copy of the keys — BACK IT UP
├── data/                 downloads and databases (can all be recreated)
│   ├── raw_transfermarkt/
│   ├── raw_understat/
│   └── football.db, football_tm.db, football_us.db, football_index.db
└── output/               written for you: bq_export/, sanity_report/, tweets/
```

Run every command from the `football-db` folder.

```
1_transfermarkt ─┐
2_understat     ─┴─► data/football.db ─► 3_link ─► 4_build_sources ─► 5_build_index ─► 6_to_bigquery ─► 7_sanity_checks
                                                    data/football_us.db ─► 8_tweets
```

| Step | Script | What it does | Output |
|---|---|---|---|
| 1 | `scripts/1_transfermarkt.py download` / `build` | Downloads the Transfermarkt files, keeps the top 5 leagues and seasons | `data/raw_transfermarkt/`, Transfermarkt tables in `data/football.db` |
| 2 | `scripts/2_understat.py update` / `build` | Downloads the current season's league file (matches, fixtures, team stats, player totals) and match files (rosters, shots) for new and recent matches (see "What is frozen" below), 1 request every 1.5 s | `data/raw_understat/`, `us_*` tables in `data/football.db` |
| 3 | `scripts/3_link.py` | Finds the same match, club and player in both sources (see section 4), then applies `config/link_overrides.csv`; prints a coverage report | `link_*` tables in `data/football.db` |
| 4 | `scripts/4_build_sources.py` | Writes one clean database per source. Cleaning stays inside a source: same column names, proper dates, names next to ids, TM lineups + stats in one table, `match_id` on US team stats, shooter's team from the roster. Prints checks | `data/football_tm.db`, `data/football_us.db` |
| 5 | `scripts/5_build_index.py` | Gives every real match, club and player one stable key, updating the master CSVs in `index/` (section 3) | `index/*.csv`, `data/football_index.db` |
| 6 | `scripts/6_to_bigquery.py` | Uploads each database to its dataset; column types inferred, tables fully replaced (nothing to delete first) | BigQuery |
| 7 | `scripts/7_sanity_checks.py` | Runs 31 checks on the BigQuery data (section 5) | scoreboard, `output/sanity_report/` |
| 8 | `scripts/8_tweets.py` | Finds stats linked to the last matchday and next fixtures, drafts tweets (section 6) | `output/tweets/<date>.md` |

### Commands

First build:
```bash
python3 scripts/1_transfermarkt.py download && python3 scripts/1_transfermarkt.py build
python3 scripts/2_understat.py fetch --no-matches && python3 scripts/2_understat.py fetch --season 2026 && python3 scripts/2_understat.py build
python3 scripts/3_link.py && python3 scripts/4_build_sources.py && python3 scripts/5_build_index.py
gcloud config configurations activate football        # personal account
python3 scripts/6_to_bigquery.py --project football-model-494509
```

Weekly refresh (~5 min): runs steps 2–8, then check `index/review.csv`, `output/sanity_report/summary.csv` and the drafts in `output/tweets/`:
```bash
bash weekly_update.sh
```
The BigQuery project is set at the top of `weekly_update.sh` (`PROJECT=football-model-494509`).

### What is frozen, what is refreshed

| Data | Rule |
|---|---|
| Understat match files (`data/raw_understat/match/`) | Downloaded once. `update` downloads again only matches played in the **last 30 days** (`--window`), to pick up Understat's corrections. Older files are the frozen archive |
| Played matches not yet downloaded | Always fetched, whatever their date (e.g. a postponed match played months later) |
| Current season league file | Downloaded on every `update` (results, fixtures, team and player season totals) |
| `football_us.fixtures` | Rebuilt every run; a match leaves it once played and enters `matches` |
| `football_us.player_seasons`, `players`, `teams` | Only the current season changes: its league file is re-downloaded each run. Past seasons' league files are never downloaded again, so their rows stay identical |
| `football_tm` | Frozen (dataset stopped July 2026); upload only with `--sources tm` |
| `football_index` | Rows only added (section 3) |

BigQuery tables are replaced in full from the local data each run, but only current-season data is ever re-downloaded: every past row is rebuilt from the same frozen file, so it comes out identical and can't be duplicated. The current season is taken from today's date (from July = the new season).

`scripts/6_to_bigquery.py` options: `--sources tm,us,index` (subset), `--clean` (delete tables not in the database), `--dry-run` (files in `output/bq_export/` only).

---

## 2. Datasets

Each source keeps **its own ids**. Columns are as delivered by the source unless listed under "derived".

### `football_tm` — Transfermarkt (updates stopped July 2026)

| Table | Grain | Main columns |
|---|---|---|
| `matches` | match | league_id, league, country, season, round, date, status, home/away team id + name, goals, result (H/D/A), league positions, managers, referee, venue, attendance |
| `player_matches` | player × match, **unused subs included** | team, home_away, lineup_role (starter/bench), started, played, minutes, goals, assists, penalty_goals, yellow, red, captain, shirt_number, match_position |
| `events` | goal, card, substitution, shootout | minute, team, type, detail (Normal Goal / Penalty / Own Goal / card colour), player, assist_player, player_in, description |
| `players` | player | name, birth_date, nationality, position, sub_position, foot, height_cm, photo_url, has_profile |
| `teams` | club in the loaded matches | name, full_name |
| `transfers` | transfer (whole careers) | date, from/to team, fee_eur (0 = free or loan, null = unknown), market_value_eur at the move |
| `market_values` | valuation | date, value_eur, team |

Derived: `result`, `home_away`, `started`/`played` (from lineups and stats), `penalty_goals` (parsed from event descriptions), names for players without a profile (from lineups/events, `has_profile = 0`).

### `football_us` — Understat

| Table | Grain | Main columns |
|---|---|---|
| `matches` | played match | league, season, date, kickoff_utc, home/away team, goals, result, xG, forecast_home_win/draw/away_win (pre-match) |
| `fixtures` | match without a result | same ids and teams, kickoff_utc, status (upcoming / postponed), forecast |
| `team_matches` | team × played match | home_away, opponent, goals_for/against, result, xg, xga, npxg, npxga, ppda, ppda_allowed, deep, deep_allowed, xpts, points |
| `player_matches` | player × match played (**current season only**) | team, home_away, match_position, started, minutes, goals, own_goals, assists, cards, shots, xg, xa, key_passes, xg_chain, xg_buildup, shots_on_target, npxg, penalty_goals |
| `shots` | shot (**current season only**) | minute, player, team, result, xg, x, y (0–1, x = 1 is the goal line), situation, shot_type, last_action, assist_player |
| `player_seasons` | player × league × season | teams, position, games, minutes, goals, non_penalty_goals, penalty_goals, assists, xg, npxg, xa, shots, key_passes, cards, xg_chain, xg_buildup |
| `players` | player | name, first_season, last_season, leagues (= **number** of leagues played in) |
| `teams` | club | name, leagues (= **list** of league names), first_season, last_season |

Derived: fixtures `status` (no result 3 h after kick-off = postponed), `started` (roster position ≠ "Sub"), `team_matches.match_id` (matched on team + kick-off), `shots_on_target` / `npxg` / `penalty_goals` (summed from shots), shot `team` (from the roster; `home_away_flag` keeps Understat's own flag).

### `football_index` — links between sources

`players`, `teams`, `matches` (played matches and fixtures): one row per source id. A fixture keeps its `match_key` once played.

| Column | Meaning |
|---|---|
| `player_key` / `team_key` / `match_key` | Our stable id for the real player / club / match |
| `source`, `source_id` | `transfermarkt` or `understat`, and that source's id |
| `name` | Name in that source (matches: `date home v away`) |
| `method` | `new` (key created), `link:<method>` (attached by scripts/3_link.py), `link:override` |
| `added_at` | Date the row was added |

`merges`: keys merged into another (old_key → new_key), when an Understat-only id was later linked.

---

## 3. Ids and the index

- **Join across sources only through the index**, never on names (names are not unique: two Otávios, two Allans).
- A key with one source only = that item exists in one source (e.g. current-season matches, promoted clubs' players).
- **Keys never change.** `index/*.csv` is the master copy: each run only adds rows. Back up the `index/` folder; it is the only part that can't be recreated with the same keys.
- **Understat-only player later linked** (via `config/link_overrides.csv`): its key is merged into the Transfermarkt key and logged in `merges`.
- **scripts/3_link.py changes its mind about an id already indexed:** nothing changes; the case is listed in `index/review.csv`. Fix by editing the key in the CSV.
- `index/review.csv` also lists every new Understat-only player and club, to check for missed links.

Example — Transfermarkt profile next to Understat season xG:
```sql
SELECT tp.name, tp.birth_date, s.season_label, s.goals, s.xg
FROM `football-model-494509.football_index.players` it
JOIN `football-model-494509.football_index.players` iu
  ON iu.player_key = it.player_key AND iu.source = 'understat'
JOIN `football-model-494509.football_tm.players` tp ON tp.player_id = it.source_id
JOIN `football-model-494509.football_us.player_seasons` s ON s.player_id = iu.source_id
WHERE it.source = 'transfermarkt' AND tp.name = 'Erling Haaland'
ORDER BY s.season;
```

---

## 4. Linking rules (`scripts/3_link.py`)

1. **Matches:** league + date (±1 day) + score; then checked against the clubs, and linked by clubs when the score differs (e.g. awarded matches).
2. **Clubs:** majority vote over linked matches; by name for clubs with no shared match (promoted this season).
3. **Players:** name similarity + minutes, appearances and goals within each club-season.
4. **Players new to the top 5:** name only, with age (16–37) and club checks.
5. **Overrides** in `config/link_overrides.csv` always win:
   ```
   kind,us_id,tm_id
   player,14293,975336
   ```
   `tm_id` is the number in the Transfermarkt web address; empty `tm_id` removes a link. `scripts/3_link.py report` shows coverage and the biggest unlinked players.

---

## 5. Sanity checks (`scripts/7_sanity_checks.py`)

Each check is a query returning the rows that look wrong. **error** = must be 0; **warn** = review (some are real quirks, e.g. awarded matches, nicknames). `--validate` checks the SQL for free; `--only P1,M1` runs a subset. Rows go to `output/sanity_report/<id>.csv`, the scoreboard to `output/sanity_report/summary.csv`.

| Area | Checks |
|---|---|
| Index (I1–I5) | one key per source id; never two Transfermarkt ids in one key; two Understat ids in one key; every source id indexed; index rows pointing to missing ids |
| Players (P1–P7) | linked names share no word; impossible age; clubs differ in a season; minutes/goals differ; season present in one source only; possible missed links (same name, one source each); Transfermarkt duplicate profiles |
| Clubs (T1) | linked names share no word |
| Matches (M1–M4) | linked matches with different clubs; more than 1 day apart; different score; played matches with no partner |
| Transfermarkt (D1–D6) | duplicates; score vs goals + own goals; 11 starters; impossible minutes; player's club not in the match; played but no stats |
| Understat (U1–U8) | duplicates; match in both matches and fixtures; 2 team rows per match; player's club not in the match; score vs goals; swapped rosters; goals vs shot data; team xG vs players' xG |

## 6. Tweet drafts (`scripts/8_tweets.py`)

```bash
python3 scripts/8_tweets.py                                   # Premier League, 15 drafts
python3 scripts/8_tweets.py --leagues all --top 25
python3 scripts/8_tweets.py --date 2026-10-20                 # as if today were that day
```

1. **Context:** the last matchday (results within 4 days of the latest one) and the fixtures of the next 7 days. Facts are about the teams and players in them.
2. **Fact patterns**, each run over every team, player and stat:

| Type | Finds | Example |
|---|---|---|
| `contrast` | top 3 for one stat, bottom 5 for its partner: shots vs conversion, xG vs goals, xG against vs conceded, xPts vs points | "Manchester United have the most shots in the Premier League (168) but the 5th-worst conversion rate (6.5%)." |
| `streak` | first clean sheet / win / home win / away win / goal in N matches; unbeaten, winning, winless, losing, scoring runs vs the team's and the league's longest since 2021/22 | "Chelsea have kept a clean sheet in the Premier League for the first time in 25 matches (9 months)." |
| `start` | points, goals, conceded, xG after N matches vs the same point of each past season (and every team in the league) | "Manchester City have 7 points after 8 matches - their worst start in any of their 4 Premier League seasons since 2021/22" |
| `overtake` | a scorer passes or matches his league tally for a previous club; penalties and G/A | "Joao Pedro's goal against … meant he matched his total league tally for Brighton (19)." |
| `luck` | biggest goals − xG and points − xPts gaps; best finisher (non-penalty goals − npxG) | "Arsenal have 21 points but 12.0 expected points (+9.0)" |
| `preview` | head-to-head runs for next week's fixtures (one per fixture) | "Tottenham have won their last 5 league meetings with Leeds." |

3. **Scoring:** rarity (rank gap, streak length, record) × context (1.5 if the team played on the last matchday, 1.3 if it plays next) × club popularity from `config/clubs.csv`. At most 2 drafts per team or player.
4. **Repeats:** every proposed fact is logged in `output/tweets/history.csv`; a fact proposed by an earlier run in the last 14 days is skipped (`--include-repeats` to show it). Rerunning on the same day gives the same drafts.
5. **Output:** `output/tweets/<date>.md` (drafts + the evidence behind each number) and `<date>.csv`. Tweets over 280 characters are flagged. **Check the evidence before posting.**

`config/clubs.csv` (`club,hashtag,popularity`): club as Understat spells it, hashtag added at the end, popularity 1–3 (each point above 1 adds 25% to the score). Clubs not listed get no hashtag and popularity 1.

Notes: uses `data/football_us.db` (Understat only, no BigQuery cost). `overtake` uses season totals from 2021/22 and leaves out seasons split between two clubs. Phrasing always says "since 2021/22", never "ever".

## 7. Limits

- **Transfermarkt stopped in July 2026:** no current-season matches, lineups, cards, substitutions, shirt numbers or captains; no summer 2026 fees; no values after June 2026.
- **Transfermarkt match stats gap:** in ~710 matches of 2021/22 (all 5 leagues), plus Hertha and some Ligue 1 in 2022/23, some players are in the lineup but have no minutes, goals or cards (`played = 1`, `minutes = 0`; sanity check D6, also behind D2). Understat has those seasons as totals (`player_seasons`); match level needs the history download below.
- **Understat match-level history isn't downloaded:** `player_matches` and `shots` cover the current season only (`scripts/2_understat.py fetch` gets the rest: ~9,000 requests, ~4 h, resumable).
- **Known Understat errors** (still wrong after a fresh download, i.e. on Understat's side):
  - match 30804, Villarreal v Deportivo (05/09/2026): every goal appears twice in `shots`. Player goals and the score are right: count goals from `player_matches` / `matches`, not `shots`.
  - match 31574, Juventus v Parma (29/08/2026): player xG is 0 (to confirm).
  - team xG in `matches` often differs slightly from the sum of player xG (sanity check U8): Understat computes them separately; use one or the other consistently.
- **Understat isn't an official API**; keep the 1.5 s delay.
- **Market values are estimates:** tweet "valued at", not "worth". Check any surprising fact before posting.
- **BigQuery free tier:** 1 TiB of queries and 10 GiB storage per month. In a sandbox project (no billing) tables expire after 60 days; rerun the upload.

---

## 8. Files

| File | Role | Can be recreated? |
|---|---|---|
| `scripts/` | The pipeline (section 1) | — |
| `weekly_update.sh` | Steps 2–8 in one command | — |
| `config/clubs.csv`, `config/link_overrides.csv`, `config/club_names.csv` | Your settings and manual fixes | No — keep |
| `output/tweets/history.csv` | Facts already proposed (avoids repeats) | Losing it only means repeats come back |
| `index/` | Master copy of the keys | **No — back it up** |
| `data/raw_transfermarkt/`, `data/raw_understat/` | Downloads | Yes, by downloading again (Understat: hours) |
| `data/*.db` | Working and upload databases | Yes, by rerunning the scripts |
| `output/` | Upload files and sanity reports | Yes |
