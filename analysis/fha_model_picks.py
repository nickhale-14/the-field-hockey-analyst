"""The FHA Model's picks for the FHA Weekly Predictor.

Same model as the Top 6 simulation (simulate.py): the RESULT is the power rankings'
likeliest of home/draw/away, then the SCORELINE is the likeliest Poisson score for that
result. Not game_scoring.best_picks (user, 2026-11-17: one FHA Model, not a points-maximiser).

    python round_card.py --from 2026-11-21 --to 2026-11-22 --json /tmp/r.json
    python fha_model_picks.py /tmp/r.json
"""
from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd

import predictor as P
import simulate as S


def pick(elo_p, xg, coupling) -> tuple[str, float, int, int]:
    o = int(np.argmax(elo_p))
    g = P.score_grid(xg[0], xg[1], coupling)
    i, j = np.indices(g.shape)
    w = np.where((i > j, i == j, i < j)[o], g, 0.0)
    h, a = np.unravel_index(w.argmax(), g.shape)
    return "HDA"[o], float(elo_p[o]), int(h), int(a)


def main(path: str) -> None:
    d = json.load(open(path))
    as_of = pd.Timestamp(d["as_of"])
    coup = {}
    for f in d["fixtures"]:
        g = f["gender"]
        if g not in coup:
            prem = S.load_prem(g)
            coup[g] = S.prepare(g, prem, prem.season.max(), as_of)["coupling"]
        res, p, h, a = pick(f["elo"]["p"], f["poisson"]["xg"], coup[g])
        print(f'{g} {f["date"]}  {f["home"]:28s} v {f["away"]:30s} {res} {p:.0%}  pick {h}-{a}')


if __name__ == "__main__":
    main(sys.argv[1])
