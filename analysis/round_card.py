"""
Round card: what both models said about a Prem round, and (once the results
are in) how they did. The pipeline for The Call (before) and The Verdict
(after).

Both models are rebuilt from only what was known at --as-of, so a round can be
marked honestly after the event: Elo + ordered logit from prem_elo.py, Poisson
expected goals and likeliest score from predictor.py (via simulate.prepare).

    # before the round: predictions only
    docker compose exec jupyter python3 /home/jovyan/work/round_card.py --from 2026-10-24 --to 2026-10-25
    # after it: same command, results now in the DB, so each game is marked
    docker compose exec jupyter python3 /home/jovyan/work/round_card.py --from 2026-10-17 --to 2026-10-18 --as-of 2026-10-12

--as-of defaults to the day before --from. Rerunning with the same --as-of
after the results arrive reproduces the original predictions exactly, since
both models only ever look at matches before it.
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

import predictor as P
import prem_elo as pe
import simulate as S

LABEL = "HDA"


def rounds(prem: pd.DataFrame, season: str) -> dict[int, int]:
    """Round number of each Phase 1 match: its place in the home side's fixture list (every team
    plays once a round, so this matches the league's own Match 1-11; see content/series/ROUNDS.md)."""
    p1 = S.phase1(prem, season)
    long = pd.concat([p1[["match_id", "date", "home_team_id"]].rename(columns={"home_team_id": "t"}),
                      p1[["match_id", "date", "away_team_id"]].rename(columns={"away_team_id": "t"})], ignore_index=True)
    long["n"] = long.sort_values(["date", "match_id"]).groupby("t").cumcount() + 1
    return long.groupby("match_id")["n"].max().to_dict()


def card(gender: str, start: pd.Timestamp, end: pd.Timestamp, as_of: pd.Timestamp) -> list[dict]:
    prem = S.load_prem(gender)
    season = prem.season.max()
    prep = S.prepare(gender, prem, season, as_of)
    round_of = rounds(prem, season)
    b, c0, c1 = prep["logit"]
    fx = prep["remaining"]
    fx = fx[(fx.date >= start) & (fx.date < end + pd.Timedelta(days=1))].sort_values("date")
    title = "Men's/Open" if gender == "M" else "Women's"
    print(f"\n=== {title} Premier Division, {start:%d %b}-{end:%d %b}, models as of {as_of:%a %d %b %H:%M} ===")
    if fx.empty:
        print("  no Phase 1 fixtures in that window")
        return []

    marks = {"Elo": [], "Poisson": []}
    records = []
    for r in fx.itertuples(index=False):
        rh, ra = prep["rating"][r.home_team_id], prep["rating"][r.away_team_id]
        p_elo = pe.probs(np.array([rh + prep["params"]["home"] - ra]), b, c0, c1)[0]
        ph, pd_, pa, (sh, sa) = P.outcome_probs(r.xg_home, r.xg_away, prep["coupling"])
        p_poi = np.array([ph, pd_, pa])
        played = r.status == "R"
        score = f"{int(r.home_score)}-{int(r.away_score)}" if played else "v"
        print(f"{r.date:%a %H:%M}  {r.home} ({rh:.0f}) {score} {r.away} ({ra:.0f})")
        y = None
        if played:
            y = 0 if r.home_score > r.away_score else (1 if r.home_score == r.away_score else 2)
        records.append({
            "gender": gender, "season": season, "match_id": int(r.match_id), "round": int(round_of[r.match_id]),
            "date": f"{r.date:%Y-%m-%d %H:%M}", "status": r.status,
            "home_team_id": int(r.home_team_id), "away_team_id": int(r.away_team_id), "home": r.home, "away": r.away,
            "elo": {"home": round(float(rh), 1), "away": round(float(ra), 1), "p": [round(float(x), 4) for x in p_elo]},
            "poisson": {"xg": [round(float(r.xg_home), 3), round(float(r.xg_away), 3)],
                        "p": [round(float(x), 4) for x in p_poi], "likeliest": [sh, sa],
                        "likeliest_p": round(float(P.score_grid(r.xg_home, r.xg_away, prep["coupling"])[sh, sa]), 4)},
            "result": [int(r.home_score), int(r.away_score)] if played else None,
        })
        for name, p, extra in (("Elo", p_elo, ""),
                               ("Poisson", p_poi, f"   xG {r.xg_home:.1f}-{r.xg_away:.1f}, likeliest {sh}-{sa}")):
            verdict = ""
            if y is not None:
                hit = p.argmax() == y
                marks[name].append((hit, -np.log(p[y])))
                verdict = "  hit " if hit else "  MISS"
            print(f"    {name:<8} H {p[0]:.0%}  D {p[1]:.0%}  A {p[2]:.0%}  -> {LABEL[p.argmax()]}{verdict}{extra}")

    if marks["Elo"]:
        n = len(marks["Elo"])
        print(f"  marked {n} of {len(fx)} played:")
        for name, m in marks.items():
            print(f"    {name:<8} {sum(h for h, _ in m)}/{n} right, log-loss {np.mean([l for _, l in m]):.3f} "
                  f"(flat 1-in-3 guess 1.099; lower is better)")
    print("  Neither model ever picks a draw; picks under ~55% have hit about a third of the time.")
    return records


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", required=True, help="First day of the round, YYYY-MM-DD")
    ap.add_argument("--to", dest="end", default=None, help="Last day (default: same as --from)")
    ap.add_argument("--as-of", default=None, help="What the models may know: matches before this (default: day before --from)")
    ap.add_argument("--gender", choices=["M", "W", "both"], default="both")
    ap.add_argument("--json", default=None, help="Also write every fixture's numbers here (for the weekly graphics)")
    args = ap.parse_args()
    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end) if args.end else start
    as_of = pd.Timestamp(args.as_of) if args.as_of else start - pd.Timedelta(days=1)
    records = []
    for g in ("M", "W") if args.gender == "both" else (args.gender,):
        records += card(g, start, end, as_of)
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"from": f"{start:%Y-%m-%d}", "to": f"{end:%Y-%m-%d}", "as_of": f"{as_of:%Y-%m-%d %H:%M}",
                       "fixtures": records}, f, indent=1)


if __name__ == "__main__":
    main()
