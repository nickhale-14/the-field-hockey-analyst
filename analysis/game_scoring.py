"""
Scoring-rule backtest for the predictor game (docs/predictor-game.md).

Which points system should the game use? Each candidate rule is tried on real Prem scorelines
with simulated players, and judged on what a game needs:
  - skill should show: better forecasters should finish higher over a season
  - spread: round totals that separate players, few ties at the top
  - nobody stuck on zero most weeks (exact scores are rare in hockey)

Players are simulated from the FHA Model's own out-of-sample predictions (rolling refit, so no
match is predicted with knowledge of itself):
  - "FHA Model": the likeliest score
  - a crowd of players at different skill levels: each pick is a scoreline drawn from the model's
    score grid with noise added to the expected goals (more noise = worse forecaster)
  - simple fixed habits: "always 2-1 home", "favourite by one", "always 2-2"

    docker compose exec jupyter python3 /home/jovyan/work/game_scoring.py            # predictions cached after first run
    docker compose exec jupyter python3 /home/jovyan/work/game_scoring.py --refresh  # rebuild the predictions
"""
from __future__ import annotations

import argparse
import os

import numpy as np
import pandas as pd

import predictor as P

SEASONS = ["2024/25", "2025/26"]
CACHE = "/home/jovyan/work/output/game_scoring_preds.csv"
MAXG = 12


def build_predictions() -> pd.DataFrame:
    out = []
    for g in ("M", "W"):
        df = P.load_matches(g)
        t = P.TUNED[g]
        for s in SEASONS:
            pred, fit = P.predict_season(df, s, t["half_life"], t["shrink"], rolling=True,
                                         season_discount=t["season_discount"], tier_home=t["tier_home"])
            pred = pred[(pred.tier == "Premier") & ~pred.competition.str.contains("Finals Weekend")].copy()
            pred["gender"], pred["season"], pred["coupling"] = g, s, fit.coupling
            out.append(pred)
            print(f"{g} {s}: {len(pred)} Prem matches predicted", flush=True)
    res = pd.concat(out, ignore_index=True)
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    res.to_csv(CACHE, index=False)
    return res


# ---------------------------------------------------------------- rules
def sign(x):
    return np.sign(x)


def rule_points(name, ph, pa, h, a):
    """Points for pick (ph, pa) against result (h, a); all arrays, broadcastable."""
    exact = (ph == h) & (pa == a)
    res = sign(ph - pa) == sign(h - a)
    margin = res & ((ph - pa) == (h - a))
    if name == "Super 6 (5 exact / 2 result)":
        return np.where(exact, 5, np.where(res, 2, 0))
    if name == "5 exact / 3 margin / 2 result":
        return np.where(exact, 5, np.where(margin, 3, np.where(res, 2, 0)))
    if name == "3 exact / 1 result":
        return np.where(exact, 3, np.where(res, 1, 0))
    if name == "Result 2 + 1 per side's goals":
        return 2 * res + (ph == h) + (pa == a)
    if name == "5 exact / 3 margin / 2 result / 1 within a goal":
        near = (np.abs(ph - h) + np.abs(pa - a)) <= 1
        return np.where(exact, 5, np.where(margin, 3, np.where(res, 2, np.where(near, 1, 0))))
    if name == "10 exact / 5 margin / 2 result":
        return np.where(exact, 10, np.where(margin, 5, np.where(res, 2, 0)))
    if name == "Result 3 + margin 3/2/1 + exact 2":
        off = np.abs((ph - pa) - (h - a))
        return res * (3 + np.maximum(0, 3 - off)) + 2 * exact
    if name == "Result 3 + margin 3 (1 out: 2) + exact 2":
        off = np.abs((ph - pa) - (h - a))
        return res * (3 + np.where(off == 0, 3, np.where(off == 1, 2, 0))) + 2 * exact
    if name == "Result 3 + margin within one 2 + exact 3":
        off = np.abs((ph - pa) - (h - a))
        return res * (3 + 2 * (off <= 1)) + 3 * exact
    if name == "Result 3 + margin 2 (1 out: 1) + exact 2":
        off = np.abs((ph - pa) - (h - a))
        return res * (3 + np.where(off == 0, 2, np.where(off == 1, 1, 0))) + 2 * exact
    if name == "Exact 6 / result 3 (+1 margin)":
        return np.where(exact, 6, np.where(margin, 4, np.where(res, 3, 0)))
    if name == "Exact 6 / result 3 (+2 margin, +1 one out)":
        off = np.abs((ph - pa) - (h - a))
        return np.where(exact, 6, res * (3 + np.where(off == 0, 2, np.where(off == 1, 1, 0))))
    if name == "Exact 7 / result 3 (+2 margin, +1 one out)":
        off = np.abs((ph - pa) - (h - a))
        return np.where(exact, 7, res * (3 + np.where(off == 0, 2, np.where(off == 1, 1, 0))))
    if name == "Exact 5 / one goal off 3 / result 2":       # THE GAME'S RULE, locked 2026-11-16
        near = res & ((np.abs(ph - h) + np.abs(pa - a)) == 1)
        return np.where(exact, 5, np.where(near, 3, np.where(res, 2, 0)))
    raise KeyError(name)


GAME_RULE = "Exact 5 / one goal off 3 / result 2"
RULES = [GAME_RULE, "Super 6 (5 exact / 2 result)", "3 exact / 1 result", "5 exact / 3 margin / 2 result",
         "Result 2 + 1 per side's goals", "5 exact / 3 margin / 2 result / 1 within a goal",
         "10 exact / 5 margin / 2 result", "Result 3 + margin 3/2/1 + exact 2",
         "Result 3 + margin 3 (1 out: 2) + exact 2", "Result 3 + margin within one 2 + exact 3",
         "Result 3 + margin 2 (1 out: 1) + exact 2", "Exact 6 / result 3 (+1 margin)",
         "Exact 6 / result 3 (+2 margin, +1 one out)", "Exact 7 / result 3 (+2 margin, +1 one out)"]


# ---------------------------------------------------------------- players
def crowd_picks(pred: pd.DataFrame, noise: np.ndarray, rng) -> tuple[np.ndarray, np.ndarray]:
    """Picks for len(noise) players over every match. A player's belief is the model's xG times
    a lognormal error of size `noise`; they pick the likeliest score of that belief, or with prob
    0.3 a draw from it (people don't always pick the mode)."""
    n, m = len(noise), len(pred)
    xh = pred.xg_home.values[None, :] * np.exp(rng.normal(0, 1, (n, m)) * noise[:, None])
    xa = pred.xg_away.values[None, :] * np.exp(rng.normal(0, 1, (n, m)) * noise[:, None])
    # likeliest independent-Poisson score is (floor xh, floor xa) per side
    ph, pa = np.floor(xh).astype(int), np.floor(xa).astype(int)
    draw = rng.random((n, m)) < 0.3
    ph = np.where(draw, rng.poisson(xh), ph)
    pa = np.where(draw, rng.poisson(xa), pa)
    return np.clip(ph, 0, MAXG), np.clip(pa, 0, MAXG)


def fixed_picks(pred: pd.DataFrame) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    m = len(pred)
    ls = pred.pred_score.str.split("-", expand=True).astype(int)
    fav_home = pred.p_home.values >= pred.p_away.values
    return {
        "FHA likeliest score": (ls[0].values, ls[1].values),
        "Always 2-1 home": (np.full(m, 2), np.full(m, 1)),
        "Favourite by one (2-1 / 1-2)": (np.where(fav_home, 2, 1), np.where(fav_home, 1, 2)),
        "Always 2-2": (np.full(m, 2), np.full(m, 2)),
    }


def best_picks(pred: pd.DataFrame, rule: str, k: int = 11) -> tuple[np.ndarray, np.ndarray]:
    """The FHA Model as a player: the pick with the most expected points under `rule`, from its own
    score grid. Not the likeliest score, which calls draws in close games and loses points."""
    i, j = np.meshgrid(np.arange(k), np.arange(k), indexing="ij")
    pts = rule_points(rule, i[:, :, None, None], j[:, :, None, None], i[None, None], j[None, None])
    out = []
    for r in pred.itertuples():
        g = P.score_grid(r.xg_home, r.xg_away, r.coupling)[:k, :k]
        ev = (pts * (g / g.sum())[None, None]).sum((2, 3))
        out.append(np.unravel_index(ev.argmax(), ev.shape))
    bh, ba = np.array(out).T
    return bh, ba


# ---------------------------------------------------------------- rounds
def make_rounds(pred: pd.DataFrame, rng) -> pd.DataFrame:
    """Group into game rounds of 3 Men's/Open + 3 Women's from the same weekend, like Phase 2."""
    pred = pred.copy()
    d = pd.to_datetime(pred.date)
    pred["weekend"] = (d - pd.to_timedelta((d.dt.weekday - 5) % 7, unit="D")).dt.date
    pred["round"] = -1
    rid = 0
    for (s, wk), grp in pred.groupby(["season", "weekend"]):
        m = rng.permutation(grp.index[grp.gender == "M"].values)
        w = rng.permutation(grp.index[grp.gender == "W"].values)
        for i in range(min(len(m), len(w)) // 3):
            pred.loc[np.r_[m[3 * i:3 * i + 3], w[3 * i:3 * i + 3]], "round"] = rid
            rid += 1
    return pred[pred["round"] >= 0].reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--players", type=int, default=2000)
    args = ap.parse_args()
    rng = np.random.default_rng(7)

    pred = build_predictions() if args.refresh or not os.path.exists(CACHE) else pd.read_csv(CACHE)
    pred = make_rounds(pred, rng)
    h, a = pred.home_score.values.astype(int), pred.away_score.values.astype(int)
    n_rounds = pred["round"].nunique()
    print(f"\n{len(pred)} Prem matches in {n_rounds} rounds of six (3 Men's/Open + 3 Women's), "
          f"{', '.join(SEASONS)}; season = {n_rounds // len(SEASONS)} rounds\n")

    # how often each kind of call lands, for the model
    fx = fixed_picks(pred)
    mh, ma = fx["FHA likeliest score"]
    print("FHA likeliest-score picks: exact {:.1%}, right margin {:.1%}, right result {:.1%}, draws in results {:.1%}\n".format(
        np.mean((mh == h) & (ma == a)), np.mean((mh - ma) == (h - a)),
        np.mean(np.sign(mh - ma) == np.sign(h - a)), np.mean(h == a)))

    # crowd: skill = noise level, spread from sharp (0.1) to clueless (1.2)
    noise = rng.uniform(0.1, 1.2, args.players)
    ch, ca = crowd_picks(pred, noise, rng)
    rnd = pred["round"].values
    season_of_round = pred.groupby("round").season.first()

    rows = []
    for rule in RULES:
        pts = rule_points(rule, ch, ca, h[None, :], a[None, :])               # players x matches
        per_round = pd.DataFrame(pts.T).groupby(rnd).sum()                  # rounds x players
        season_tot = per_round.groupby(season_of_round.values).sum()        # seasons x players
        # skill shows: rank correlation of season total with (minus) noise, averaged over seasons
        skill = np.mean([pd.Series(r.values).rank().corr(pd.Series(-noise).rank()) for _, r in season_tot.iterrows()])
        max_round = 6 * rule_points(rule, np.array([1]), np.array([0]), np.array([1]), np.array([0]))[0]
        zero = (per_round.values == 0).mean()
        rsd = per_round.values.std(axis=1).mean()
        # ties among the top 20 of each season's table
        top_ties = np.mean([pd.Series(np.sort(r.values)[::-1][:20]).duplicated().mean() for _, r in season_tot.iterrows()])
        # where the fixed players would finish among the crowd (percentile, season average)
        fixed, fixed_tot = {}, {}
        for k, (fh, fa) in {"FHA Model (best pick)": best_picks(pred, rule), **fx}.items():
            fp = pd.Series(rule_points(rule, fh, fa, h, a)).groupby(rnd).sum()
            ft = fp.groupby(season_of_round.values).sum()
            fixed[k] = np.mean([(season_tot.loc[s].values < ft[s]).mean() for s in ft.index])
            fixed_tot[k] = ft.mean()
        rows.append({"rule": rule, "max/round": max_round,
                     "avg round": per_round.values.mean(), "round spread (sd)": rsd,
                     "rounds on 0": zero, "skill shows (rank corr)": skill, "ties in top 20": top_ties,
                     **{f"{k.split(' (')[0]} pct": v for k, v in fixed.items()},
                     "Model minus fav-by-one": fixed_tot["FHA Model (best pick)"] - fixed_tot["Favourite by one (2-1 / 1-2)"]})
    res = pd.DataFrame(rows).set_index("rule")
    pd.set_option("display.width", 220, "display.max_columns", 20, "display.float_format", "{:.2f}".format)
    print(res.to_string())
    print("\nskill shows: rank correlation between a player's true forecasting skill and season total "
          "(1 = the table orders players by skill exactly).\n'... pct': share of the simulated crowd that "
          "player would beat over a season.")


if __name__ == "__main__":
    main()
