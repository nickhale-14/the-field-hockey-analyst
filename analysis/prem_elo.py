"""
Premier Division power rankings: an Elo rating per team, per gender.

Why this exists when analysis/elo.py already does Elo: that one rates every
tier at once, and a Division 1 side that never plays a Premier side can drift
above genuine Premier teams on results alone - which is exactly why the
ratings weren't trusted. Restricting the pool to one division removes the
failure mode completely: everyone in here has played everyone else.

What it is, in order:

1. Elo over every Premier Division match, chronologically, per gender.
   Margin of victory damps the update (a 6-0 says more than a 1-0, but not
   six times more), the season boundary regresses everyone toward the mean,
   and a newly promoted side starts below it.
2. An ordered-logit layer that turns a rating difference into real
   P(home) / P(draw) / P(away). Elo on its own gives a single expected score
   and hockey has draws, so this is what makes it checkable.
3. Tuning, then honest out-of-sample validation against the Poisson model
   (analysis/predictor.py), which gets 66.1% of Premier results right. A
   power ranking nobody can check is just an opinion with numbers on it.

    docker compose exec jupyter python3 /home/jovyan/work/prem_elo.py --gender M --tune
    docker compose exec jupyter python3 /home/jovyan/work/prem_elo.py --gender M --rankings
    docker compose exec jupyter python3 /home/jovyan/work/prem_elo.py --gender M --movement /home/jovyan/work/output/power_M.json

`--league de` runs the same model on the German 1. Bundesliga (its own database, hockey_stats_de;
docs/germany.md): one division again, so the same one-pool logic holds. Settings in TUNED_DE.
"""
from __future__ import annotations

import argparse
import math
import os
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from sqlalchemy import create_engine, text

from predictor import DATABASE_URL

START = 1500.0
# Post-COVID: every side starts level on START here. The first season is burn-in,
# so the result translator is never fitted on it (see logit_rows).
FIRST_SEASON = "2021/22"

# The German 1. Bundesliga: the same model on its own database (never hockey_stats: docs/germany.md).
# Data starts 2022/23, so that is its level start and burn-in. League, play-offs and play-downs are all
# rated (every side in them is a 1. Bundesliga side). Shoot-outs (2023/24 on) count as the drawn
# regulation result they settle: the shoot-out is a points tie-break, not evidence of quality.
DE_URL = os.getenv("DATABASE_URL_DE", DATABASE_URL.replace("/hockey_stats?", "/hockey_stats_de?"))
LEAGUES = {
    "en": {"url": DATABASE_URL, "comp": "Premier Division", "first": FIRST_SEASON,
           "label": "Premier Division", "val": "2024/25", "test": "2025/26"},
    "de": {"url": DE_URL, "comp": "1. Bundesliga", "first": "2022/23",
           "label": "1. Bundesliga", "val": "2024/25", "test": "2025/26"},
}
# k, mov and carry grid-searched on 2024/25 and tested on 2025/26; k re-checked
# on the fixed seeding 2026-10-20 (flat, seasons disagree, kept). The women's
# game needs a fast-updating rating (k 32 v 12, squad turnover in
# university-heavy sides), as the Poisson model found independently.
# Home is 0: the Prem has no measurable home edge (home_advantage.py), and the
# setting never changed the probabilities anyway, since the ordered logit learns
# its own home lean. Men's log-loss was better at 0 than 50 at every k.
TUNED = {
    "M": {"k": 12.0, "mov": 1.5, "carry": 0.75, "promoted": -90.0, "home": 0.0},
    "W": {"k": 32.0, "mov": 1.5, "carry": 0.75, "promoted": -150.0, "home": 0.0},
}
# carry and mov re-checked on the fixed seeding too (2026-10-20): best values
# gained 0.001-0.002 log-loss with the test seasons disagreeing, so kept.
# Promoted offsets re-tuned 2026-10-20 on the fixed seeding, scored on promoted
# sides' Phase 1 games 2022/23-2026/27 (92 per gender): men best -60 to -100,
# women -140 to -160. Women's spread of ratings is wider (k 32), so a promoted
# side sits further below the average in points.


# Checked 2026-10-21 the England way: each setting swept, scored out of sample on 2024/25 and 2025/26
# (translator fitted on 2023/24 to the season before; 2022/23 is burn-in). Adopted only where BOTH test
# seasons improved. Men's log-loss 0.931/0.869 on England's settings -> 0.910/0.844:
#   carry 0.5   a stronger summer pull than England (0.75); best in each season on its own
#   promoted -150  as England's women; best -120 to -200, flat
#   home 20     the 1. Bundesliga men DO have a home edge (+0.35 goals a game, home sides win 57% of
#               decisive league games, 2022/23-now), unlike the Prem; 15-30 flat. Final Four neutral.
#   k, mov      flat, kept.
# Women's: England's settings kept. Every alternative helped one season and hurt the other (carry 1.0,
# promoted -200, k 20); home edge +0.15 a game, not clear of zero, and home 15+ was worse in 2024/25.
TUNED_DE = {
    "M": {"k": 12.0, "mov": 1.5, "carry": 0.5, "promoted": -150.0, "home": 20.0},
    "W": {"k": 32.0, "mov": 1.5, "carry": 0.75, "promoted": -150.0, "home": 0.0},
}


def load(gender: str, league: str = "en") -> pd.DataFrame:
    """Every played match in the league's top division for one gender, oldest first."""
    lg = LEAGUES[league]
    sql = text(
        """
        SELECT m.id AS match_id, s.name AS season, c.name AS competition, m.starts_at,
               m.home_team_id, m.away_team_id, th.name AS home, ta.name AS away,
               m.home_score, m.away_score, v.name AS venue,
               ch.name AS home_club, ca.name AS away_club
        FROM matches m
        JOIN seasons s ON s.id = m.season_id
        JOIN competitions c ON c.id = m.competition_id
        JOIN teams th ON th.id = m.home_team_id
        JOIN teams ta ON ta.id = m.away_team_id
        JOIN clubs ch ON ch.id = th.club_id
        JOIN clubs ca ON ca.id = ta.club_id
        LEFT JOIN venues v ON v.id = m.venue_id
        WHERE m.status = 'R' AND c.name LIKE :comp
        ORDER BY m.starts_at, m.id
        """
    )
    with create_engine(lg["url"]).connect() as conn:
        df = pd.read_sql(sql, conn, params={"comp": f"%{lg['comp']}%"})
    is_w = df["competition"].str.lower().str.contains("women")
    df = (df[is_w] if gender == "W" else df[~is_w])
    df = df[df.season >= lg["first"]].copy()
    df["home_factor"] = ([home_factor(r) for r in df.itertuples(index=False)] if league == "en"
                         else home_factor_de(df))
    df["date"] = pd.to_datetime(df["starts_at"])
    df["outcome"] = np.where(df.home_score > df.away_score, 0,
                             np.where(df.home_score == df.away_score, 1, 2))  # H / D / A
    return df.sort_values(["date", "match_id"]).reset_index(drop=True)


def home_factor(r) -> float:
    """
    How much of the home edge applies: 1 for a normal fixture, 0 at a neutral
    venue. Finals Weekend is the only neutral one - except when a finalist's own
    club hosts it (Surbiton, 2023/24 and 2025/26), and then that side gets the
    edge whichever way round the fixture is listed (-1 = it's the away side's).
    """
    if "finals weekend" not in r.competition.lower():
        return 1.0
    venue = r.venue.lower() if isinstance(r.venue, str) else ""  # NaN when unknown
    stem = lambda club: club.lower().removesuffix(" hc").removesuffix(" hockey club")
    if venue.startswith(stem(r.home_club)):
        return 1.0
    if venue.startswith(stem(r.away_club)):
        return -1.0
    return 0.0


def home_factor_de(df: pd.DataFrame) -> list[float]:
    """
    Germany's Final Four (play-off semi-finals and final) is one weekend at one
    ground, so it is neutral unless the host club is in it. Earlier play-off
    rounds are home and away. Only matters if the home setting is non-zero.
    """
    out = []
    po = df[df.competition.str.contains("Play-offs")]
    finals = {s: d.date.max() - pd.Timedelta(days=2) for s, d in po.assign(date=pd.to_datetime(po.starts_at)).groupby("season")}
    for r in df.itertuples(index=False):
        if "Play-offs" not in r.competition or pd.Timestamp(r.starts_at) < finals[r.season]:
            out.append(1.0)
            continue
        venue = r.venue.lower() if isinstance(r.venue, str) else ""
        out.append(1.0 if r.home_club.lower() in venue else -1.0 if r.away_club.lower() in venue else 0.0)
    return out


def margin_multiplier(goal_diff: int, scale: float) -> float:
    """Damped margin weight: log so a rout counts more, but not proportionally."""
    return 1.0 if goal_diff <= 1 else 1.0 + scale * math.log(goal_diff)


@dataclass
class Ratings:
    k: float
    mov: float
    carry: float
    promoted: float
    home: float
    rating: dict[int, float] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)
    # Rating for every team the moment a season began, i.e. AFTER the regression
    # toward the mean. Movement must be measured from here, otherwise the
    # season reset shows up as a huge fake drop for whoever was on top.
    season_start: dict[str, dict[int, float]] = field(default_factory=dict)

    def new_season(self, teams: set[int]) -> None:
        """
        The summer, given the coming season's sides. First season: everyone level
        on START. After that, relegated sides leave the pool; the rest regress toward
        last season's average (squads turn over, nobody stays elite by default); and
        any side not in last season - promoted, or back after time away - starts
        `promoted` below that average. A returning side's old rating is ignored: it
        would have drifted toward the average while the side was gone.
        """
        if not self.rating:
            self.rating = {t: START for t in teams}
            return
        mean = float(np.mean(list(self.rating.values())))
        self.rating = {t: mean + self.carry * (self.rating[t] - mean) if t in self.rating
                       else mean + self.promoted for t in teams}

    def run(self, df: pd.DataFrame) -> pd.DataFrame:
        """Walk the matches in order; record each PRE-match rating difference."""
        sides = {s: set(d.home_team_id) | set(d.away_team_id) for s, d in df.groupby("season")}
        rows = []
        season = None
        for r in df.itertuples(index=False):
            if r.season != season:
                season = r.season
                self.new_season(sides[season])
                self.season_start[season] = dict(self.rating)
            rh, ra = self.rating[r.home_team_id], self.rating[r.away_team_id]
            diff = (rh + self.home * r.home_factor) - ra
            rows.append({"match_id": r.match_id, "season": r.season, "date": r.date,
                         "home": r.home, "away": r.away, "diff": diff,
                         "outcome": r.outcome,
                         "home_rating": rh, "away_rating": ra})
            exp_home = 1.0 / (1.0 + 10 ** (-diff / 400.0))
            actual = 1.0 if r.outcome == 0 else (0.5 if r.outcome == 1 else 0.0)
            mult = margin_multiplier(abs(r.home_score - r.away_score), self.mov)
            delta = self.k * mult * (actual - exp_home)
            self.rating[r.home_team_id] = rh + delta
            self.rating[r.away_team_id] = ra - delta
            self.history.append({"date": r.date, "season": r.season,
                                 "team_id": r.home_team_id, "team": r.home,
                                 "rating": self.rating[r.home_team_id]})
            self.history.append({"date": r.date, "season": r.season,
                                 "team_id": r.away_team_id, "team": r.away,
                                 "rating": self.rating[r.away_team_id]})
        return pd.DataFrame(rows)


def logit_rows(rows: pd.DataFrame, first: str = FIRST_SEASON) -> pd.DataFrame:
    """Rows the result translator may learn from: everything after the burn-in season."""
    return rows[rows.season > first]


def fit_ordered_logit(diff: np.ndarray, outcome: np.ndarray,
                      symmetric: bool = True) -> tuple[float, float, float]:
    """
    Rating difference -> P(home win) / P(draw) / P(away win).

    Ordered logistic with two cutpoints: P(H) = 1 - s(c1 - b*d),
    P(A) = s(c0 - b*d), draw is the middle band. Fitted by MLE so the
    probabilities are calibrated rather than assumed.

    Symmetric (c0 = -c1, the default): two equal sides get equal home and away
    chances. The Prem has no measurable home edge, and a free home lean only
    learned noise from short fitting windows: 2022/23's +0.54 home goal difference
    made the men's 2023/24 test worse (0.645 v 0.624). Worse in no season, 2026-10-20.
    """
    def cuts(p: np.ndarray) -> tuple[float, float]:
        w = abs(p[2]) + 1e-6
        c0 = -w / 2 if symmetric else p[1]
        return c0, c0 + w

    def nll(p: np.ndarray) -> float:
        b = p[0]
        c0, c1 = cuts(p)
        z = b * diff
        p_away = 1.0 / (1.0 + np.exp(-(c0 - z)))
        p_ha = 1.0 / (1.0 + np.exp(-(c1 - z)))
        p_draw = np.clip(p_ha - p_away, 1e-9, 1.0)
        p_home = np.clip(1.0 - p_ha, 1e-9, 1.0)
        p_away = np.clip(p_away, 1e-9, 1.0)
        probs = np.where(outcome == 0, p_home, np.where(outcome == 1, p_draw, p_away))
        return -np.log(probs).sum()

    res = minimize(nll, np.array([0.004, -1.0, 1.2]), method="Nelder-Mead",
                   options={"maxiter": 4000, "xatol": 1e-6, "fatol": 1e-6})
    c0, c1 = cuts(res.x)
    return res.x[0], c0, c1


def probs(diff: np.ndarray, b: float, c0: float, c1: float) -> np.ndarray:
    z = b * diff
    p_away = 1.0 / (1.0 + np.exp(-(c0 - z)))
    p_ha = 1.0 / (1.0 + np.exp(-(c1 - z)))
    return np.column_stack([1 - p_ha, np.clip(p_ha - p_away, 1e-9, 1), p_away])


def evaluate(df: pd.DataFrame, params: dict, test_season: str, fit_seasons: list[str],
             min_fit: int = 50) -> dict:
    """Rate everything in order, fit the logit on earlier seasons, score the test one."""
    r = Ratings(**params)
    rows = r.run(df)
    tr = logit_rows(rows[rows.season.isin(fit_seasons)], df.season.min())
    te = rows[rows.season == test_season]
    if len(tr) < min_fit or len(te) == 0:
        return {"n": 0, "acc": float("nan"), "logloss": float("nan")}
    b, c0, c1 = fit_ordered_logit(tr["diff"].to_numpy(), tr["outcome"].to_numpy())
    p = probs(te["diff"].to_numpy(), b, c0, c1)
    y = te["outcome"].to_numpy()
    acc = float((p.argmax(axis=1) == y).mean())
    ll = float(-np.log(np.clip(p[np.arange(len(y)), y], 1e-9, 1)).mean())
    return {"n": len(te), "acc": acc, "logloss": ll, "b": b, "c0": c0, "c1": c1}


def tune(df: pd.DataFrame, val_season: str, fit_seasons: list[str]) -> dict:
    best, best_ll = None, float("inf")
    for k in (12.0, 16.0, 20.0, 24.0, 32.0):
        for mov in (0.0, 0.5, 1.0, 1.5):
            for carry in (0.5, 0.75, 0.9, 1.0):
                for promoted in (0.0, -40.0, -60.0, -90.0):
                    for home in (15.0, 25.0, 35.0, 50.0):
                        p = {"k": k, "mov": mov, "carry": carry,
                             "promoted": promoted, "home": home}
                        res = evaluate(df, p, val_season, fit_seasons)
                        if res["n"] and res["logloss"] < best_ll:
                            best_ll, best = res["logloss"], {**p}
    return best


def rankings(df: pd.DataFrame, params: dict, as_of: pd.Timestamp | None = None) -> pd.DataFrame:
    """
    The weekly post: current rating, movement in the last match, and movement
    across the season so far. Both movements are measured within the season,
    so the between-season regression never shows up as form.
    """
    if as_of is not None:
        df = df[df.date <= as_of]
    r = Ratings(**params)
    r.run(df)
    hist = pd.DataFrame(r.history).sort_values("date")
    latest = df.season.iloc[-1]
    in_league = (set(df[df.season == latest].home_team_id)
                 | set(df[df.season == latest].away_team_id))
    start = r.season_start.get(latest, {})
    names = hist.groupby("team_id")["team"].last().to_dict()
    this_season = df[df.season == latest]
    played = pd.concat([this_season.home_team_id, this_season.away_team_id]).value_counts()

    rows = []
    for t in in_league:
        h = hist[(hist.team_id == t) & (hist.season == latest)]["rating"].tolist()
        rows.append({
            "team": names.get(t, str(t)),
            "rating": round(r.rating[t], 1),
            "last": round(h[-1] - h[-2], 1) if len(h) >= 2 else None,
            "season": round(r.rating[t] - start[t], 1) if t in start else None,
            "P": int(played.get(t, 0)),
        })
    out = pd.DataFrame(rows).sort_values("rating", ascending=False).reset_index(drop=True)
    out.insert(0, "rank", range(1, len(out) + 1))
    return out


def movement(df: pd.DataFrame, params: dict, as_of: pd.Timestamp | None = None) -> dict:
    """
    The latest season round by round, for the power rankings post: every team's
    rating and rank before a ball was hit ("Pre", after the season-boundary
    regression) and at the end of each weekend since. A round is a Saturday-based
    weekend, so a Sunday game counts with its Saturday. A team that didn't play
    that weekend keeps its rating.
    """
    if as_of is not None:
        df = df[df.date <= as_of]
    r = Ratings(**params)
    r.run(df)
    latest = df.season.iloc[-1]
    cur = df[df.season == latest]
    hist = pd.DataFrame(r.history)
    hist = hist[hist.season == latest]
    start = r.season_start[latest]
    teams = sorted(set(cur.home_team_id) | set(cur.away_team_id))
    club = {**dict(zip(cur.home_team_id, cur.home_club)), **dict(zip(cur.away_team_id, cur.away_club))}
    name = {**dict(zip(cur.home_team_id, cur.home)), **dict(zip(cur.away_team_id, cur.away))}
    # Germany also plays Thursday and Friday nights, as the start of that weekend's round.
    de = cur.competition.str.contains("Bundesliga").any()
    sat = lambda d: ((d + pd.Timedelta(days=5 - d.weekday())) if de and d.weekday() in (3, 4)
                     else (d - pd.Timedelta(days=(d.weekday() - 5) % 7))).normalize()
    weekends = sorted({sat(d) for d in cur.date})

    snaps = [dict(start)]
    # Games most sides had played by the end of each weekend (the mode, highest on a tie), so one side
    # playing a rescheduled game early doesn't run the matchday count ahead.
    mode_played = lambda m: int(pd.concat([m.home_team_id, m.away_team_id]).value_counts()
                                .reindex(teams, fill_value=0).value_counts().sort_index(ascending=False).idxmax())
    games = []
    for w in weekends:
        upto_m = cur[cur.date < w + pd.Timedelta(days=2)]
        games.append(mode_played(upto_m))
        upto = hist[hist.date < w + pd.Timedelta(days=2)]
        last = upto.groupby("team_id")["rating"].last().to_dict()
        snaps.append({t: last.get(t, start[t]) for t in teams})
    # Tied ratings share a position, as in a league table: promoted sides get identical seeds, so
    # ranking them by list order would invent a gap between them (and false places gained or lost).
    ranks = [{t: 1 + sum(s[u] > s[t] + 1e-9 for u in teams) for t in teams} for s in snaps]

    def stage(comp: str) -> str:
        c = comp.lower()
        return ("Finals" if any(k in c for k in ("finals pool", "play off", "finals weekend", "play-offs", "play-downs"))
                else "League" if "bundesliga" in c
                else "Phase 2" if "phase 2" in c else "Phase 1")
    wk_of = cur.date.map(sat)
    stages = [cur[wk_of == w].competition.map(stage).mode()[0] for w in weekends]

    record = {t: [0, 0, 0] for t in teams}
    for m in cur.itertuples(index=False):
        for t, o in ((m.home_team_id, m.outcome), (m.away_team_id, 2 - m.outcome)):
            record[t][o] += 1  # W / D / L from that side's view
    return {
        "season": latest,
        "as_of": str(cur.date.max().date()),
        "games": games[-1],  # Germany's matchday count
        "games_by_round": games,
        "rounds": ["Pre"] + [f"R{i + 1}" for i in range(len(weekends))],
        "weekends": [str(w.date()) for w in weekends],
        "stages": stages,  # Phase 1 / Phase 2 / Finals (finals pools, play-offs, Finals Weekend), per weekend
        "teams": [{"team_id": int(t), "team": name[t], "club": club[t],
                   "rating": [round(s[t], 1) for s in snaps],
                   "rank": [rk[t] for rk in ranks],
                   "record": record[t]} for t in teams],
    }


def by_season(df: pd.DataFrame, params: dict, since: str) -> dict:
    """
    Season on season, for the "why does everyone start where they start" post:
    for every side in the latest season, its rating at the start and end of each
    season since `since` (None when it wasn't in the Prem), plus the pool average
    at each point. Start = after the season-boundary regression, or the promoted
    seed. The model starts in FIRST_SEASON with every side on START; `since` only
    trims the output.
    """
    r = Ratings(**params)
    r.run(df)
    hist = pd.DataFrame(r.history)
    latest = df.season.iloc[-1]
    cur = df[df.season == latest]
    teams = sorted(set(cur.home_team_id) | set(cur.away_team_id))
    club = {**dict(zip(cur.home_team_id, cur.home_club)), **dict(zip(cur.away_team_id, cur.away_club))}
    seasons = [s for s in sorted(df.season.unique()) if s >= since]
    out = {"seasons": seasons, "latest": latest, "teams": [], "mean": []}
    ends = {}
    for s in seasons:
        h = hist[hist.season == s]
        ends[s] = h.groupby("team_id")["rating"].last().to_dict()
        played = set(df[df.season == s].home_team_id) | set(df[df.season == s].away_team_id)
        start = r.season_start[s]
        out["mean"].append({"start": round(float(np.mean([start[t] for t in played if t in start])), 1),
                            "end": round(float(np.mean([ends[s][t] for t in played])), 1) if s != latest else None})
    for t in teams:
        row = {"team_id": int(t), "club": club[t], "start": [], "end": []}
        for s in seasons:
            inn = t in ends[s]
            row["start"].append(round(r.season_start[s][t], 1) if inn else None)
            row["end"].append(round(ends[s][t], 1) if inn and s != latest else None)
        out["teams"].append(row)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gender", choices=["M", "W"], required=True)
    ap.add_argument("--league", choices=list(LEAGUES), default="en",
                    help="en: Premier Division (default); de: German 1. Bundesliga (hockey_stats_de)")
    ap.add_argument("--tune", action="store_true")
    ap.add_argument("--rankings", action="store_true")
    ap.add_argument("--as-of", default=None, help="Rankings as they stood on this date")
    ap.add_argument("--movement", metavar="JSON", default=None,
                    help="Write the latest season round by round (rating + rank per weekend) to this file")
    ap.add_argument("--seasons", metavar="JSON", default=None,
                    help="Write each current side's start/end rating for every season since --since to this file")
    ap.add_argument("--since", default=None, help="First season in the --seasons output (post-COVID by default)")
    args = ap.parse_args()

    lg = LEAGUES[args.league]
    args.since = args.since or lg["first"]
    df = load(args.gender, args.league)
    seasons = sorted(df.season.unique())
    label = "Men's/Open" if args.gender == "M" else "Women's"
    print(f"{label} {lg['label']}: {len(df)} matches, seasons {seasons[0]}-{seasons[-1]}")

    val, test = lg["val"], lg["test"]
    fit_val = [s for s in seasons if s < val]
    fit_test = [s for s in seasons if s < test]

    params = tune(df, val, fit_val) if args.tune else (TUNED if args.league == "en" else TUNED_DE)[args.gender]
    if args.tune:
        print(f"\ntuned on {val}: {params}")

    if args.seasons:
        import json
        with open(args.seasons, "w") as f:
            json.dump(by_season(df, params, args.since), f, indent=1)
        print(f"seasons since {args.since} -> {args.seasons}")
        return

    if args.movement:
        import json
        mv = movement(df, params, pd.Timestamp(args.as_of) if args.as_of else None)
        with open(args.movement, "w") as f:
            json.dump(mv, f, indent=1)
        print(f"{mv['season']} {', '.join(mv['rounds'])} -> {args.movement}")
        return

    if args.rankings:
        as_of = pd.Timestamp(args.as_of) if args.as_of else None
        print(f"\n=== POWER RANKINGS{' as of ' + args.as_of if as_of else ''} ===")
        print(rankings(df, params, as_of).to_string(index=False))
        return

    for season, fit in ((val, fit_val), (test, fit_test)):
        res = evaluate(df, params, season, fit)
        if res["n"]:
            print(f"\n{season}: {res['n']} matches   accuracy {res['acc']:.1%}   "
                  f"log-loss {res['logloss']:.3f}")
    print("\nBenchmarks: the Poisson model gets 66.1% of Premier results right "
          "(log-loss ~0.93); always-home is ~45%; a flat 1/3 guess is log-loss 1.099.")
    print("\n=== CURRENT POWER RANKINGS ===")
    print(rankings(df, params).to_string(index=False))


if __name__ == "__main__":
    main()
