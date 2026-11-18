"""
How sure are we that home advantage is real in the Premier Division?

League matches only: Phase 1 and Phase 2 (Top 6 / Lower 6). Finals Weekend, the League Finals pools
and the play-offs are dropped, since they are hosted at one club's ground or a neutral one.

Three views, per gender, with 95% intervals from 10,000 bootstrap resamples:
  1. Raw: home goal difference per game, home win / draw / away win rates.
  2. Paired: every pair of teams that played at both grounds in the same season (Phase 1 is a single
     round robin since 2021/22, so the return game is often in Phase 2). Averaging the home-view goal
     difference at each ground cancels team strength, so what's left is the venue.
  3. By season, to see whether it moves.
Plus a sign-flip permutation test on the paired numbers: if venue made no difference, which side was
at home is a coin flip, so flip it at random and see how often the edge is as big as the real one.

    docker compose exec jupyter python3 /home/jovyan/work/home_advantage.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

import predictor as P

RNG = np.random.default_rng(2026)
B = 10_000


def load_lower() -> pd.DataFrame:
    """Division 1 and the Conferences, league games only, for comparison."""
    sql = text("""
        SELECT s.name AS season, c.name AS comp, m.home_score - m.away_score AS gd
        FROM matches m JOIN seasons s ON s.id = m.season_id JOIN competitions c ON c.id = m.competition_id
        WHERE m.status = 'R' AND (c.name LIKE '%%Division 1%%' OR c.name LIKE '%%Conference%%')
          AND c.name NOT LIKE '%%Final%%' AND c.name NOT LIKE '%%Play%%'
    """)
    with create_engine(P.DATABASE_URL).connect() as conn:
        df = pd.read_sql(sql, conn)
    df["gender"] = np.where(df.comp.str.lower().str.contains("women"), "W", "M")
    df["tier"] = np.where(df.comp.str.contains("Division 1"), "Division 1", "Conference")
    return df


def load() -> pd.DataFrame:
    sql = text("""
        SELECT m.id, s.name AS season, c.name AS comp, m.home_team_id AS h, m.away_team_id AS a,
               m.home_score AS hs, m.away_score AS as_
        FROM matches m JOIN seasons s ON s.id = m.season_id JOIN competitions c ON c.id = m.competition_id
        WHERE m.status = 'R' AND c.name LIKE '%%Premier Division%%'
          AND c.name NOT LIKE '%%Final%%' AND c.name NOT LIKE '%%Play%%'
    """)
    with create_engine(P.DATABASE_URL).connect() as conn:
        df = pd.read_sql(sql, conn)
    df["gender"] = np.where(df.comp.str.lower().str.contains("women"), "W", "M")
    df["phase"] = np.where(df.comp.str.contains("Phase 2"), 2, 1)
    df["gd"] = df.hs - df.as_
    return df


def ci(x: np.ndarray, stat=np.mean) -> tuple[float, float, float]:
    x = np.asarray(x, dtype=float)
    boots = np.array([stat(RNG.choice(x, len(x))) for _ in range(B)])
    return stat(x), *np.percentile(boots, [2.5, 97.5])


def pairs(df: pd.DataFrame) -> pd.DataFrame:
    """Pairs that played at both grounds in a season (any league phase): the home-view goal difference
    averaged at each ground, then the two grounds averaged, so a pair counts once however often it met."""
    d = df.copy()
    d["key"] = [tuple(sorted(t)) for t in zip(d.h, d.a)]
    per_ground = d.groupby(["season", "key", "h"]).gd.mean().reset_index()
    both = per_ground.groupby(["season", "key"]).filter(lambda x: len(x) == 2)
    return both.groupby(["season", "key"]).agg(edge=("gd", "mean")).reset_index()


def permutation_p(edge: np.ndarray) -> float:
    """Two-sided: share of random home/away assignments with an edge at least as big as the real one."""
    flips = RNG.choice([-1, 1], size=(B, len(edge)))
    null = np.abs((flips * edge).mean(axis=1))
    return float((null >= abs(edge.mean())).mean())


def report(df: pd.DataFrame, g: str) -> None:
    d = df[df.gender == g]
    title = "Men's/Open" if g == "M" else "Women's"
    print(f"\n=== {title} Premier Division, league matches {d.season.min()} to {d.season.max()}: {len(d)} games ===")

    m, lo, hi = ci(d.gd.values)
    print(f"1. Home goal difference per game: {m:+.2f}  (95% interval {lo:+.2f} to {hi:+.2f})")
    hw, dr, aw = (d.gd > 0).mean(), (d.gd == 0).mean(), (d.gd < 0).mean()
    dec = d[d.gd != 0]
    s, slo, shi = ci((dec.gd > 0).astype(float).values)
    print(f"   Home win {hw:.0%}, draw {dr:.0%}, away win {aw:.0%}. "
          f"Home side won {s:.0%} of decisive games (95% {slo:.0%} to {shi:.0%}; no edge = 50%)")
    hp = np.where(d.gd > 0, 3, np.where(d.gd == 0, 1, 0)).mean()
    ap = np.where(d.gd < 0, 3, np.where(d.gd == 0, 1, 0)).mean()
    print(f"   Points per game: home {hp:.2f}, away {ap:.2f}")

    pr = pairs(d)
    m, lo, hi = ci(pr.edge.values)
    p = permutation_p(pr.edge.values)
    print(f"2. Paired (same two teams, both venues, same season): {len(pr)} pairs")
    print(f"   Venue worth {m:+.2f} goals a game  (95% interval {lo:+.2f} to {hi:+.2f}); "
          f"permutation p = {p:.4f}" + ("  (none of 10,000 shuffles matched it)" if p == 0 else ""))

    print("3. By season (raw home goal difference per game, and paired edge):")
    for season, sd in d.groupby("season"):
        m, lo, hi = ci(sd.gd.values)
        sp = pairs(sd)
        paired = f"paired {sp.edge.mean():+.2f} ({len(sp)} pairs)" if len(sp) else "no pairs"
        print(f"   {season}  {len(sd):>3} games  {m:+.2f}  ({lo:+.2f} to {hi:+.2f})   {paired}"
              + ("   COVID season, cut short" if season == "2020/21" else ""))


def main() -> None:
    df = load()
    for g in ("M", "W"):
        report(df, g)
    low = load_lower()
    print("\n=== For comparison: the divisions below, league games, home goal difference per game ===")
    for (g, tier), d in low.groupby(["gender", "tier"]):
        m, lo, hi = ci(d.gd.values)
        dec = d[d.gd != 0]
        print(f"   {'Men' if g == 'M' else 'Women'} {tier:<11} {len(d):>5} games  {m:+.2f}  ({lo:+.2f} to {hi:+.2f})"
              f"   home won {(dec.gd > 0).mean():.0%} of decisive games")
    print("\nIntervals: 10,000 bootstrap resamples of games (or pairs). Excludes Finals Weekend, League Finals"
          " pools and play-offs (hosted or neutral venues).")


if __name__ == "__main__":
    main()
