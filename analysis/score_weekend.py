"""
Mark the predictor's card for a set of fixtures that have since been played.

Joins a snapshot of pre-match predictions (the CSV written by
`predictor.py --upcoming`, filtered to the dates you care about) onto the
results now in the database and reports: outcome hit rate, exact scores,
log-loss vs the "always home" and "flat" baselines, and the per-match card.

The predictions file MUST predate the results - the point is to score real
forecasts, not fits. `predictor.py --upcoming` writes one per gender to
output/upcoming_{M,W}_to_<date>.csv; pass one or both.

    python3 score_weekend.py --from 2026-09-19 --to 2026-09-20 \
        output/upcoming_M_to_2026-12-31.csv output/upcoming_W_to_2026-12-31.csv
"""
from __future__ import annotations

import argparse
import math
import os

import pandas as pd
from sqlalchemy import create_engine, text

from predictor import DATABASE_URL


def load_results(start: str, end: str) -> pd.DataFrame:
    """Every match in the window (any status), with team and competition names."""
    q = text(
        """
        SELECT m.starts_at, c.name AS competition,
               th.name AS home, ta.name AS away,
               m.home_score, m.away_score, m.status
        FROM matches m
        JOIN competitions c ON c.id = m.competition_id
        JOIN teams th ON th.id = m.home_team_id
        JOIN teams ta ON ta.id = m.away_team_id
        WHERE DATE(m.starts_at) BETWEEN :s AND :e
        """
    )
    with create_engine(DATABASE_URL).connect() as conn:
        df = pd.read_sql(q, conn, params={"s": start, "e": end})
    df["date"] = pd.to_datetime(df["starts_at"]).dt.strftime("%Y-%m-%d")
    df["gender"] = df["competition"].str.lower().str.contains("women").map({True: "W", False: "M"})
    return df.drop(columns=["starts_at"])


def outcome(h: float, a: float) -> str:
    return "H" if h > a else ("A" if a > h else "D")


def score(pred: pd.DataFrame, res: pd.DataFrame) -> pd.DataFrame:
    df = pred.merge(res, on=["date", "competition", "home", "away"], how="left", suffixes=("", "_res"))
    played = df[df["status"] == "R"].copy()
    played["actual"] = played.apply(lambda r: f"{int(r.home_score)}-{int(r.away_score)}", axis=1)
    played["act_out"] = played.apply(lambda r: outcome(r.home_score, r.away_score), axis=1)
    played["pick"] = played.apply(lambda r: max((("H", r.p_home), ("D", r.p_draw), ("A", r.p_away)), key=lambda t: t[1])[0], axis=1)
    played["p_pick"] = played[["p_home", "p_draw", "p_away"]].max(axis=1)
    played["hit"] = played["pick"] == played["act_out"]
    played["exact"] = played["pred_score"] == played["actual"]
    played["p_actual"] = played.apply(lambda r: {"H": r.p_home, "D": r.p_draw, "A": r.p_away}[r.act_out], axis=1)
    played["logloss"] = -played["p_actual"].clip(lower=1e-6).apply(math.log)
    # Goals: how far off was the expected total?
    played["xg_total"] = played["xg_home"] + played["xg_away"]
    played["goals"] = played["home_score"] + played["away_score"]
    return df, played


def summarise(played: pd.DataFrame) -> str:
    n = len(played)
    out = []
    out.append(f"{n} played, outcome correct {played['hit'].sum()}/{n} = {played['hit'].mean():.1%}, "
               f"exact score {played['exact'].sum()}/{n}")
    hd = (played["act_out"] == "H").mean()
    flat = -math.log(1 / 3)
    out.append(f"log-loss {played['logloss'].mean():.3f} (flat 1/3 baseline {flat:.3f}); "
               f"actual split H {hd:.0%} / D {(played['act_out']=='D').mean():.0%} / A {(played['act_out']=='A').mean():.0%}")
    out.append(f"expected goals per game {played['xg_total'].mean():.2f} vs actual {played['goals'].mean():.2f}")
    # calibration by confidence bucket
    b = pd.cut(played["p_pick"], [0, 0.45, 0.55, 0.65, 1.0], labels=["<45%", "45-55%", "55-65%", ">65%"])
    cal = played.groupby(b, observed=True)["hit"].agg(["count", "mean"])
    out.append("by confidence of the pick:\n" + cal.rename(columns={"count": "n", "mean": "hit rate"}).to_string())
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("preds", nargs="+", help="prediction CSVs from predictor.py --upcoming")
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    args = ap.parse_args()

    pred = pd.concat([pd.read_csv(p) for p in args.preds], ignore_index=True)
    pred = pred[(pred["date"] >= args.start) & (pred["date"] <= args.end)]
    res = load_results(args.start, args.end)
    df, played = score(pred, res)

    pd.set_option("display.width", 220)
    cols = ["date", "home", "away", "pred_score", "actual", "p_pick", "pick", "act_out", "hit", "exact"]
    for comp, g in played.groupby("competition", sort=True):
        print(f"\n== {comp}: {g['hit'].sum()}/{len(g)} outcomes, {g['exact'].sum()} exact ==")
        print(g[cols].to_string(index=False, formatters={"p_pick": "{:.0%}".format}))

    print("\n== overall ==")
    print(summarise(played))
    for gender, g in played.groupby("gender"):
        print(f"\n-- {'Men' if gender == 'M' else 'Women'} --")
        print(summarise(g))

    missing = df[df["status"] != "R"]
    if len(missing):
        print(f"\n{len(missing)} fixtures in the window not played / no result yet:")
        print(missing[["date", "competition", "home", "away", "status"]].fillna("not in DB").to_string(index=False))


if __name__ == "__main__":
    main()
