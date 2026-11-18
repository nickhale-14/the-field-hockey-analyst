"""
Is one combined call better than either model alone? Premier Division, both genders.

Combined = weighted average of the two models' home/draw/away chances:
    p = w * Elo (power rankings) + (1 - w) * Poisson (score model)
The weight is chosen on 2024/25 and tested once on 2025/26, so the test season never picks it.
All predictions are out of sample: Poisson from predictor.py's weekly rolling backtest CSVs, Elo from
ratings walked forward match by match with its result translator fitted on earlier seasons only.

The combined scoreline: the score model's most likely score among those where the combined pick wins
(the same rule simulate.py uses), so score and pick can never disagree.

    # needs the Poisson backtest CSVs (predictor.py --test-season 2024/25 and 2025/26, TUNED settings)
    docker compose exec jupyter python3 /home/jovyan/work/blend_backtest.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import predictor as P
import prem_elo as pe

POISSON_CSV = {  # (gender, season) -> rolling backtest predictions, current TUNED settings
    ("M", "2024/25"): "/home/jovyan/work/output/blend_M/predictions_M_2024-25.csv",
    ("W", "2024/25"): "/home/jovyan/work/output/blend_W/predictions_W_2024-25.csv",
    ("M", "2025/26"): "/home/jovyan/work/output/ha_Mnew/predictions_M_2025-26.csv",
    ("W", "2025/26"): "/home/jovyan/work/output/ha_Wold/predictions_W_2025-26.csv",
}
COUPLING = {"M": 0.0, "W": 0.08}  # fitted goal coupling, roughly (predictor.py backtest output)
WEIGHTS = np.round(np.arange(0, 1.01, 0.1), 1)


def elo_probs(gender: str, season: str) -> pd.DataFrame:
    df = pe.load(gender)
    rows = pe.Ratings(**pe.TUNED[gender]).run(df)
    train = pe.logit_rows(rows[rows.season < season])
    b, c0, c1 = pe.fit_ordered_logit(train["diff"].to_numpy(), train["outcome"].to_numpy())
    te = rows[rows.season == season].copy()
    p = pe.probs(te["diff"].to_numpy(), b, c0, c1)
    te["e_h"], te["e_d"], te["e_a"] = p[:, 0], p[:, 1], p[:, 2]
    te["day"] = pd.to_datetime(te["date"]).dt.date.astype(str)
    return te[["day", "home", "away", "outcome", "e_h", "e_d", "e_a"]]


def both(gender: str, season: str) -> pd.DataFrame:
    po = pd.read_csv(POISSON_CSV[(gender, season)])
    po = po[po.tier == "Premier"].rename(columns={"date": "day"})
    po["day"] = po["day"].astype(str)
    m = po.merge(elo_probs(gender, season), on=["day", "home", "away"], how="inner")
    return m


def metrics(p: np.ndarray, y: np.ndarray) -> dict:
    return {"acc": float((p.argmax(1) == y).mean()),
            "ll": float(-np.log(np.clip(p[np.arange(len(y)), y], 1e-9, 1)).mean()),
            "brier": float(((p - np.eye(3)[y]) ** 2).sum(1).mean())}


def restricted_score(xh: float, xa: float, side: int, coupling: float) -> tuple[int, int]:
    """Most likely scoreline in which `side` (0 home, 2 away) wins."""
    g = P.score_grid(xh, xa, coupling)
    mask = np.tril(np.ones_like(g, dtype=bool), -1) if side == 0 else np.triu(np.ones_like(g, dtype=bool), 1)
    h, a = np.unravel_index(int(np.argmax(np.where(mask, g, -1))), g.shape)
    return int(h), int(a)


def report(gender: str) -> None:
    title = "Men's/Open" if gender == "M" else "Women's"
    val, test = both(gender, "2024/25"), both(gender, "2025/26")
    print(f"\n=== {title} Premier Division: {len(val)} games in 2024/25 (choose weight), {len(test)} in 2025/26 (test) ===")

    def stack(d, w):
        return w * d[["e_h", "e_d", "e_a"]].to_numpy() + (1 - w) * d[["p_home", "p_draw", "p_away"]].to_numpy()

    yv, yt = val.outcome.to_numpy(), test.outcome.to_numpy()
    print("Weight on Elo   2024/25 log-loss   2025/26 log-loss  acc")
    lls = {}
    for w in WEIGHTS:
        mv, mt = metrics(stack(val, w), yv), metrics(stack(test, w), yt)
        lls[w] = mv["ll"]
        print(f"   {w:.1f}            {mv['ll']:.3f}              {mt['ll']:.3f}         {mt['acc']:.1%}")
    w_best = min(lls, key=lls.get)

    print(f"\n2025/26 test (weight chosen on 2024/25: {w_best:.1f} on Elo):")
    rows = [("Score model alone", 0.0), ("Rankings (Elo) alone", 1.0), ("50/50 blend", 0.5), (f"Blend at {w_best:.1f}", w_best)]
    for name, w in rows:
        m = metrics(stack(test, w), yt)
        print(f"   {name:<22} accuracy {m['acc']:.1%}   log-loss {m['ll']:.3f}   Brier {m['brier']:.3f}")

    p = stack(test, w_best)
    pick = p.argmax(1)
    conf = p.max(1)
    pp = test[["p_home", "p_draw", "p_away"]].to_numpy().argmax(1)
    ep = test[["e_h", "e_d", "e_a"]].to_numpy().argmax(1)
    split = pp != ep
    print(f"\n   Models backed different teams in {split.sum()} of {len(test)} games; "
          f"score model right {(pp[split] == yt[split]).sum()}, rankings right {(ep[split] == yt[split]).sum()}, "
          f"blend right {(pick[split] == yt[split]).sum()}")

    print("   Blend by confidence of its pick:")
    for lo, hi in ((0, 0.55), (0.55, 0.70), (0.70, 0.85), (0.85, 1.01)):
        s = (conf >= lo) & (conf < hi)
        if s.any():
            print(f"     {lo:.0%}-{min(hi, 1):.0%}: {s.sum():>3} games, pick right {(pick[s] == yt[s]).mean():.0%}, "
                  f"average stated chance {conf[s].mean():.0%}")

    exact_free = exact_tied = 0
    for r, side in zip(test.itertuples(index=False), pick):
        actual = (int(r.home_score), int(r.away_score))
        exact_free += r.pred_score == f"{actual[0]}-{actual[1]}"
        exact_tied += restricted_score(r.xg_home, r.xg_away, side, COUPLING[gender]) == actual
    print(f"   Exact scores: score model's likeliest {exact_free}/{len(test)}, "
          f"combined call's score (likeliest where the pick wins) {exact_tied}/{len(test)}")


def main() -> None:
    for g in ("M", "W"):
        report(g)
    print("\nLog-loss: lower is better (flat 1-in-3 guess 1.099). Brier: lower is better (flat 0.667).")


if __name__ == "__main__":
    main()
