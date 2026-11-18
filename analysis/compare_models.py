"""Elo v Poisson v 50/50 blend, Prem, out of sample, per season. Read-only.
Elo: ratings walked forward, translator fitted on earlier seasons only. Poisson: weekly rolling predictions.

    # needs predictor.py --test-season <each season> --out output/compare_{M,W}, with the TUNED settings passed in
    docker compose exec jupyter python3 /home/jovyan/work/compare_models.py
"""
import os

import numpy as np
import pandas as pd
import prem_elo as pe

SEASONS = ["2023/24", "2024/25", "2025/26", "2026/27"]
E, Po = ["e_h", "e_d", "e_a"], ["p_home", "p_draw", "p_away"]


def elo(g, season):
    rows = pe.Ratings(**pe.TUNED[g]).run(pe.load(g))
    train = pe.logit_rows(rows[rows.season < season])
    b, c0, c1 = pe.fit_ordered_logit(train["diff"].to_numpy(), train["outcome"].to_numpy())
    te = rows[rows.season == season].copy()
    p = pe.probs(te["diff"].to_numpy(), b, c0, c1)
    te["e_h"], te["e_d"], te["e_a"] = p[:, 0], p[:, 1], p[:, 2]
    te["day"] = pd.to_datetime(te["date"]).dt.date.astype(str)
    return te[["day", "home", "away", "outcome", "e_h", "e_d", "e_a"]]


def score(p, y):
    idx = np.arange(len(y))
    return ((p.argmax(1) == y).sum(), -np.log(np.clip(p[idx, y], 1e-9, 1)), ((p - np.eye(3)[y]) ** 2).sum(1))


def boot_diff(a, b, n=5000, seed=0):
    """95% interval for mean(a - b), resampling games."""
    rng = np.random.default_rng(seed)
    d = a - b
    m = d[rng.integers(0, len(d), (n, len(d)))].mean(1)
    return np.percentile(m, [2.5, 97.5])


for g, label in (("M", "Men's/Open"), ("W", "Women's")):
    frames = []
    print(f"\n=== {label} ===")
    print(f"{'season':<10}{'games':>6} | {'correct: Elo':>13}{'Poisson':>9}{'Blend':>7} | {'log-loss: Elo':>14}{'Poisson':>9}{'Blend':>8} | {'Brier: Elo':>11}{'Poisson':>9}{'Blend':>8}")
    for s in SEASONS:
        path = f"/home/jovyan/work/output/compare_{g}/predictions_{g}_{s.replace('/', '-')}.csv"
        if not os.path.exists(path):
            print(s, "no Poisson file")
            continue
        po = pd.read_csv(path)
        po = po[po.tier == "Premier"].rename(columns={"date": "day"})
        po["day"] = po["day"].astype(str)
        e = elo(g, s)
        m = po.merge(e, on=["day", "home", "away"], how="inner")
        m["season"] = s
        lost = len(e) - len(m)
        frames.append(m)
        y = m.outcome.to_numpy()
        pe_, pp = m[E].to_numpy(), m[Po].to_numpy()
        ce, le, be = score(pe_, y)
        cp, lp, bp = score(pp, y)
        cb, lb, bb = score((pe_ + pp) / 2, y)
        print(f"{s:<10}{len(m):>6} | {ce:>13}{cp:>9}{cb:>7} | {le.mean():>14.3f}{lp.mean():>9.3f}{lb.mean():>8.3f} | {be.mean():>11.3f}{bp.mean():>9.3f}{bb.mean():>8.3f}  (unmatched Elo rows {lost})")
    a = pd.concat(frames, ignore_index=True)
    y = a.outcome.to_numpy()
    pe_, pp = a[E].to_numpy(), a[Po].to_numpy()
    ce, le, be = score(pe_, y)
    cp, lp, bp = score(pp, y)
    cb, lb, bb = score((pe_ + pp) / 2, y)
    n = len(a)
    print(f"{'ALL':<10}{n:>6} | {ce:>13}{cp:>9}{cb:>7} | {le.mean():>14.3f}{lp.mean():>9.3f}{lb.mean():>8.3f} | {be.mean():>11.3f}{bp.mean():>9.3f}{bb.mean():>8.3f}")
    print(f"accuracy: Elo {ce/n:.1%}  Poisson {cp/n:.1%}  Blend {cb/n:.1%}")
    for name, x, z in (("Elo - Poisson", le, lp), ("Elo - Blend", le, lb), ("Poisson - Blend", lp, lb)):
        lo, hi = boot_diff(x, z)
        print(f"log-loss {name:<16} {x.mean() - z.mean():+.3f}  (95% {lo:+.3f} to {hi:+.3f})")
    split = pe_.argmax(1) != pp.argmax(1)
    print(f"models backed different sides in {split.sum()} of {n}: Elo right {(pe_.argmax(1)[split] == y[split]).sum()}, Poisson right {(pp.argmax(1)[split] == y[split]).sum()}")
    print("said v happened (draws): " + f"Elo {pe_[:, 1].sum():.0f}, Poisson {pp[:, 1].sum():.0f}, Blend {((pe_ + pp) / 2)[:, 1].sum():.0f}, actual {(y == 1).sum()}")
    print("calibration of the top pick (stated chance -> right):")
    for nm, p in (("Elo", pe_), ("Poisson", pp), ("Blend", (pe_ + pp) / 2)):
        conf, pick = p.max(1), p.argmax(1)
        parts = []
        for lo, hi in ((0, .55), (.55, .7), (.7, .85), (.85, 1.01)):
            s_ = (conf >= lo) & (conf < hi)
            if s_.any():
                parts.append(f"{lo:.0%}+: n={s_.sum()} said {conf[s_].mean():.0%} right {(pick[s_] == y[s_]).mean():.0%}")
        print(f"  {nm:<8}" + " | ".join(parts))
