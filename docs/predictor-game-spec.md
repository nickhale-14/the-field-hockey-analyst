# Predictor game: spec (v1, Phase 2 2027)

What the game must do and how we'll know it worked. The how (stack, tables, feed, build order)
is in [predictor-game.md](predictor-game.md). Name: **FHA Predictor** (full form FHA Weekly
Predictor; settled 2026-11-16).

## Problem

FHA posts are read, not played. Followers have no reason to come back on a set day or to act on
a post, and the weekly Call (the FHA Model's predictions) is a one-way broadcast. English club
hockey has no free score-prediction game, so there is nothing for followers to join and
nothing that turns the FHA Model into something people can compete with.

## Goals

1. **A weekly habit.** Followers come back every Phase 2 weekend to make six calls.
2. **A call to action for Instagram.** Every Call and Verdict post has something to tap
   ("beat the FHA Model").
3. **A content engine.** Each round produces material on its own: the admin vs the FHA Model,
   the round's best player, how the crowd did against the Model.
4. **Zero extra weekly work.** Rounds open and score from `weekly.sh` with no manual steps.
5. **A base for 2027/28.** The same game and accounts carry over to the full season and the
   iOS app.

## Non-goals (v1)

- **Prizes.** Avoids competition rules and cost on a first run.
- **Leagues between friends / private groups.** Worth having, but a P2 once there are players.
- **Divisions below the Prem.** The six Prem matches a weekend are the game; more fixtures
  would dilute it and the data is thinner.
- **Germany or other leagues.** Separate audience and database.
- **iOS app.** Website first; the app comes after the web version has proved itself.
- **Picking which fixtures are in a round.** A round is always six matches. Phase 2 has 12 Prem
  matches a weekend (6 per gender), so rounds alternate Top 6 week and Lower 6 week, 3 Men's/Open
  + 3 Women's each time; no editorial step.

## User stories

**Follower (first visit, from Instagram)**
- As a follower who taps the link in a post, I want to make my six calls without signing up and
  get an image of them to share, so that trying it takes under a minute.
- As a first-time player, I want to see when picks lock (in my own time zone) so that I don't
  miss the deadline.
- As a first-time player, I want to keep my picks and get scored by adding an email before the
  deadline, so that I don't lose what I've already done.

**Returning player**
- As a returning player, I want my saved picks shown when I reopen the round so that I can
  change one call without re-entering all six.
- As a player, I want to see my points per match after the weekend so that I know where I won
  and lost.
- As a player, I want to see where I rank this round, this season and all-time, and whether I
  beat the admin and the FHA Model.
- As a player, I want to share my six as an image so that I can post it.
- As a player, I want to delete my account and data.

**Edge cases**
- As a player whose match is postponed, I want to know it scores zero for everyone so that it
  isn't unfair to me.
- As a player who arrives after the deadline, I want to see everyone's picks and the round's
  scores, and when the next round opens.

**Admin (Nick)**
- As the admin, I want rounds to open and results to score from `weekly.sh` so that the game
  costs me no extra time each week.
- As the admin, I want to play as myself on the leaderboard so that "Admin vs the FHA Model"
  runs inside the game.

## Requirements

### P0: cannot launch without

| # | Requirement | Acceptance criteria |
|---|---|---|
| 1 | Round page with six matches and score entry | Shows 3 Men's/Open + 3 Women's matches, badges, kick-off in viewer's time zone, a whole-number score per side (0 to 15). Save needs all six scores. Save works on a phone. |
| 2 | Play without an account | A new visitor can make six calls without entering anything, and gets a share image of them. They are not scored and not on any leaderboard. |
| 3 | Sign up to be scored | Given an anonymous player with saved picks, when they confirm an email (6-digit code) before the deadline, then the same picks belong to the new account and are scored. Confirming after the deadline does not bring that round's picks in. |
| 4 | Deadline lock | Given the round's first kick-off has passed, when anyone tries to save or change a pick, then the database rejects it (not just the page). The countdown uses server time, not the phone's clock. |
| 5 | Picks private until the deadline | Before the deadline no one can read anyone else's picks, including through the API and the leaderboard views. After it, all are public. |
| 6 | Automatic rounds and scoring | `weekly.sh verdict` enters results, scores the round and opens the next one with the FHA Model's picks, as soon as results are in and the next fixtures are known. No manual step. |
| 7 | Scoring rule | Exact score 5, right result and one goal off 3, right result 2, wrong result 0 (locked 2026-11-16; see predictor-game.md). Computed in one place in the database. Postponed or abandoned matches score 0 for everyone. |
| 8 | Leaderboards | Round, season and all-time tables, signed-up players only; the admin and the FHA Model always shown and labelled; your own row highlighted. Round winner: ties separated by more exact scores, then more one-goal-off picks, else joint; season ties stay joint. |
| 9 | Results view | Per match: your pick, the score, your points; round total. |
| 10 | Privacy | Privacy notice live; account deletion removes the email and picks. |
| 11 | Brand | Men's/Open on navy, Women's on paper, Arimo and Big Shoulders, "the FHA Model" naming, no em dashes, no Elo or Poisson anywhere. |
| 12 | Share image | An image of your six (1080x1350, the feed format), signed up or not. It is the hook for anonymous players. |
| 13 | Corrections | A corrected result re-scores the round. Freezing after a set period is deferred (2026-11-18): a scheduled job driven by the fixtures, designed later. |

### P1: fast follows

- Crowd view after the deadline: how many called each result, the most popular score.
- "How did the crowd do vs the FHA Model" figure for the Verdict post.
- Reminder email before the deadline (opt-in).

### P2: design for, don't build

- Private leagues with friends or club-mates (keep players and leaderboards able to filter by a
  group).
- iOS app on the same backend (Sign in with Apple, push reminders).
- Full 2027/28 season, including Phase 1 weekends with 6 matches per gender (12 fixtures, so a
  round may need picking).
- Prizes, which bring terms and conditions.

## Success metrics

Targets are proposals to set once the follower count at launch is known.

**Leading (first three rounds)**
- Players in round 1 as a share of Instagram followers: success 5%, stretch 10%.
- Visitors who start a round and save all six: 60%.
- Anonymous players who add an email: 30%.
- Rounds needing a manual scoring fix: none.

**Lagging (end of Phase 2, May)**
- Round-to-round retention of signed-in players: 50% play the next round.
- Players in the last round vs the first: at least equal.
- Instagram link taps and follower growth, March to May against September to November.

Measured from the game's own tables (players, picks per round) plus Instagram insights.

## Open questions

| Question | Who | Blocking? |
|---|---|---|
| Domain, Instagram handle | Nick | Yes, before public launch |
| QF rounds: 4 QF legs + 2 relegation per gender, or a different six | Nick | No, needed by April |
| Club badges on a public site | Legal / clubs | No, can launch with names only |
| Do aggregate-score QF ties change what players call | Design | No |

## Timeline

- **Hard date:** Phase 2 match 12, mid-March 2027. If it slips, launch at the next round rather
  than changing things mid-phase.
- **Dependencies:** domain, and the FHA Model predicting Phase 2: today
  `simulate.prepare()` and `round_card.card()` only see Phase 1 fixtures (after Christmas).
- **Phasing:** Dec to Jan build P0; February private test on replayed Phase 1 rounds; March
  launch with P0; P1 items between rounds; P2 over the summer for September.
