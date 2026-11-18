# Predictor game: build plan

The free score-prediction game, **FHA Weekly Predictor** ("FHA Predictor" for short; name chosen
2026-11-16). Never "Super 6": it is Sky Bet's brand, and England Hockey's indoor competition is
the Super Sixes. Call to action: "Play the FHA Predictor game!". Website first, iOS app later on the same backend. Decisions and open questions
live in memory (`project_predictor_game.md`); this is the build drawer. What it must do and
how we'll know it worked (goals, requirements, metrics): [predictor-game-spec.md](predictor-game-spec.md).
**Visual map** (where data lives, who can access it, which script writes what, built vs planned):
[website-architecture.html](website-architecture.html), published as the artifact
https://claude.ai/artifact/NAgZ1GvK5Lf8iaaR6aoqVd (state at 18 Nov 2026; update both when the structure changes).

**Timeline:** build Dec–Jan, private test in February, public launch with Phase 2 match 12
(mid-March 2027), run to the May final, relaunch for the full 2027/28 season in September.

## What a round is

Phase 2 weekends have exactly 3 Prem matches per gender in each half, so a round picks itself:
**Top 6 week** (3 Men's/Open + 3 Women's), then **Lower 6 week**, alternating. No editorial choice
of fixtures. QF weekends (4 QF legs + 2 relegation games per gender) are undecided.

A player calls the exact score of all six. Picks lock at the **first kick-off of the round**
(Super 6's rule: one deadline, nothing to game by waiting for early results).

## Stack

| Piece | Choice | Why |
|---|---|---|
| Frontend | Static site (Vite + plain TypeScript, or Astro), hosted on **Cloudflare Pages**, free tier, at `thefieldhockeyanalyst.com` (DNS already on Cloudflare; see Before launch) | No server to run, no traffic cap; the iOS app later talks to the same backend. Supabase needs no custom domain (that's a paid add-on) |
| Backend | Supabase: Postgres, auth, row-level security | Login and a database without writing a server |
| Sign-in | Supabase **anonymous sign-in** first, then link an email with a **6-digit code** (Supabase's email OTP, free) | Covers "play once without an account": anonymous picks get a share image but are only scored once an email is confirmed before the deadline. A code, not a magic link: Instagram's in-app browser loses the session when a link opens elsewhere. Needs a custom SMTP sender (Supabase's own only reaches the project team). Apple/Google sign-in added with the app |
| Feed | Python script in this repo, run by `weekly.sh` | The pipeline already freezes everything needed |

The game's database is **separate from `hockey_stats`**. Only the weekly script crosses between
them, with the Supabase service key held in `.env` (never committed, same rule as `EH_API_KEY`).

**Where site data lives (agreed 2026-11-18).** `hockey_stats` stays the master; everything online
is a one-way copy. Supabase holds only what users write or the game logic needs (picks, profiles,
the six fixtures and their results). Everything read-only is **static JSON** in
`web/public/data/`, written by the weekly run: public by nature (anyone can open it in DevTools),
so never put anything private there. **Later, stats pages (not built):** fixed pages (match
report, team page, league table, top scorers) stay static JSON, grouped so the file count stays
well under Cloudflare's free-plan limit (~20,000 files a deploy, as understood 2026-11-18;
`hockey_stats` has ~6.9k matches and ~6.5k players, so group per team per season rather than one
file per match). Only a dashboard where users pick their own filters needs a live queryable copy:
then a read-only mirror, either a Supabase `stats` schema (61 MB vs the 500 MB free plan; joins
with the predictor) or Cloudflare D1 (SQLite, 5 GB free; `export_sqlite.py` already builds the
file). Decide at the first such page.

## Tables (Supabase)

**Project (created 2026-11-18):** `fha`, ref `<supabase-project-ref>`, London (eu-west-2), Postgres
17, free plan, owned by the FHA email account, **not** connected to GitHub (that is Branching, a
paid add-on). Migrations live in `predictor/supabase/migrations/`; apply with
`supabase db push --workdir predictor --remote` (CLI 3 targets a local stack unless `--remote`; it reads `SUPABASE_DB_PASSWORD` from the environment, so export it from `.env` first) (CLI installed with Homebrew, `supabase login` done; the link
is in the gitignored `predictor/supabase/.temp/`, redo with `supabase link --workdir predictor --project-ref
<supabase-project-ref>` on a new machine). DB password and service key go in `.env` only.
`predictor/supabase/config.toml` is for the local stack; don't `supabase config push` it unreviewed.
**Where things live:** everything predictor-side is in `predictor/` (Supabase migrations, the
feed scripts `publish_round.py` / `publish_results.py`); no folder called `game/` (unclear, user
2026-11-18). The website is `web/` (the whole domain, not just the predictor; Cloudflare builds
from it). `analysis/game_scoring.py` and `analysis/fha_model_picks.py` stay in `analysis/`
because they run in the Jupyter container.

**First migration PUSHED 2026-11-18** (remote matches local, `supabase migration list --workdir predictor --remote`):
`predictor/supabase/migrations/20261118120000_init.sql` is the source of truth for the schema; the
table list below is the summary. Three schemas: `public` (profiles, user_roles), `ref` (clubs,
teams, matches; ids = hockey_stats ids, filled by the feed), `predictor` (editions > rounds >
round_matches, players, picks, plus the `pick_points` and three leaderboard views).
**Second migration PUSHED 2026-11-18** (round 10 moved across; `publish_round.py` re-run on it OK) (`20261118180000_matches_to_ref.sql`,
user: a match is a hockey fact, not predictor data): `predictor.matches` becomes `ref.matches`
(fixture, kick-off, score, status), `predictor.round_matches` (round_id, match_id) says which
matches a round uses, and picks key on (player, round, match) with a foreign key to
`round_matches`, so a picked match can't leave its round and one match could serve two editions.
**Third migration PUSHED 2026-11-18** (`20261118200000_games_to_editions.sql`,
user: "games" reads as a hockey match): `predictor.games` is renamed `predictor.editions` (one run
of the FHA Predictor: `prem-2026-27` launches March 2027, 2027/28 is the next edition) and
`rounds.game_id` becomes `edition_id`; the views are rebuilt with `edition_id`
(`leaderboard_season` keeps its name: the site's tab is "Season"). Pure rename, tested on a
throwaway Postgres with all three migrations and the full rule tests.
**Live end-to-end check 2026-11-18** (publishable key, real project): anonymous sign-in, profile
made by the trigger, join as `public` (joining as `admin` refused), save and change a round 10
pick, read own, public sees nothing before the deadline, then the test user deleted through the
admin API and its profile, player and pick cascaded away (all three tables empty again).
**Auth still open, none blocks design:** custom SMTP sender for the 6-digit email code (SPF/DKIM in
Cloudflare DNS; needed for the January email-linking step), the email template showing the code,
CAPTCHA (Cloudflare Turnstile, free) on anonymous sign-ins before launch to stop scripted
sign-ups, and the FHA Model's auth user. (Full secret key in `.env` since 2026-11-18;
`publish_round.py` runs from `.env` alone. The CLI login can also fetch keys with `--reveal`.)
The 7-day result freeze is DROPPED (user: later, a scheduled job driven by the fixtures); until
then only the service role writes scores. Refuses to run if any pick exists. Tested on a
throwaway Postgres 17 over migration 1 + live-shaped data: data moved, pick outside the round
rejected, own-only and deadline rules, unlinking a picked match refused, scoring and tie-break,
anon reads; and the guard stops it (changing nothing) when a pick exists. Tested on a throwaway
Postgres 17 with stubbed `auth`: own-picks-only and deadline lock (insert and update), picks
hidden until the deadline (tables and views, signed in and anon), no self-promotion to admin or
faking `confirmed_at`, anonymous-then-confirmed-after-deadline kept off that round, P/A score 0,
the exact-score tie-break, the 7-day freeze (since dropped), account deletion cascading, and `pick_score` equal to
`game_scoring.py` on all 6,561 pick/result pairs 0–8. To test again: same stub (roles anon /
authenticated / service_role, `auth.users`, `auth.uid()` from `request.jwt.claim.sub`).
**Exposed 2026-11-18:** `predictor` and `ref` added to the Data API (checked: each answers with
the publishable key; use header `Accept-Profile: <schema>`). Later migrations: `set -a; source .env; set +a` then `supabase db push --workdir predictor --remote`. Done already: `SUPABASE_DB_PASSWORD` in `.env`, anonymous sign-ins on (2026-11-18).
Design points the migration settles: a profile row is made for every sign-in (anonymous too) by a
trigger on `auth.users`, which also copies `confirmed_at`; a player is on a round's leaderboard
only if `confirmed_at` is before that round's deadline (admin and model always); players join
with `insert (id)` only, so `kind` is always `public` unless the service key sets it; the server
stamps `submitted_at`. **The FHA Model needs a real auth user** (made with the admin API, e.g.
`model@` the domain, email confirmed), then a `players` row with `kind = 'model'`: part of
building `publish_round.py`.

**One account for everything FHA (user, 2026-11-18).** The project is named for the brand, not
the game: a sign-in may later cover more than the predictor. So account data and game data are
split: `public.profiles` (one row per person: id = auth user id, display name, created) is
shared; the predictor's tables below live in their own **`predictor` schema** (which must be
added to the Data API's exposed schemas in the dashboard). A future feature gets its own schema.
**Schema rule (agreed 2026-11-18):** one Postgres database (`fha`), schemas as the boxes. Two
shared: `public` (true of a person across the site) and `ref` (hockey facts, `hockey_stats` ids,
grows only when a feature needs something, never a full copy). Plus one schema per feature that
users write into (`predictor`, later e.g. `blog`). A feature schema may reference `public` and
`ref`, never another feature's schema. Read-only pages need no schema: static JSON. `auth` is
Supabase's; never edit it.
Sign-in emails come from "The Field Hockey Analyst", not the game. Free plan: 500 MB, 50k monthly
active users, and **the project pauses after 7 days idle**: fine in season (weekly feed), so add
a weekly keep-alive to `weekly.sh` before the summer break.

- `rounds`: id, season, label ("Round 12: Top 6"), deadline (first kick-off), status
  (`open` / `locked` / `scored`), scored_at (freeze after scoring: deferred to a scheduled job).
- `ref.matches` (migration 2; was `predictor.matches`): id = hockey_stats match id, home/away
  team ids, kick-off, result home/away (null until played), `status` (mirrors `S`/`R`/`P`/`A`).
  `predictor.round_matches` (round_id, match_id): the round's six.
  **Times are `timestamptz`** (stored as UTC, like a Unix time): EH gives local UK times with no
  zone, so convert from Europe/London once, in `publish_round.py`. Test it with a kick-off on
  28 Mar 2027, the day UK clocks go forward.
- `players`: id (= `profiles.id`), `kind` (`public` / `admin` / `model`), joined. Display name
  comes from `profiles`. The admin and the FHA Model are two fixed rows: on every leaderboard, labelled. Players can
  edit their display name only, never `kind` (column grant). Only email-confirmed players appear
  on leaderboards.
- `picks`: player_id, match_id, home, away, submitted_at. Unique per player per match.
  RLS: you can write only your own, only before the round's deadline; everyone's become readable
  after the deadline.
- `pick_points` (view or SQL function): points per pick from the scoring rule, so the rule can
  change in one place and every table recomputes.
- `leaderboard_round`, `leaderboard_season`, `leaderboard_alltime` (views), all three from
  launch.

**Security, Supabase-specific:** views skip row-level security unless created with
`security_invoker = true`, so every view over `picks` needs it, or picks leak
before the deadline. One test before launch: as a normal player, try to read someone else's pick
before the deadline through the API; it must fail.

A postponed or abandoned match scores zero for everyone and is not re-opened.

## The weekly feed

Both ends hang off the existing `weekly.sh` modes, as one extra step each:

1. **`weekly.sh verdict`** marks results; `predictor/publish_results.py <verdict.json>` writes the
   scores and the round's first-goal minute (earliest goal event across the six, `status='R'`
   only), then sets the round to `scored`. The database does the scoring.
2. **In the same run, the next round opens** (user, 2026-11-16: as soon as scores are in and the
   next fixtures are known, not at Thursday's Call). `predictor/publish_round.py` creates the round
   and its six matches and inserts the FHA Model's picks: the same model as the Top 6 simulation
   (`simulate.py`): the result from the power rankings (Elo's likeliest of H/D/A, never a draw so
   far), then the likeliest Poisson scoreline for that result (user, 2026-11-17: the FHA Model
   is one model, not a points-maximiser). Frozen before anyone
   plays. Kick-off times are not expected to move once a round is open.
3. The admin plays through the site like anyone else, before the deadline.
4. **`publish_round.py` BUILT 2026-11-18** (steps 1-2 minus the FHA Model's picks):
   `python3 predictor/publish_round.py --round N --matches ids` (or `--from/--to` for every Prem
   match in a window; `--label`, `--deadline "<UK time>"` for practice replays, `--dry-run`,
   `--force` to touch a round that is no longer open). One run upserts `ref.clubs`/`ref.teams`
   (colours from `analysis/club_colours.csv`), the season's edition (`prem-2026-27`), the round
   (deadline = first kick-off) and its matches (schedule fields only, never scores, so a re-run
   can't wipe a result; matches dropped from the id list are deleted), then writes the
   pick-screen JSON via `round_json.py` from the same rows. Refuses a deadline already passed.
   Re-runs are idempotent (same game/round ids). Stdlib only, reads the local MySQL, writes over
   the Data API with `SUPABASE_SERVICE_ROLE_KEY` from `.env`: use the project's **secret** key
   (`sb_secret_...`; dashboard Project Settings > API Keys, or `supabase projects api-keys
   --reveal`, without `--reveal` it is masked and gives 401). Legacy `service_role` JWT also works.
   **Round 10 is live in Supabase** (21 Nov 2026, the user's six, deadline 14:00 UK: Men's/Open
   Bowdon v Exeter Uni 6526, Holcombe v H&W 6528, East Grinstead v Oxted 6525; Women's Wimbledon v
   Birmingham Uni 7134, Reading v Nottingham Uni 7136, Durham Uni v Teddington 7133). Phase 1
   weekends have six a side and no Top/Lower 6 split, so the user picks 3 + 3 and passes
   `--matches`; `--from/--to` takes every Prem match, which is never a round on its own.
   Still to hook into `weekly.sh`; FHA Model picks wait for its auth user and Phase 2 support.
5. **Pick-screen JSON** (`round_json.py`, also runnable alone): per match, both teams (display
   name, `badge_key` = `assets/badges-hq/` filename, hockey_stats ids), kick-off in UTC, last three
   results each (`status='R'`, any competition, can span seasons) and the last meeting (`null` if
   they have never met: the page needs a "first meeting" fallback).
   **The Data API can't embed across schemas** (e.g. `predictor.round_matches` -> `ref.matches` gives
   PGRST200), so the site joins Supabase matches to the JSON's teams by id.

So no Claude and no manual entry on a normal week. **Phase 2 is the gap:** `simulate.prepare()`
and `round_card.card()` only see Phase 1 fixtures, so the FHA Model can't predict a Phase 2 round
yet. Build after Christmas; test it in the January replays.

## Screens

1. **This round**: countdown to the deadline, six match cards (badges, kick-off in the viewer's
   time zone, score steppers), Men's/Open on navy and Women's on paper as on Instagram. Save.
2. **Saved**: "Your six" card and share image for everyone. Not signed up: "Sign up to be
   scored", email then 6-digit code, before the deadline.
3. **Results**: your picks against the scores, points per match, round total, how the admin and
   the FHA Model did.
4. **Leaderboard**: round / season / all-time tabs, signed-up players only; your row pinned;
   admin and FHA Model marked.
5. **How to play**: the scoring rule and the deadline rule, in the factual explainer style.
6. **Account**: display name, email, delete account.
7. **Privacy notice**: what is collected (email, display name, picks), why, how long it is kept,
   who holds it (Supabase, the host), and how to delete your account. UK GDPR; linked from the
   footer and from the email prompt on Saved.

No admin screen: rounds and results come from the scripts.

Reference for the pick row and a "% of players made this prediction" line under each match:
`content/references/predictor-pick-screen-pl.webp` (README entry 05).

## Build order

1. **Dec, week 1–2:** Supabase project, tables + RLS as migrations in `predictor/supabase/`, anonymous
   sign-in, the "This round" screen against a hand-made round.
2. **Dec, week 3–4:** `publish_round.py` / `publish_results.py`, scoring function, Results and
   Leaderboard screens.
3. **Jan:** email linking, share image, How to play, privacy page; design pass on the brand system.
4. **Jan, testing trick:** replay 2026/27 Phase 1 rounds as practice rounds (deadline in the
   future, results released by hand) so the whole loop is exercised before any real fixture.
5. **Feb:** private test with a few friends on real Phase 1 replays; fix, then freeze.
6. **Mar, match 12:** launch.

## Scoring rule (LOCKED 2026-11-16)

**Exact score 5 · One goal off 3 · Right result 2.** "One goal off" counts only with the right
result: one side's score out by one (for 4-1: 3-1, 5-1, 4-2, 4-0). Called 2-2 when it ends 2-1
is a wrong result, so 0. A draw pick can never be one goal off (moving one score breaks the
draw), so it scores 5 or 2. Wrong result 0. Max 30 a round (6 x 5).

**The reasoning, for How to play and the launch post:** modelled on Sky's Super 6, but hockey
has more goals, so exact scores are rarer and a near miss earns points.

How to play wording: *"Exact score: 5. Right result and one goal off: 3. Right result: 2."*

In code: `game_scoring.rule_points("Exact 5 / one goal off 3 / result 2", ...)`; the
database's `pick_points` must match it, and the FHA Model's entry is
`best_picks(pred, "Exact 5 / one goal off 3 / result 2")`.

**Tie-break (decided 2026-11-18): round winner only, from the picks themselves.** Level on points,
the player with more exact scores wins; still level, more "one goal off" picks; still level, joint
winners. Season and all-time ties stay joint. Players enter nothing extra; it is computed in the
leaderboard view.

**Golden Goal: PARKED 2026-11-18** (was the tie-break from 2026-11-16: call the minute of the first
goal across the six). Parked because it only split weekly ties with no prize, added a required
input, and tied results to late-logged goal minutes and two clocks. The FHA Model would never make
a Golden Goal call anyway (it plays only to be measured). If revived, it is one new migration: a
`round_entries` table (player, round, minute; same RLS as `picks`) plus `first_goal_minute` and
`clock_max` on `rounds`. Facts kept for then: league minutes run 1–70 (Finals Weekend, Altius,
1–60; never mix in one round); the earliest goal across six is by minute 5 in 82% of rounds, so it
leaves many ties; settle only after `backfill-events`; all six 0-0 means joint winners.

## Scoring rule backtest (2026-11-16)

`analysis/game_scoring.py`, report `analysis/reports/game_scoring_2026-11-16.txt`. 390 Prem
matches (2024/25 and 2025/26) in 65 rounds of 3 Men's/Open + 3 Women's, scored with the FHA
Model's week-by-week out-of-sample predictions, a simulated crowd of 2,000 players at different
skill levels, and fixed habits.

- **Hockey pushes every rule towards "favourite to win 2-1".** Margins of one are the most
  common (28% of results; 16% are draws), and exact scores land about 6–7% of the time
  whatever you do. Under Super 6's 5 exact / 2 result, "favourite by one" scores *more* than
  the FHA Model at its best. A game where a one-line habit beats the analysis is the wrong game.
- **Chosen (2026-11-16, user's brief: simple, more for the exact score, a reward for nearly
  exact, and a tight leaderboard over perfect separation): exact score 5, one goal off 3,
  right result 2.** "One goal off" = right result with one side's score out by one (3-1 for a
  4-1). It is Super 6 with one middle tier, so it explains in a line. Skill ranking 0.83 (Super
  6's rule 0.79), ties in the top 20 35% (45%). The sharpest players land exact 6%, one goal off
  11%, result only 43% of the time.
- **Middle tier: "one goal off", not "right margin"** (tested with the same 5 / 3 / 2). Margin:
  skill 0.81, ties 43%, and it rewards "favourite 2-1" for every one-goal win (the commonest
  result), so the habit draws level with the FHA Model. Either-or: skill 0.84 but ties 40% and a
  two-part rule.
- **Tightness barely depends on the rule.** Over a season the leader is 3–5% ahead of 10th and
  7–9% ahead of 50th under every rule tried, so a tight table comes for free in hockey. What the
  rules change is ties: plan a tie-break (Sky has the same problem; its top 15 has four players
  level on 41 results / 20 scores / 182).
- Considered and set aside for being harder to explain: "score 8; result 3, +3 right margin, +2
  margin one goal out" (skill 0.85, ties 17%, the best on paper), and margin-bonus versions of
  the user's "score 6 / result 3, +1 margin" (0.80, ties 28%). Full table in the report.
- The likeliest Poisson score calls draws in close games and costs 25–35 points a season;
  `best_picks()` maximises expected points instead. **Not used for the FHA Model's entry**
  (user, 2026-11-17): it plays as the simulation model, result first then scoreline.
- Caveats: simulated crowd, not real players; rounds are drawn from Prem weekends rather than the
  actual Phase 2 pairing; two seasons.

## Instagram graphics (in design, 2026-11-17)

Everything is on the design canvas https://claude.ai/artifact/MZTyWqyA4126o2UFkYJBB8 (the canvas is
the source of truth; generator scripts lived in a session scratchpad and are gone).

**Settled**
- **Logo: "S1", the stepper mark.** Two ?-? score tiles (navy, sage) with up/down arrows above and
  below each, like the pick screen's steppers. Lockup FHA (small, spaced, sage) / PREDICTOR (Big
  Shoulders 900) / mark. No "Weekly". Full sheet on the "Picked" board.
- **Player icons:** Admin = the user, shown as the FHA circle logo
  (`brand/brand_icon_circle_no_border.png`); the FHA Model = "M3", a sage circle with a navy line
  chart on an L-shaped axis ("FHA Model icons" board). Guest: later.
- **Round slide rules (user, 2026-11-17):** house colours exactly as `content/brief/BRIEF.md` (no new
  slates/sands: Fable's first "modern takes" row invented some and was rejected as "weird colours");
  one background, never half navy / half paper, Men's/Open and Women's told apart by small labels;
  low contrast between elements (tonal row panels, like Hot Starts), not big colour blocks; club
  badges by every team (`assets/badges-hq`, 200px copies uploaded to the canvas); logo top left and
  "ROUND N" on the same top row, fairly big; pick columns **FHA Model / Admin / Guest** (guests
  planned), icons in the column headings. Title "Admin vs the FHA Model" dropped as the headline.
- Fixtures and picks on every mock are placeholders (real Prem club names, invented picks).

**PICKED 2026-11-17: "Depth A" (bottom row of the canvas).** Navy at the top for Men's/Open fading
to paper for Women's, the fade landing in the gap between divisions so no text sits on the mid tone;
raised cards (navy / paper); flip-style score tiles with a split line and solid edge (sage = FHA
Model, navy = Admin/Guest); a faint sage pitch in perspective behind; grain over everything;
"ROUND N" extruded in sage. Header layout unchanged. User asked for "less static, more punchy / 3D".
- **Pilot rounds 10 and 11 (Phase 1): no Guest column and no footer** ("Depth A · Pilot" artboard).
  So the pilot is on: it runs on the last two Phase 1 rounds.
- Generator: `content/predictor/round_slide_mocks.py <root>` writes the canvas artboards
  (placeholder fixtures in `MENS` / `WOMENS`; badges are canvas blob ids).
- **Crests: cutouts, 54px, with a soft drop shadow** (user, 2026-11-17: larger, no white square).
  `analysis/graphics/crest_cutouts.py assets/badges-hq assets/badges-cutout` makes a `navy/` and a
  `paper/` version of every crest (contact sheet `assets/badges-cutout/_sheet.png`). White
  backgrounds are flood-filled from the edge only; dark mono crests (Surbiton, Bedford) are
  reversed for navy; dark-edged crests get a thin paper outline on navy; logos that are a coloured
  square keep it, corners rounded. Men's/Open rows use `navy/`, Women's `paper/`. Source flaws to
  fix by hand: Old Cranleighan (white strip on the right), Newcastle Uni (grey line on top).
- Fixed in the Depth mocks: the logo's inner navy tile is now #26384C; the small "FHA" sits on navy.

**Earlier candidates (superseded by Depth A)**
- Mine: "Round slide" Paper / Navy / Soft cards (same table layout, three grounds).
- Fable, updated brief: Tonal rows (navy, closest to the feed), Centred (navy, borrows the football
  pick screen in `content/references/predictor-pick-screen-pl.webp`), Scoreboard (paper), and a
  Scoring slide (navy, Tonal-rows style).
- Earlier rows (split table, cards, side by side; Fable's off-palette modern takes) are superseded.

**Open decisions**
1. Subheading: none on the Depth mocks (user cleared it on Paper); confirm.
2. Scoring slide copy: Fable's three rows, "HOME WIN" worked example (pick 2-1; ends 2-1 = 5,
   3-1 = 3, 4-0 = 2), "Pick all six before the first match starts." Restyle in Depth A; approve or cut.
3. Guest icon (not needed until guests play; the pilot has no Guest column).
4. Round 10 six PICKED 2026-11-17, FINAL 2026-11-18 (user swapped East Grinstead v Loughborough
   Students for Reading v Nottingham Uni): Men's/Open East Grinstead v Oxted, Holcombe v H&W,
   Bowdon v Exeter Uni; Women's Durham Uni v Teddington, Reading v Nottingham Uni, Wimbledon v
   Birmingham Uni (ids 6525 6528 6526 7133 7136 7134, as in Supabase). Deadline = Durham, 14:00 UK.
   Any FHA Model picks made for the 17 Nov six need re-running to include Reading v Nottingham Uni.
   FHA Model picks from `analysis/fha_model_picks.py <round_card json>` (run in the jupyter
   container after `round_card.py --json`); re-run once rearranged games before the round are in.
   Still needed: the admin's picks, then the slide.

## Before launch (not code)

- **Name** settled (FHA Weekly Predictor). **Logo** picked (see Instagram graphics below).
  **Domain:** `thefieldhockeyanalyst.com`, registered on Route 53 (expires 2027-10-06; as of
  2026-11-17 transfer lock on, auto-renew still off by choice: turn it on before Oct 2027). Registration stays on Route 53; **DNS moved
  to Cloudflare** (free, and lets Cloudflare Pages serve the bare domain): nameservers
  `malcolm.ns.cloudflare.com` / `sofia.ns.cloudflare.com`, switched and Active on Cloudflare
  2026-11-17 (zone has no records yet); the Route 53 hosted zone was deleted 2026-11-17. Host the site on **Cloudflare Pages**
  (free, no traffic cap). The OTP sender's SPF/DKIM records go in Cloudflare DNS.
  **Placeholder live 2026-11-17:** `web/` is a Cloudflare Worker with static assets (`fha-site`,
  Cloudflare's current recommendation over Pages; same free tier) serving `web/public/` on the bare
  domain and `www` (custom domains, DNS records made by wrangler). The game's built site replaces
  `web/public/`. The dashboard can't show the page's files (static assets have no code view); use the repo.
  **Deploys are automatic (Workers Builds, connected 2026-11-18): GitHub `main` is the master copy.**
  A push to `main` that touches `web/` makes Cloudflare clone the repo and run `npx wrangler deploy`
  from `web/`; live in about a minute (first one 59s, commit 1b408c7). Settings: root/path `web`,
  build command empty (add `npm run build` here once the game has a build step), deploy command
  `npx wrangler deploy`, preview builds off, API token auto-created and held by Cloudflare. Check a
  deploy under fha-site → Deployments (row tagged Build + main). Manual `cd web && npx wrangler deploy`
  still works (needs `npx wrangler login`) but would put live something not in git: avoid.
  `web/.wrangler/` is local cache, gitignored. Build watch path: include `web/**` (not `web/*`, which
  may only match files directly in `web/`), exclude empty; pushes elsewhere don't build. The
  Cloudflare GitHub app can see only `field-hockey-analyst` (set 2026-11-18).
  **Cost: free, and must stay free (user, 2026-11-17).** No card on the Cloudflare account.
  Free plan covers it: static asset requests are free and unlimited, DNS and custom domains free,
  Workers Builds 3,000 build minutes/month. Don't add anything that needs Workers Paid.
- Workers Builds was picked over GitHub Actions (no API token to keep as a GitHub secret).
  **Handle** still needed before the build is public.
- **Scoring rule**: locked, exact 5 / one goal off 3 / result 2 (above). Tie-break: more exact scores,
  then more one-goal-off picks, round winner only; season ties stay joint (2026-11-18; Golden
  Goal parked).
- **Privacy:** it collects emails, so it needs a privacy notice (UK GDPR) and account deletion.
- **No prize** on the first run, which keeps it clear of competition rules.
