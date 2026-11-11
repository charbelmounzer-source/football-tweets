# FotMob → our tables: what maps, what is lost

Written 11 November 2026 from two real answers read on a GitHub runner (Premier League
2026/27, and match 5795512, Man United 4–4 Aston Villa, 8 Nov 2026). One league, one match:
every other league, season and edge case below is **not checked**.

## The two requests

| Request | Size | Replaces |
|---|---|---|
| `https://www.fotmob.com/api/data/leagues?id=47` | 0.9 MB | Understat `getLeagueData` |
| `https://www.fotmob.com/api/data/matchDetails?matchId=<id>` | 0.4 MB | Understat `getMatchData` |

No key and no signed header were needed on that day. League ids: 47 Premier League; the other
four (87, 55, 54, 53 according to other projects) are not checked.

## Table by table

### `matches` and `fixtures` — from the league answer, `fixtures.allMatches[]` (380 rows)

| Column | FotMob field | Note |
|---|---|---|
| match id | `id` | new ids, unrelated to Understat's |
| date, kickoff_utc | `status.utcTime` | |
| home / away team | `home.name`, `home.id`, `away.…` | names differ from Understat ("Manchester City") |
| goals, result | `status.scoreStr` ("3 - 0") | text to split; result H/D/A is derived |
| played or not | `status.finished`, `started`, `cancelled`, `awarded` | `postponed` status: not seen yet |
| round | `round` | new, Understat had none |
| xG per team | **not in the league answer** | comes from the match answer |
| pre-match forecast | **none** | lost |

### `team_matches` — from the match answer, `content.stats.Periods.All`

| Column | FotMob key | Note |
|---|---|---|
| xg, xga | `expected_goals` | home and away values |
| npxg, npxga | `expected_goals_non_penalty` | |
| goals, result, points | `header.teams[].score` | derived |
| **ppda** | none | **lost** |
| **deep** | none (`touches_opp_box` is a different measure) | **lost** |
| **xpts** | none per match | **lost per match**; season total exists (`table[].data.table.xg[].xPoints`), or compute it from shots |
| new | `expected_goals_open_play`, `…_set_play`, `…_on_target`, `big_chance`, possession, corners, cards, distance covered | not in Understat |

### `shots` — `content.shotmap.shots[]` (38 rows in the match read)

| Column | FotMob field | Note |
|---|---|---|
| minute | `min`, `minAdded` | |
| player, team | `playerId`, `playerName`, `teamId` | |
| result | `eventType`: Goal, AttemptSaved, Miss, Post | blocked = `isBlocked`; own goal = `isOwnGoal` |
| xg | `expectedGoals` | Opta model, not Understat's |
| x, y | `x`, `y` | pitch in metres-like units (x up to ~105), Understat used 0–1: convert |
| situation | `situation`: RegularPlay, FastBreak, FromCorner, SetPiece, Penalty | Understat: OpenPlay, FromCorner, SetPiece, DirectFreekick, Penalty |
| shot_type | `shotType`: LeftFoot, RightFoot, Header | |
| assist_player | not on the shot | goals only: `header.events.*Goals.*[].assistPlayerId` |
| new | `expectedGoalsOnTarget`, `isFromInsideBox`, `keeperId` | |

The same shots are repeated in four other places of the answer (per player, per period, goal
events, player of the match). Read `content.shotmap.shots` only.

### `player_matches` — `content.playerStats{player id}` (40 players) + `content.lineup`

| Column | FotMob field | Note |
|---|---|---|
| player, team | `id`, `name`, `teamId` | also `optaId` |
| started | `content.lineup.homeTeam.starters[]` / `subs[]` | |
| minutes | stat `minutes_played` | |
| goals, assists | `goals`, `assists` | |
| shots, shots_on_target | `total_shots`, `ShotsOnTarget` | |
| xg, xa | `expected_goals`, `expected_assists` | |
| npxg, penalty_goals | not given | derive from the player's shots (`situation = Penalty`) |
| key passes | `chances_created` | Opta definition |
| cards | `lineup…performance.events[].type` (`yellowCard`) | red card value not seen |
| new | rating, distance covered, top speed, defensive actions, duels | |

Unused substitutes are in the list with no stats: skip them.

### `player_seasons`

No single answer gives it. Two options, neither tested:
1. add up `player_matches` (needs every match of the season loaded: 380 requests per league-season);
2. the per-stat files the league answer points to (`https://data.fotmob.com/stats/47/season/36781/goals.json`),
   one file per stat, on another host. Season files are listed from 2016/17 only.

## What this means for the tweets

| Pattern | With FotMob |
|---|---|
| `streak`, `start`, `preview` | work (results only) |
| `contrast` | works for xG-type stats; pairs using PPDA or deep disappear |
| `luck` | goals vs xG works; points vs xPts needs xPts computed from shots |
| `overtake` | works once player match rows are loaded; previous-club history restarts from zero |

## Open questions before building

1. **History.** The league lists 17 seasons (2010/11 →), stat files 11 (2016/17 →). How a past
   season is requested, and from which season matches carry xG, is not known.
2. **Two xG models.** FotMob xG is Opta's; the old archive is Understat's. Do not compare a
   2026/27 FotMob figure with a pre-outage Understat figure in one tweet.
3. **Ids.** Teams, players and matches all have new ids. Either FotMob becomes the only source
   (simple), or an id bridge is needed to join the Understat archive (the old pipeline had one
   for Transfermarkt; it was the fragile part).
4. **Volume.** Tested with 2–6 requests. A first load is ~100 matches per league this season
   (~500 requests, 0.4 MB each, ~200 MB); the daily run ~50. Not tested at that volume.
5. **Terms.** Unofficial, like Understat. Keep the delay, one run at a time, and expect it to
   change without notice.
