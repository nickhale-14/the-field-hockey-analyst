# Modelling — understanding the predictor and the ratings

Read when working on the model, or before a modelling / stats learning session.

**What goes where (so nothing is duplicated):**
- **This file** — the *understanding*: how and why each method works, what the
  parameters mean, what he's decided to trust or not, open questions to chase in
  the next session.
- [`docs/findings.md`](findings.md) — the *facts*: what's built, the numbers it
  produced, the accuracy results. Don't restate those here; link to them.
- The code: [`analysis/predictor.py`](../analysis/predictor.py) (Poisson
  attack/defence + backtest), [`analysis/prem_elo.py`](../analysis/prem_elo.py)
  (the power rankings the user trusts), [`analysis/elo.py`](../analysis/elo.py)
  (the earlier tier-anchored Elo, **parked** — "not a good reflection").

## The planned session

A proper walk-through of the predictor and the ratings, end to end. **The Elo
mechanics half happened on 2026-10-15 (see Understanding below);** the Poisson
predictor half, and the later-stage frontier, are still to do. Candidate things to
actually understand (not just know the result of):

- **Why Poisson** for goals, and what attack/defence strengths per team really
  represent once shrunk to tier means.
- **Home advantage as a multiplier** (×1.146 men / ×1.071 women) and why it lands
  where it does; why the women's game needs a faster rating and a smaller home edge.
- **Time-decay / half-life** — why men ≈ no decay but women half-life 0.5y
  (squad turnover), and how refitting weekly buys +6–7 points of accuracy.
- **Why the model never picks a draw**, and what that costs (the ~14–17% drawn
  games as automatic misses).
- **Elo vs Poisson** — the ordered-logit layer that turns a rating gap into
  P(home)/P(draw)/P(away), why the restricted-to-one-division pool fixed the
  distrust, and whether the parked `elo.py` is worth reviving or retiring.
- **The scores-only ceiling** — log-loss stopped improving; the next gain needs
  new *kinds* of information (international windows, squad availability), not more
  seasons of the same data (the 2019–22 ablation showed older history barely helps).

## Power rankings calibration: done 2026-10-20

`prem_elo.py` is fixed and re-tuned; numbers in [`findings.md`](findings.md). What changed:

- **Everyone starts level:** the model starts in 2021/22 (`FIRST_SEASON`) with every side on 1500.
  2021/22 is burn-in: the result translator never learns from it (`logit_rows`).
- **Seeding fixed (`Ratings.new_season(teams)`):** each summer relegated sides leave the pool, the
  rest regress toward last season's average, and **any side not in last season's Prem** (promoted,
  or back after time away) starts at average + `promoted`. A returning side's old rating is ignored.
  `simulate.py` calls `new_season(teams)` for its pre-season path; `_seed` is gone.
- **Promoted offset: men −90 (kept), women −150.** Tuned on what it actually affects: promoted
  sides' Phase 1 games 2022/23–2026/27 (92 per gender). Men best −60 to −100, women −140 to −160.
- **Home 0 in both genders.** See the home lessons below.
- **Symmetric translator (`fit_ordered_logit(symmetric=True)`):** equal sides get equal home and
  away chances. Worse in no test season; men's 2023/24 log-loss 0.645 → 0.624.
- **Checked and kept:** k (12 / 32), margin (1.5), summer pull (0.75). Best alternatives gained
  0.001–0.003 log-loss with the test seasons disagreeing on direction, so noise.
- **Not re-centred** (decided 2026-10-20): predictions are identical either way, "everyone started
  on 1500" is true without it, and it would add a rule to the explainer. The average drifts up a
  little each summer (2026/27: men 1523, women 1514) because relegated sides leave further below the
  average than promoted ones arrive. Can be added later without changing any prediction.
- **Squad continuity tested, not adopted:** a per-side summer pull based on how much of the squad
  returned gained nothing, even with hindsight (findings.md). The Elo is at its ceiling on the data
  we hold; the 50/50 blend with the score model is the remaining proven gain, for The Call.
- **Older data not collected:** Flashscore has men's Prem back to 2009/10 but women's only from
  2019/20. Would add ~20 men's promoted sides, from a different era (10 teams, no split). Worth it
  for history content, not for the model. If it ever goes in, the men's score model (no decay)
  needs a start date first.

**Post rebuilt 2026-10-20** on the new model, **posted 2026-10-21** (`content/backlog.md`, Power Rankings).
The slide 1 "returning sides drift" story no longer exists: returning sides get the promoted seed
(Exeter M and Bowdon M both start 2026/27 on 1426).

**Explainer post, in order (the user's idea, 2026-10-20):**
1. One pool: Prem matches only, so every side has played every other.
2. Everyone starts level (1500, 2021/22). Answer "why 1500?" here (the first thing a test reader asked):
   it is the chess convention, and only the gaps matter; starting on 1000 would shift every rating by 500
   and change nothing else. The Power Rankings post leaves the number out for that reason.
3. Each result moves points from loser to winner. The more surprising the result, the bigger the
   move; a bigger margin adds more, but tails off (k 12 men, 32 women: the women's game turns
   over faster).
4. Each summer, every rating moves a quarter of the way back to the average.
5. Promoted sides join below the average.
6. A rating gap becomes home/draw/away chances (the ordered logit), so it can be checked:
   2025/26 men 68.6% / 0.761, women 63.6% / 0.833 (findings.md). Venue is ignored: there is no
   measurable home edge in the Prem, so the ratings and the translator both leave it out.
   A 100-point gap, from the translator fitted on 2022/23-2025/26: Men's/Open 72% win / 16% draw / 13% loss
   (70% observed, 80 games); Women's 54 / 23 / 23 (54% observed, 70 games). Women's ratings spread wider
   (k 32 v 12), so the same gap means less; say so if both numbers appear together.
7. It isn't the league table, and that's the point. After Round 4, rank correlation was 0.75 in
   both divisions. H&W W were 4th in the rankings but 11th in the table (0-0-4, three one-goal
   losses); East Grinstead M were 6th against 2nd.
8. Where it sits (asked publicly 2026-10-21): the same family as **Opta's Power Rankings** (Elo; Opta adds
   xG, links every league worldwide and rescales to 0–100 with the top team on 100), and unlike the
   **NCAA NPI**, which is RPI-family: one season's record plus strength of schedule, recalculated from
   scratch to a fixed point, committee-set weights (often 25% result / 75% opponent), bad results can be
   dropped, built for tournament selection, not prediction. A loss to a strong side can raise an NPI.

## Next steps (left at 2026-10-17)

1. ~~Mark Round 4~~ done 2026-10-18 (`reports/round4_card_2026-10-17.txt`): men's both 6/6; women's Elo 3/6,
   Poisson 5/6. Rounds 3+4 tally: Elo 16/23 (log-loss 0.64), Poisson 18/23 (0.61). Too few games to judge.
   ~~Run a proper Prem-only head-to-head~~ done 2026-11-07 over 2023/24-2026/27 (`compare_models.py`, findings.md):
   "Elo is much better calibrated" does not hold; Elo leans better Men's/Open, Poisson Women's, blend safest.
2. **Re-run the rankings and Top 6 odds** after that import (`prem_elo.py --rankings`,
   `simulate.py --csv ...`), then build the Top 6 odds post and the first weekly Power Rankings.
3. **The Call / The Verdict** now have their pipeline: `round_card.py` before a round (default
   as-of = day before) and again after it with the same as-of. Save the "before" output to
   `reports/` before the first kick-off if it's going to be posted.
   Rounds 5-6 (24-25 Oct) saved 2026-10-19: `reports/round5-6_predictions_2026-10-19.txt`.
   **Graphic layout uses both models** (2026-10-19, user's idea): Poisson's likeliest score plus an Elo
   home/draw/away bar, a "Split" tag where they back different teams. That sidesteps picking one model
   for publication until the Prem-only head-to-head (item 1) settles it. A drawn likeliest score is
   common (6 of 24 fixtures that weekend) and is not a disagreement. Design notes: `content/backlog.md`.
4. Simulator Phase 2 (top-8 finals and relegation odds): Phase 2 fixtures don't exist until the
   split, so they'd be generated inside the simulator, venues unknown.
5. Travel distance as a home-edge input: low value for the Prem now (no home edge to explain),
   possibly useful for the lower-tier score model. Squads were tested and are too weak for now
   (findings.md). **International call-ups are not a candidate:** the league breaks for
   internationals, so no league game is played without its internationals (user, 2026-10-20).
6. Lower-tier squads backfill, politely (`--pause`), only when needed.

## Understanding

### Design philosophy (session 2026-10-15)

Start simple, build a strong statistical baseline, add complexity **only when
chronological backtesting proves it helps**. Every parameter (`K`, offseason
regression, home advantage, promoted prior, scale, any margin adjustment) is
*tuned from historical seasons with rolling/chronological backtests*, never chosen
by feel. The rough build order discussed: reproducible pipeline → naive baselines →
Premier-only Elo → Poisson score model → time decay → promoted/relegated handling →
then Dixon-Coles / bivariate / negative-binomial → event-level goal/card modelling →
ML challengers → ensembles → eventually in-play win probability.

**Where the repo already is against that order:** most of it is built and, on the
Elo side, ahead of the "V1" sketch below — [`analysis/predictor.py`](../analysis/predictor.py)
is the Poisson model with time decay and promotion/relegation offsets, and
[`analysis/prem_elo.py`](../analysis/prem_elo.py) is a Premier-only, margin-damped,
season-regressed Elo with promoted-team seeding **and an ordered-logit layer giving
real P(home)/P(draw)/P(away)**. So the material below is the mechanics understood
from first principles, not a fresh to-build list; the open frontier is the *later*
items (Dixon-Coles, event-level, ML/ensembles, in-play). See [`findings.md`](findings.md)
for the accuracy numbers.

### Why the old lower-division Elo drifted

Confirmed the cause of the distrusted `elo.py`: when leagues are mostly
disconnected (teams rarely cross divisions), their rating scales never get tied
together, so they drift independently and a Conference team can end up rated above
a Premier team. Fixes: keep Elo **within one competition** (what `prem_elo.py`
does, and why it works), or use explicit division-level priors / transition
adjustments. Pretending all leagues share one naturally calibrated scale is the
mistake.

### Elo mechanics (the first-principles walk-through)

- **Baseline (e.g. 1500) is an arbitrary convention** — only the *difference*
  between two teams' ratings matters.
- **Scale (e.g. 400)** sets how sensitive the expected result is to a rating gap:
  larger scale → gaps matter less, smaller → more.
- Expected score: `E_A = 1 / (1 + 10^((R_B − R_A)/400))`.
- Result scored win 1 / draw 0.5 / loss 0; update `R_new = R_old + K·(S − E)`,
  where `K` sets reaction speed.
- **An expected score of 0.7 is NOT a 70% win probability** when draws exist — it
  is the expected *result score* under win/draw/loss. (This is exactly why
  `prem_elo.py` adds the ordered-logit layer to get real P(H/D/A).)
- Built over time: process matches chronologically, update both teams after each,
  carry ratings into the next fixture.
- **Season boundary:** regress toward the league mean rather than reset —
  `R_new = 1500 + α·(R_old − 1500)`, α tuned (≈0.75 as a starting example).
- **Home advantage:** boost the home rating by `H` before computing expectation —
  `E_home = 1 / (1 + 10^((R_away − (R_home + H))/400))`, `H` tuned (≈50 to start).
  Team-specific home effects are possible but too noisy early unless shrunk toward
  the league-wide effect.
- **Margin of victory:** basic Elo treats 7–1 and 3–2 identically. A multiplier
  `M` by goal difference (with diminishing returns, e.g. logarithmic) helps, **but
  must also correct for the pre-match rating gap** so strong teams can't farm Elo
  by thrashing weak teams they were already expected to beat.
- **Promoted teams:** don't start them at the Premier average and don't carry over
  raw lower-division Elo. V1 is a tuned promoted-team prior `P`. For clubs
  *returning* to the Premier, blend old Premier Elo with that prior —
  `R_return = P + β·(R_old − P)`, β shrinking the longer they've been away.

Provisional "V1" Elo (all starting values, to be tuned by backtest): baseline 1500,
scale 400, K 20, offseason α 0.75, home `H` 50, promoted prior `P` tbd, no
margin-of-victory term at first (clean baseline). Note `prem_elo.py` already moves
past several of these defaults.

### Where the power rankings stop working (measured 2026-09-28)

The phase-by-phase accuracy table lives in [`findings.md`](findings.md); this is
*why* it reads the way it does. Two distinct causes, worth keeping separate:

- **Phase 2 costs 5 points (men) to 10 points (women).** During the split the Top
  6 only play each other and the Lower 6 only each other, so nothing re-links the
  two brackets and any error in their relative level persists until Phase 1 of the
  next season. This is missing information, not a fixable bug. The 2026/27 format
  *helps*: the top-8 quarter-finals mix Top 6 with the best two of the Lower 6.
- **Knockouts are where it fails outright.** One-off games between well-matched
  sides, on small samples (8 matches a season): at Finals Weekend the model is
  confidently wrong — worse than a flat guess — consistently across both genders.
  **Decision: publish rankings, not knockout predictions.**

The phase *structure* otherwise needs no special handling. Elo rates matches, not
tables, so uneven schedule strength is handled by design — which is exactly why it
rates East Grinstead above Oxted where the bracket does not (see
[`data-scope.md`](data-scope.md) on bracket vs points).

### What the 2026-10-17 re-tune taught (numbers in findings.md)

- **The home-advantage setting barely matters, and that's expected.** Across 0–120 the
  log-loss moves by about 0.002 (best at 0) in both genders. The ordered-logit translator
  learns its own home lean from history (its two cutpoints aren't symmetric), so the
  *probabilities* get the home edge either way; the setting only changes how much credit a
  home v away win earns in the update. Home advantage is real; this dial just isn't where it
  lives. The earlier worry that 50 and 15 sat at the edge of the tested range is resolved.
- **The scale (400) never needs tuning:** doubling it equals doubling every rating, K, home
  and promoted together, and the translator rescales to match. K covers it.
- **Re-tuning fitted noise.** Better on the tuning seasons, worse on the held-out one. The
  surface is flat near the current settings, so the honest reading is that this model has hit
  the scores-only ceiling, the same conclusion the Poisson model reached. Gains now need new
  information, not new dial values.
- **Don't add per-phase settings** (Phase 2 K, dead-rubber weights): about 30 Phase 2 games
  per gender a season is too few to tune one without fitting noise. The split costs accuracy
  because it stops information crossing between brackets, which no setting can restore.

### What the 2026-10-20 calibration taught

- **Judge on log-loss, not accuracy.** Accuracy is "did the likeliest outcome happen", and a draw
  is never the likeliest (two equal men's sides: 37 / 25 / 37), so every draw is a miss. That's
  normal: football models and bookmakers almost never favour the draw either. Log-loss (or RPS)
  scores the probabilities, which is the real test.
- **Draw chances:** women's are honest (said 71.5 draws in 2023/24–2025/26, 63 happened, every
  band within noise). Men's are low: said 39, happened 50, mostly in mismatches the model gives
  under 10% (happened ~7%). Two other draw curves were tested out of sample (Davidson, a separate
  draw-v-gap term) and neither beat the ordered logit; the shortfall is the level of draws the
  fitting seasons happened to have, not the curve's shape. Kept as is.
- **The home dial never controlled home advantage.** Equal sides got the same chances at home 0 or
  50, to 0.1%, because the translator learns its own home lean. The setting only changed credit in
  the update, and the Prem has no edge to credit. Freeing the translator's home lean then turned out
  to be the actual cost: it learned noise from short windows, hence the symmetric fit.
- **Women's promoted sides start further down the order, but men's face longer odds.** −90 is 0.70
  standard deviations of the men's pool (a promoted side wins 14% v an average side); −150 is 0.89
  of the women's (wins 18%), because the women's game is less predictable. Women's promoted sides
  also vary far more (Durham ended Phase 1 above average, Sutton Coldfield 324 below).
- **The offset is the dial that matters.** On the old full-season sweep it moved men's log-loss by
  0.055 across its range, against about 0.005 for home. Score it on the games it affects (promoted
  sides' first half-season), or the other ~90% of games dilute it.

### Naming and the explainer post (decided 2026-11-13)

In published content the whole system (The Call, The Verdict, Breakdown top 6 odds, the simulator) is **"the FHA Model"**, "FHA Model" in captions. Never "our model", and posts never name Elo or Poisson. A future explainer post says how it works, as short factual declaratives (no build-up, no 1500 anchor):

- Elo power ratings pick each result; ratings update inside a simulated season, so uncertainty about the ratings gets in.
- A Poisson attack/defence model picks the scoreline, which goal difference needs.
- The season is played out 20,000 times; each figure is the share of those seasons ending a given way.

The explainer may be the one place the method is described; whether it names Elo and Poisson is its own decision. Backlog entry: content/backlog.md.

### How the two models combine into one call (session 2026-10-20)

- **They answer different questions.** Elo: who has been winning, and against whom (results only).
  Poisson: how many goals each side scores and concedes (attack x defence x home edge -> expected
  goals -> a probability for every scoreline). They disagree most when results and goal records
  tell different stories.
- **Score v confidence are two readings of one grid.** The score model's scoreline grid gives both:
  the likeliest score is its single biggest cell; the win chance is the sum of the cells on one side
  of the diagonal. So the likeliest score can be a draw while the region leans to a side.
- **The combined call:** average the two models' home/draw/away 50/50, back the likelier side, show
  the likeliest score *in which that side wins* (the rule simulate.py already uses). Score and
  pick can't contradict. Backtest numbers in findings.md (blend at least as good as either alone).
  Other ways considered: a winning margin ("by 2+, 71%"), expected goals ("3.4 v 1.1"), top three
  scores; kept for The Breakdown, not The Call.
- **Stakes for a single fixture (The Breakdown, since 2026-11-11):** `simulate.py --stakes` splits the same
  simulations by one fixture's result and gives each side's top 6 chance under each. The default 20,000 runs
  leave about 7% of them on an unlikely result, so the figures wobble by a point; pool several seeds of
  `simulate(prep, 200000, seed=..., outcomes=res)` for published numbers (Round 9 used 1,000,000). The CLI has no
  seed flag, so `--stakes` is always seed 0 and won't match a pooled run exactly. A big swing
  usually means the model rates that result unlikely: H&W v Reading was 82 / 11 / 7, which is why Reading's
  12% became 75% with a win. Say so if asked; the match odds themselves stay in The Call.
- **The Men's/Open translator is steep (checked 2026-11-13):** rating gap 100 -> 71 / 16 / 13, 200 -> 91 / 6 / 3,
  300 -> 98 / 2 / 1 (home 0). So Reading (1428) get ~1% at home to Wimbledon (1685) and under 1% to Surbiton
  (1798). The Phase 1 backtest says the top 6 calls are calibrated, but single-game figures at big gaps look
  harsh; don't quote them. Conditioning a simulation on one side's final points total ("Reading's route":
  14 pts 19%, 15 pts 52%, 17 pts 98%) is a cleaner way to show a run-in than per-game odds.
- **Worked example, Buckingham W1 v Nottingham Uni W1 (R5, 24 Oct), as of 23 Oct:** Elo 1284 (12th)
  + 15 home v 1305 (11th), gap -6 -> 37 / 25 / 38 (post-calibration seeding; the frozen Call had
  41 / 22 / 37). Poisson: average 1.43 goals; Buckingham attack x0.94, concedes x1.24 (promoted
  offset included), Nottingham attack x1.19, concedes x0.83, women's home x1.23 -> xG 1.36 v 2.11
  -> 24 / 21 / 55, likeliest 1-2 (9.4%, 1-1 9.0%). Combined 31 / 23 / 46 -> Nottingham, 1-2. Elo sees
  two sides that have lost almost everything; Poisson sees Buckingham conceding 25 in four.
- **Gotcha when reproducing by hand:** `fit.rates()` needs `home_move` / `away_move`, or a promoted
  side loses its first-season offset and the numbers won't match round_card.py.

### Open decisions on the ratings

- **Re-centring the pool — decided against, 2026-10-20** (see the calibration section; the numbers
  below are from the old seeding). Elo is zero-sum
  within a season, but promotion and relegation leak rating mass: relegated teams
  leave with their ratings and promoted ones enter at mean−90. The pool mean has
  fallen 1236 → 1190 (men) and → 1181 (women) across seven seasons. Harmless for
  ordering within a season; it means ratings are **not comparable across eras**, so
  no "highest-rated team ever" claims. Fix is to re-centre each season to a fixed
  1500, which would also make published numbers read better. Not applied: the user
  wants to understand the model first (2026-09-28), and it changes every number.
- **FIXED 2026-10-20: the first season was seeded as if every side were promoted.**
  `_seed` gives a side it hasn't seen "pool mean + promoted (-90)", and that includes the opening
  season, when nobody has a rating. So the first side in the first fixture gets 1410, the next 1320,
  then 1275, 1245 ... down to ~1150, purely by fixture order (2019/20 men: Beeston 1410, H&W 1320,
  ... Exeter 1155). Sides did not start level. It is also most of why the pool average sits near
  1227 rather than 1500. It washes out: starting the model in 2021/22 (different arbitrary seeds)
  gives the same 2026/27 order with ratings within ~4 points. Fix: every side in the first season
  starts at 1500; only sides missing from the previous season get mean - 90 (this also fixes the
  returning-side quirk below). **Scheduled: see "Power rankings calibration" above.**
- **FIXED 2026-10-20: returning sides skipped the promoted prior.**
  `_seed` only seeds a team with no rating. A side that was in the Prem before
  comes back on its old rating, which has been pulled toward the mean once for
  every season away. So the longer a side is away, the closer to average it comes
  back. 2026/27: Exeter M (away three seasons) started 1164, 27 above the
  promoted seed (mean−90 = 1137), 6th. Bowdon M (away one season, went down on
  1060) started 1117, 20 below the seed, 12th. Small effect here, because 6th to
  11th started within 47 points of each other. Possible fix: don't regress teams
  outside the pool, or seed every newly promoted side at mean−90.
  Bowdon M's three 2024/25 relegation play-off wins are real results, not bad
  data: they were already relegated after Phase 2 (user, 2026-10-18). Those
  matches still count in the Elo.
- **Neutral venues:** `home_factor` only neutralises Finals Weekend; relegation play-offs are also
  at neutral venues. Moot while home is 0. If a home setting ever comes back, mark play-offs
  neutral the same way (a hosting club's own side keeps the edge).
- **Whether to publish probabilities at all, or only the ranking.** Knockout
  behaviour above is the argument for rankings-only.
- **`prem_elo.load()` filters on `'%Premier Division%'`.** When the 2026/27
  two-legged quarter-finals appear, if England Hockey names them without that
  string they are **silently dropped** from the ratings. Analysis-side twin of the
  `TARGET_COMPETITIONS` risk in [`pipeline.md`](pipeline.md); check it when the new
  competitions first show up.

### Direction

Treat the Instagram account (@thefieldhockeyanalyst) as the **main practical
output**: each modelling improvement should directly generate content (predictions,
team strength, home advantage, promoted-team analysis, goal timing, card effects,
score-state trends). A paper, if ever, emerges later from work already done rather
than as a separate project. While job-hunting, prioritise work that either improves
the prediction engine or produces public-facing content.
