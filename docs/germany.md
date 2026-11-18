# Germany: the 1. Bundesliga data

Read before querying German data or touching `data/ingest/src/ingest/sources/hockeybundesliga.py`.

## Where it lives

**A separate database, `hockey_stats_de`**, on the same MySQL server, with the same schema (`ingest migrate`). Never load German data into `hockey_stats`. The English analysis code (predictor, Elo, simulate, the weekly graphics, the MCP server: 29 queries in 18 files as of 2026-10-20) reads every played match with no source filter, so German games would leak into the English models. Women's games named "Damen" would even land in the men's model. `ingest import-bundesliga` refuses to run against a database that holds England Hockey matches.

To query it: `docker compose exec db mysql -uhockey_stats -phockey_stats hockey_stats_de` (add `--default-character-set=utf8mb4` or umlauts print as `?`). To point a model at it, set `DATABASE_URL=...hockey_stats_de`. The models still need a German `tier_of` and gender split first; see below.

**In the committed seed:** `data/seed/hockey_stats_de.sql.gz` (about 0.5 MB). `./data/seed/restore.sh` creates the database, grants it to `hockey_stats` and loads it, so a fresh clone needs no scraping. After a German import, run `./data/seed/dump.sh hockey_stats_de` and commit the `.sql.gz`.

## The source

hockeybundesliga.de, run by Hockeyliga e.V. It has no API, no key and no terms of use (only an Impressum). robots.txt disallows only `/admin/`. The scraper is polite: one request at a time with a 2-second pause, and retries with back-off. Finished-match pages are cached in `data/cache/hockeybundesliga/` (gitignored, mounted at `/app/cache`), so a re-run after a parser fix costs no requests.

Pages read, per season and gender:
- **season page** (current: `/match-center/1-bundesliga-1/herren|damen`; past: `/die-hockeyliga/service/archiv/feldhockey-YYYY-YYYY-1-bundesliga-herren|damen`) for the league id (`data-league`) and every match date.
- **match day** `/matchcenter/matchdaytab/<league>/<date>` for fixtures, clubs, score, round and venue.
- **report** `/leaguematch/<comp>/<pairing>/report` for kickoff, umpires, HT/FT/shoot-out, goals and cards per side, squads and staff.
- **match page** `/leaguematch/<comp>/<pairing>` (only for matches with goals) for the live ticker. This is the only place goal *type* is given.

When 2026/27 ends, its season page moves to the archive: repoint `SEASONS["2026/27"]` and add the new season.

## Coverage

Played matches (league + play-offs + play-downs), as loaded 2026-10-20:

| Season | Men | Women | Detail |
|---|---|---|---|
| 2022/23 | 118 | 112 | Scores all season; scorers for 1,049 of 1,055 goals, all typed. Minutes, cards and squads only from partway through (squads in 102 of 230 matches) |
| 2023/24 | 115 | 112 | Full (8 goals untyped). Pool format, see below |
| 2024/25 | 148 | 149 | Full |
| 2025/26 | 150 | 149 | Full |
| 2026/27 | 47 | 48 | Full so far; 169 fixtures still to play |

**Checked against the site's own league tables (2026-10-20): all ten season/gender tables match exactly** (games, wins, shoot-out wins, draws, losses, goals, points). 2022/23 women's does so only with one result the site never published, added from confirmed evidence (below). The 2025/26 men's top-scorer list matches too. The check script compares by team name, so a club that renamed (SC Frankfurt 1880 / SC Frankfurt 80, Rot-Weiss Köln / RW Köln) has to be matched by its numbers: teams keep one row, under the latest name.

1,431 players, 7,148 events, 36,223 squad places.

Flashscore has men's 1. Bundesliga scores back to 2012/13 (hand-paste only) and **no** women's history.

## Gotchas

- **Shoot-outs settle draws** ("n.P.") from 2023/24 on. The stored score is the drawn regulation score; the shoot-out goes in `home_shootout`/`away_shootout`. The site's table: S = regulation wins, U = draws, SnP = draws won on shoot-out, N = losses. Points are 3 for a win, 2 for a shoot-out win, 1 for a shoot-out loss. 2022/23 had plain draws.
- **Competitions:** `Men's 1. Bundesliga` / `Women's 1. Bundesliga` (the league), `... Play-offs` (quarter-finals to the final) and `... Play-downs` (relegation). Names are kept the same every season on purpose.
- **2022/23 and 2023/24 were pool seasons.** Two pools of six, each a double round-robin, plus games against the other pool: 16 league games each. 2022/23 calls the pools "Staffel A/B" and the cross games "Hinrunde"; 2023/24 calls them "Pool A/B" and "Staffel". All are filed as league; the pools weren't split by strength. 2022/23's knockout is "K.O. Phase H/D" (play-offs) and "Playdown H/D" (H/D = Herren/Damen).
- **Event codes:** `FG` field goal, `PC` penalty corner ("KURZE ECKE"), `PS` penalty stroke (the ticker's "7M", a 7-metre penalty), `G` goal of unknown type (no ticker), `GC`/`YC`/`RC` cards. Card colour comes from the report's icon fill (green triangle `#007836`, yellow square `#f8d200`, red circle `#ff0014`). An unknown round label, goal label or card colour stops the import rather than guess.
- **Coaches get cards too.** Anyone listed under a team's "Staff" is skipped, with a warning (24 across 2023/24 to 2026/27). Nine staff yellows (Rabente, Keller, Pongs and others) were deleted from 2025/26 after the first load, which ran before this check existed. **Some matches don't list staff**, so about nine coach cards remain (Stralkowski, Cheesman, Pongs, Keller, George in 2024/25), credited to a "player" who is in no squad. They weren't deleted, because not-in-squad isn't proof (Julius Hayner is a player). For player card counts, require the player to be in that match's squad (`match_players`); the audit's "events by a player not in their team's squad" check lists them.
- **Identity:** clubs are keyed on the site's club slug (stable across seasons; the per-season team ids in URLs are not). Teams are `<slug>:M` / `<slug>:W`, named `<club> M1` / `<club> W1`. **Players have no ids on the site**, so a player is club + name ("Max K." style abbreviations included). Some players are anonymised to first name plus initial.
- **Times** are German local time, naive.
- **Unfilled knockout slots** ("TEAM 1 v TEAM 2, K.O. Phase D") and kickoffs shown as `--:--` are skipped / left at midnight.

## Running it

```bash
DE="mysql+pymysql://hockey_stats:hockey_stats@db:3306/hockey_stats_de?charset=utf8mb4"
docker compose run --rm -e DATABASE_URL="$DE" ingest ingest import-bundesliga --season 2026/27 --incremental
```

Then `./data/seed/dump.sh hockey_stats_de` and commit the seed. `--incremental` skips matches already loaded as played, so a weekly run fetches only what's newly finished (about 20 requests). Without it, a whole season is about 600 requests (around 25 minutes). `--dry-run` scrapes and reports without writing. Problems (something the parser doesn't recognise) stop the write; warnings (the site disagreeing with itself, staff cards) are reported and loaded.

Known site gaps (the site disagreeing with itself; nothing to fix in the parser):
- **HTHC v Münchner SC, women's, 3 Sep 2022** (`hthc--msc`) has no result on the site itself (match day shows "14:00", the report is empty), but the site's table counts it as a 4-2 HTHC win and Flashscore shows it finished 4-2. Confirmed by the user 2026-10-20 and stored as 4-2 via `MISSING_RESULTS` in the importer (applied on every import; no scorers, as the site has none). Same bar as England's `SCORE_CORRECTIONS`: human-confirmed, evidence recorded.
- **UHC v Düsseldorfer HC, women's 2022/23 quarter-final** (`uhc--dhc-q-1-3`): the header says "n.P. 4:3 (4:3, 4:3)" and lists 8-6 shoot-out kicks, but the goal events say 4-3 in normal time. Stored as a 4-3 win with no shoot-out (a header implying a 0-0 shoot-out is treated as none).
- `rwk--uhc-77391` (2025/26 women's) lists 8-8 shoot-out kicks against an 8-9 result; the result is used.
- 10 matches whose goal events don't add up to the score (2 in 2023/24 with no events at all, the rest 2022/23). The audit lists them.

The page cache is about 500 MB. It's only needed to re-parse without re-scraping, so it can be deleted once the parser is settled.

## Before using it in the models

`predictor.py` splits gender on "women" in the competition name, which works here ("Women's 1. Bundesliga"). Its `tier_of()` looks for "Premier" / "Division 1" and files everything else as one tier, which is fine for a one-division league but gives it the label "Conference". Play-off and play-down games need the same thought as England's Finals Weekend (the final is neutral, the earlier rounds aren't). Backtest before trusting it.

## Power rankings (built 2026-10-21)

`prem_elo.py --league de` runs the Premier Division Elo on the 1. Bundesliga, one pool per gender: league,
play-offs and play-downs (every side in them is a 1. Bundesliga side), 2022/23 on, every side level at 1500,
2022/23 burn-in. **Shoot-outs count as the drawn result they settle.** The Final Four weekend is neutral
unless the host club is in it (`home_factor_de`). Rounds: a Thursday or Friday game belongs to that weekend;
a weekend often holds two matchdays, so posts count matchdays (`games_by_round`, the games most sides have played).

**Settings (`TUNED_DE`), checked the England way:** every setting swept, scored out of sample on 2024/25
and 2025/26 (translator fitted on 2023/24 to the season before), adopted only if both seasons improved.

| Log-loss / accuracy | 2024/25 | 2025/26 |
|---|---|---|
| Men's, England's settings | 0.931 / 57.4% | 0.869 / 62.7% |
| Men's, adopted (pull 0.5, promoted −150, home 20) | **0.910** / 58.8% | **0.844** / 60.7% |
| Women's, England's settings (kept) | 0.852 / 63.8% | 0.898 / 62.4% |

Much harder to call than the Prem (0.64-0.76 men): the league is tighter and ~19% of games are drawn.
- **Men's home edge is real here**, unlike the Prem: +0.35 goals a game (se 0.13), home sides win 57% of
  decisive league games. Home 15-30 Elo points all similar; 20 used. **Women's:** +0.15 (se 0.11), not clear
  of zero, and a home setting hurt 2024/25: 0.
- **Men's summer pull 0.5** (England 0.75): best in each test season on its own; 0.3-0.5 flat.
- **Promoted −150** both genders (8 promoted or returning sides per gender in the data; −120 to −200 flat).
- Women's: carry 1.0, promoted −200 and k 20 each helped one season and hurt the other, so kept.
- 2026/27 pre-season averages drift (men 1467, women 1548); not re-centred, as in England.

**Post:** `content/series/build-power-rankings.py --league de --refresh --post --png DIR`, the same five
slides as the Prem post with "Men's" (not "Men's/Open"), German short names (`SHORT_DE`), "After
Matchday N". **German colours (user, 2026-10-21):** black, red and gold, so a German post never reads as a Prem one:
Men's on near-black, Women's on pure white (a warm off-white was England's paper in all but name), gold
accent and rises, red for falls. Applied automatically with `--league de`; the options it was picked
from: `--league de --themes` (four schemes) and `--themes --grounds` (four women's grounds for option A). After Matchday 8: PNGs `content/weekly/2026-10-17/power-rankings-de/`, review artifact
https://claude.ai/artifact/9zjXeN9WPmN4QW22XMBeLV. Verified: ratings v `rankings()`, zero-sum, W-D-L v
games played, 578 + 571 = 1,149 matches. Crests: `assets/badges/<slug>.png`, converted from the site's
webp (`clubs.logo_url`); `download_badges.py` keeps the extension, so convert with `sips` after it.
