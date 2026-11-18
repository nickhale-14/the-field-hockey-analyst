"""
"Luck table": actual points vs the points the predictor expected, per team.

Expected points for a match = 3 x P(win) + 1 x P(draw) from that team's side,
using the rolling (refit-weekly) predictions written by predictor.py - i.e.
what a well-informed forecaster would have expected *before each game*, with
everything known up to that week. A team well above its expected points got
results its underlying strength didn't justify (or clutch, if you prefer);
well below, the reverse. Expected goal difference is shown alongside so you
can see whether the gap came from attack, defence or tight games.

Scope: Prem league-phase matches only (regular season + pools + Phase 2) -
the knockout Finals Weekend and the cross-tier relegation play-offs are
excluded because they aren't table games.

Usage (inside the jupyter container, after predictor.py has run):
    python luck_table.py --gender M
"""
from __future__ import annotations

import argparse

import pandas as pd


def luck_table(pred: pd.DataFrame) -> pd.DataFrame:
    p = pred[
        pred["competition"].str.contains("Premier Division")
        & ~pred["competition"].str.contains("Finals Weekend")
        & ~pred["competition"].str.contains("Relegation")
    ]
    rows = []
    for _, r in p.iterrows():
        for side, opp in (("home", "away"), ("away", "home")):
            gf, ga = r[f"{side}_score"], r[f"{opp}_score"]
            p_win = r["p_home"] if side == "home" else r["p_away"]
            rows.append({
                "team": r[side], "played": 1,
                "pts": 3 if gf > ga else (1 if gf == ga else 0),
                "xpts": 3 * p_win + r["p_draw"],
                "gf": gf, "ga": ga,
                "xgf": r[f"xg_{side}"], "xga": r[f"xg_{opp}"],
                "won": int(gf > ga), "drawn": int(gf == ga), "lost": int(gf < ga),
                "p_win": p_win,
            })
    t = pd.DataFrame(rows).groupby("team").sum(numeric_only=True)
    t["gd"] = t["gf"] - t["ga"]
    t["xgd"] = t["xgf"] - t["xga"]
    t["luck_pts"] = t["pts"] - t["xpts"]
    t["luck_gd"] = t["gd"] - t["xgd"]
    t = t.sort_values("pts", ascending=False)
    t.insert(0, "pos", range(1, len(t) + 1))
    t["xpos"] = t["xpts"].rank(ascending=False, method="min").astype(int)
    cols = ["pos", "xpos", "played", "won", "drawn", "lost", "pts", "xpts", "luck_pts", "gf", "ga", "gd", "xgd", "luck_gd"]
    return t[cols]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gender", choices=["M", "W"], required=True)
    ap.add_argument("--season", default="2025/26")
    ap.add_argument("--out", default="output")
    args = ap.parse_args()
    path = f"{args.out}/predictions_{args.gender}_{args.season.replace('/', '-')}.csv"
    pred = pd.read_csv(path)
    t = luck_table(pred)
    pd.set_option("display.width", 200)
    print(f"{'Men' if args.gender == 'M' else 'Women'}'s Premier Division {args.season} - league phases, expected points from the rolling predictor\n")
    print(t.round({"xpts": 1, "luck_pts": 1, "xgd": 1, "luck_gd": 1}).to_string())
    t.to_csv(f"{args.out}/luck_{args.gender}_{args.season.replace('/', '-')}.csv")


if __name__ == "__main__":
    main()
