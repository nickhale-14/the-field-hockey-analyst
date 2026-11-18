"""
Premier Division Phase 1 simulator: who makes the Top 6.

Phase 1 is a single round-robin (matches 1-11, Sept-Nov); then the league
splits Top 6 / Lower 6 with points carried, and that split fixes the finishing
range (Top 6 finish 1-6 whatever happens after). This plays the rest of Phase 1
out N times and counts where everyone finishes.

Two models, each doing a different job:
- the RESULT of each match comes from the power rankings (prem_elo.py: Elo +
  ordered logit; no more accurate than Poisson overall, see docs/findings.md,
  but its ratings can update inside a simulated season);
- the SCORELINE, needed for goal difference, comes from the Poisson model
  (predictor.py), drawn from its scoreline grid restricted to that result.
Ratings update inside each simulated season with the prem_elo formula, so a
side that wins its next three is rated higher for the fourth. That is how
uncertainty about the ratings themselves gets in.

Table order: points (3/1/0), then goal difference, then goals scored; anything
still level is split at random. --backtest checks that order against which
sides actually went into the Top 6, and scores the odds on 2022/23-2025/26.

    docker compose exec jupyter python3 /home/jovyan/work/simulate.py --gender M
    docker compose exec jupyter python3 /home/jovyan/work/simulate.py --gender M --backtest
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

import predictor as P
import prem_elo as pe

TOP = 6
BACKTEST_SEASONS = ["2022/23", "2023/24", "2024/25", "2025/26"]


def load_prem(gender: str) -> pd.DataFrame:
    """Every Premier Division match for one gender, played or not."""
    sql = text(
        """
        SELECT m.id AS match_id, s.name AS season, c.name AS competition, m.starts_at,
               m.status, m.home_team_id, m.away_team_id, th.name AS home, ta.name AS away,
               m.home_score, m.away_score
        FROM matches m
        JOIN seasons s ON s.id = m.season_id
        JOIN competitions c ON c.id = m.competition_id
        JOIN teams th ON th.id = m.home_team_id
        JOIN teams ta ON ta.id = m.away_team_id
        WHERE c.name LIKE '%%Premier Division%%'
        """
    )
    with create_engine(P.DATABASE_URL).connect() as conn:
        df = pd.read_sql(sql, conn)
    is_w = df["competition"].str.lower().str.contains("women")
    df = (df[is_w] if gender == "W" else df[~is_w]).copy()
    df["date"] = pd.to_datetime(df["starts_at"])
    return df.sort_values(["date", "match_id"]).reset_index(drop=True)


def phase1(prem: pd.DataFrame, season: str) -> pd.DataFrame:
    """Phase 1 of a season: its biggest competition (names drift, the size doesn't).
    Only results and fixtures still to play; abandoned/postponed duplicate rows go."""
    s = prem[prem.season == season]
    comp = s.competition.value_counts().index[0]
    return s[(s.competition == comp) & s.status.isin(["R", "S"])].reset_index(drop=True)


def actual_top6(prem: pd.DataFrame, season: str) -> set[int]:
    s = prem[(prem.season == season) & prem.competition.str.contains("Top 6")]
    return set(s.home_team_id) | set(s.away_team_id)


def table(played: pd.DataFrame, teams: list[int]) -> pd.DataFrame:
    t = pd.DataFrame(0, index=teams, columns=["P", "pts", "gf", "ga"])
    for r in played.itertuples(index=False):
        hs, as_ = int(r.home_score), int(r.away_score)
        for tid, f, a in ((r.home_team_id, hs, as_), (r.away_team_id, as_, hs)):
            t.loc[tid, ["P", "gf", "ga"]] += [1, f, a]
            t.loc[tid, "pts"] += 3 if f > a else (1 if f == a else 0)
    return t


def rank_key(pts, gf, ga, rng) -> np.ndarray:
    """Points, then goal difference, then goals scored, then a coin toss.
    Packed into one sortable number (GD sits well inside +/-1000, GF under 1000)."""
    return pts * 1e7 + (gf - ga + 1000) * 1e3 + gf + rng.random(np.shape(pts))


def prepare(gender: str, prem: pd.DataFrame, season: str, as_of: pd.Timestamp):
    """Everything the simulation needs, using only what was known at `as_of`."""
    p1 = phase1(prem, season)
    teams = sorted(set(p1.home_team_id) | set(p1.away_team_id))
    names = dict(zip(p1.home_team_id, p1.home)) | dict(zip(p1.away_team_id, p1.away))
    played = p1[(p1.status == "R") & (p1.date < as_of)]
    remaining = p1[~p1.match_id.isin(played.match_id)].copy()

    # Ratings and the result translator, from the power rankings.
    params = pe.TUNED[gender]
    elo_df = pe.load(gender)
    elo_df = elo_df[elo_df.date < as_of]
    ratings = pe.Ratings(**params)
    rows = ratings.run(elo_df)
    if season not in set(elo_df.season):
        ratings.new_season(set(teams))  # pre-season: the summer regression and promoted seeds now
    rows = pe.logit_rows(rows)
    logit = pe.fit_ordered_logit(rows["diff"].to_numpy(), rows["outcome"].to_numpy())

    # Expected goals for each remaining fixture, from the Poisson model.
    pp = P.TUNED[gender]
    pmatches = P.load_matches(gender)
    fx = remaining.assign(tier="Premier")
    both = pd.concat([pmatches, fx], ignore_index=True)
    tiers_by_season = P.team_tiers_by_season(both)
    moves = P.team_moves(both)
    tt = P.current_tier_map(pmatches, tiers_by_season, as_of, upcoming=fx)
    fit = P.fit_model(pmatches, as_of, pp["half_life"], pp["shrink"], tt, moves,
                      season_discount=pp["season_discount"], tier_home=pp["tier_home"])
    xg = [fit.rates(r.home_team_id, r.away_team_id, "Premier", "Premier",
                    home_move=moves.get((r.home_team_id, season)),
                    away_move=moves.get((r.away_team_id, season)))
          for r in remaining.itertuples(index=False)]
    remaining["xg_home"] = [h for h, _ in xg]
    remaining["xg_away"] = [a for _, a in xg]

    return {"teams": teams, "names": names, "table": table(played, teams),
            "remaining": remaining, "rating": {t: ratings.rating[t] for t in teams},
            "params": params, "logit": logit, "coupling": fit.coupling}


def simulate(prep: dict, n: int, seed: int = 0, outcomes: list | None = None,
             totals: dict | None = None) -> np.ndarray:
    """Play the remaining fixtures n times. Returns finishing position (0 = 1st)
    per simulation and team, shape (n, teams). Pass a list as `outcomes` to get each
    fixture's simulated result back (0 H, 1 D, 2 A per simulation), in date order.
    Pass a dict as `totals` to get each simulation's final pts, gf and ga back."""
    rng = np.random.default_rng(seed)
    teams, params = prep["teams"], prep["params"]
    b, c0, c1 = prep["logit"]
    idx = {t: i for i, t in enumerate(teams)}
    tab = prep["table"]
    R = np.tile([prep["rating"][t] for t in teams], (n, 1)).astype(float)
    pts = np.tile(tab.pts.to_numpy(), (n, 1)).astype(float)
    gf = np.tile(tab.gf.to_numpy(), (n, 1)).astype(float)
    ga = np.tile(tab.ga.to_numpy(), (n, 1)).astype(float)

    for r in prep["remaining"].sort_values("date").itertuples(index=False):
        h, a = idx[r.home_team_id], idx[r.away_team_id]
        diff = R[:, h] + params["home"] - R[:, a]
        p = pe.probs(diff, b, c0, c1)
        u = rng.random(n)
        outcome = (u > p[:, 0]).astype(int) + (u > p[:, 0] + p[:, 1])  # 0 H, 1 D, 2 A
        if outcomes is not None:
            outcomes.append(outcome)

        # Scoreline given the result: the Poisson grid restricted to that result.
        grid = P.score_grid(r.xg_home, r.xg_away, prep["coupling"])
        gi, gj = np.indices(grid.shape)
        hs, as_ = np.zeros(n, int), np.zeros(n, int)
        for o, cells in ((0, gi > gj), (1, gi == gj), (2, gi < gj)):
            sel = outcome == o
            if sel.any():
                w = np.where(cells, grid, 0.0).ravel()
                pick = rng.choice(w.size, sel.sum(), p=w / w.sum())
                hs[sel], as_[sel] = np.unravel_index(pick, grid.shape)

        pts[:, h] += np.choose(outcome, [3, 1, 0])
        pts[:, a] += np.choose(outcome, [0, 1, 3])
        gf[:, h] += hs
        ga[:, h] += as_
        gf[:, a] += as_
        ga[:, a] += hs

        # Same update as prem_elo.Ratings.run.
        exp_home = 1.0 / (1.0 + 10 ** (-diff / 400.0))
        actual = np.choose(outcome, [1.0, 0.5, 0.0])
        margin = np.abs(hs - as_)
        mult = np.where(margin <= 1, 1.0, 1.0 + params["mov"] * np.log(np.maximum(margin, 1)))
        delta = params["k"] * mult * (actual - exp_home)
        R[:, h] += delta
        R[:, a] -= delta

    if totals is not None:
        totals.update(pts=pts, gf=gf, ga=ga)
    order = np.argsort(-rank_key(pts, gf, ga, rng), axis=1)
    pos = np.empty_like(order)
    pos[np.arange(n)[:, None], order] = np.arange(len(teams))
    return pos


def odds_table(prep: dict, pos: np.ndarray) -> pd.DataFrame:
    tab = prep["table"]
    out = pd.DataFrame({
        "team": [prep["names"][t] for t in prep["teams"]],
        "P": tab.P.to_numpy(), "Pts": tab.pts.to_numpy(),
        "GD": (tab.gf - tab.ga).to_numpy(),
        "rating": np.round([prep["rating"][t] for t in prep["teams"]], 0).astype(int),
        "avg_pos": pos.mean(0) + 1,
        "1st": (pos == 0).mean(0),
        "top6": (pos < TOP).mean(0),
        "bottom2": (pos >= len(prep["teams"]) - 2).mean(0),
    })
    return out.sort_values(["top6", "avg_pos"], ascending=[False, True]).reset_index(drop=True)


def projected_table(prep: dict, pos: np.ndarray, totals: dict) -> pd.DataFrame:
    """The final Phase 1 table, averaged over the simulations, in average finishing order."""
    tab = prep["table"]
    out = pd.DataFrame({
        "team": [prep["names"][t] for t in prep["teams"]],
        "pts_now": tab.pts.to_numpy(),
        "pts": totals["pts"].mean(0),
        "pts_lo": np.percentile(totals["pts"], 10, axis=0),
        "pts_hi": np.percentile(totals["pts"], 90, axis=0),
        "gd": (totals["gf"] - totals["ga"]).mean(0),
        "gf": totals["gf"].mean(0),
        "ga": totals["ga"].mean(0),
        "avg_pos": pos.mean(0) + 1,
        "top6": (pos < TOP).mean(0),
    })
    return out.sort_values("avg_pos").reset_index(drop=True)


def week_starts(p1: pd.DataFrame) -> list[pd.Timestamp]:
    """Start of each match week, as a cutoff: 'after round r' = before week r."""
    weeks = sorted(p1.date.dt.to_period("W-SUN").unique())
    return [w.start_time for w in weeks]


def backtest(gender: str, prem: pd.DataFrame, n: int) -> None:
    print("\nTiebreak check (points, GD, goals scored) v the real Top 6:")
    for season in BACKTEST_SEASONS:
        p1 = phase1(prem, season)
        teams = sorted(set(p1.home_team_id) | set(p1.away_team_id))
        t = table(p1[p1.status == "R"], teams)
        key = rank_key(t.pts.to_numpy(), t.gf.to_numpy(), t.ga.to_numpy(), np.random.default_rng(0))
        mine = {teams[i] for i in np.argsort(-key)[:TOP]}
        real = actual_top6(prem, season)
        print(f"  {season}: {'matches' if mine == real else 'MISMATCH ' + str(mine ^ real)}")

    recs = []
    for season in BACKTEST_SEASONS:
        p1 = phase1(prem, season)
        real = actual_top6(prem, season)
        starts = week_starts(p1)
        for after in (0, 3, 6, 9):
            prep = prepare(gender, prem, season, starts[after])
            pos = simulate(prep, n)
            tab = prep["table"]
            key = rank_key(tab.pts.to_numpy(), tab.gf.to_numpy(), tab.ga.to_numpy(),
                           np.random.default_rng(1))
            table_now = np.zeros(len(prep["teams"]))
            table_now[np.argsort(-key)[:TOP]] = 1.0
            if after == 0:
                table_now[:] = TOP / len(prep["teams"])  # no table yet
            for i, t in enumerate(prep["teams"]):
                recs.append({"season": season, "after": after, "p": (pos[:, i] < TOP).mean(),
                             "table_now": table_now[i], "y": float(t in real)})
    bt = pd.DataFrame(recs)

    print("\nP(Top 6) scored against what happened (Brier: 0 perfect, lower is better).")
    print("'table now' = whoever is in the top six at that point is in, nobody else.")
    for after, g in bt.groupby("after"):
        sim = ((g.p - g.y) ** 2).mean()
        naive = ((g.table_now - g.y) ** 2).mean()
        print(f"  after round {after:>2}: simulator {sim:.3f}   table now {naive:.3f}   (n={len(g)})")

    print("\nCalibration, all checkpoints pooled (does 80% happen 80% of the time?):")
    bins = [0, 0.05, 0.2, 0.4, 0.6, 0.8, 0.95, 1.0001]
    bt["bin"] = pd.cut(bt.p, bins, right=False)
    cal = bt.groupby("bin", observed=True).agg(n=("y", "size"), said=("p", "mean"), happened=("y", "mean"))
    print(cal.round(2).to_string())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gender", choices=["M", "W"], required=True)
    ap.add_argument("--sims", type=int, default=20000)
    ap.add_argument("--as-of", default=None, help="Odds as they stood at this date (default: now)")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--csv", default=None, help="Write the full position distribution here")
    ap.add_argument("--fixtures", default=None, help="Write each remaining fixture's win/draw/loss chances here")
    ap.add_argument("--stakes", default=None,
                    help="Write each side's Top 6 chance given each remaining fixture's result here (what's at stake)")
    ap.add_argument("--table", action="store_true", help="Also print the projected final Phase 1 table")
    args = ap.parse_args()

    prem = load_prem(args.gender)
    label = "Men's/Open" if args.gender == "M" else "Women's"
    if args.backtest:
        print(f"{label} Premier Division: Phase 1 simulator backtest, {args.sims} sims per checkpoint")
        backtest(args.gender, prem, args.sims)
        return

    season = prem.season.max()
    as_of = pd.Timestamp(args.as_of) if args.as_of else pd.Timestamp.now()
    prep = prepare(args.gender, prem, season, as_of)
    results: list = []
    totals: dict = {}
    pos = simulate(prep, args.sims, outcomes=results, totals=totals)
    print(f"{label} Premier Division {season}, Phase 1 odds as of {as_of:%Y-%m-%d}: "
          f"{len(prep['remaining'])} fixtures left, {args.sims} simulations")
    out = odds_table(prep, pos)
    fmt = out.copy()
    for c in ("1st", "top6", "bottom2"):
        fmt[c] = (fmt[c] * 100).round(1).astype(str) + "%"
    fmt["avg_pos"] = fmt["avg_pos"].round(1)
    print(fmt.to_string(index=False))
    if args.table:
        proj = projected_table(prep, pos, totals)
        proj["top6"] = (proj["top6"] * 100).round(1).astype(str) + "%"
        print("\nProjected final Phase 1 table (averages over the simulations; pts_lo/hi = 10th-90th percentile):")
        print(proj.round(1).to_string())
    if args.csv:
        dist = pd.DataFrame({prep["names"][t]: np.bincount(pos[:, i], minlength=len(prep["teams"])) / len(pos)
                             for i, t in enumerate(prep["teams"])}).T
        dist.columns = [f"pos_{k + 1}" for k in dist.columns]
        dist.to_csv(args.csv)
        print(f"\nposition distribution -> {args.csv}")
    if args.fixtures:
        b, c0, c1 = prep["logit"]
        fx = prep["remaining"].sort_values("date")
        diff = np.array([prep["rating"][h] + prep["params"]["home"] - prep["rating"][a]
                         for h, a in zip(fx.home_team_id, fx.away_team_id)])
        p = pe.probs(diff, b, c0, c1)
        fx.assign(p_home=p[:, 0], p_draw=p[:, 1], p_away=p[:, 2])[
            ["date", "home_team_id", "away_team_id", "home", "away", "p_home", "p_draw", "p_away"]
        ].to_csv(args.fixtures, index=False)
        print(f"fixture chances -> {args.fixtures}")
    if args.stakes:
        # P(Top 6 | result): the same simulations split by how each fixture went
        top = pos < TOP
        rows = []
        fx = prep["remaining"].sort_values("date")
        for r, res in zip(fx.itertuples(index=False), results):
            for i, t in enumerate(prep["teams"]):
                row = {"date": r.date, "home_team_id": r.home_team_id, "away_team_id": r.away_team_id,
                       "home": r.home, "away": r.away, "team_id": t, "team": prep["names"][t],
                       "top6": top[:, i].mean()}
                for o, label in enumerate(("if_home", "if_draw", "if_away")):
                    sel = res == o
                    row[label] = top[sel, i].mean() if sel.any() else np.nan
                rows.append(row)
        pd.DataFrame(rows).to_csv(args.stakes, index=False)
        print(f"what's at stake -> {args.stakes}")


if __name__ == "__main__":
    main()
