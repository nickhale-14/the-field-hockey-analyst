"""
Which sides are much better at home than away, and which are the reverse? And does 2026/27 so far agree?

Every national league (Premier Division, Division 1, Conferences), league games only: Prem Phase 1
and Phase 2, and the full lower-tier double round robins. Finals Weekend, League Finals pools and
play-offs are dropped (hosted or neutral venues).

Per side (teams.id, so a club's men's and women's sides are separate) and season:
  home and away W-D-L, points per game, goal difference per game, and the split
  (home PPG minus away PPG).

A side plays 8-9 home games a season, so one season's split is mostly noise. Two checks on that:
  1. Year-on-year: does a side's split in one season predict its split the next? (correlation)
  2. Shuffle test: re-deal each side's games between "home" and "away" at random (same counts) and
     count how many sides look consistent by chance, versus how many really do.
The lower tiers have a league-wide home edge (about +0.3 PPG) and the Prem does not, so every split is
measured against its tier's norm that season (the "excess"); otherwise "best at home" just means
"plays in a Conference". A side is "consistent" when its excess points the same way in all three full
seasons and the pooled excess is at least 0.5 points per game.

    docker compose exec jupyter python3 /home/jovyan/work/home_away_splits.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

import predictor as P

FULL = ["2023/24", "2024/25", "2025/26"]
NOW = "2026/27"
MIN_SPLIT = 0.5
RNG = np.random.default_rng(2026)
SHUFFLES = 2_000


def load() -> pd.DataFrame:
    sql = text("""
        SELECT s.name AS season, c.name AS comp, m.home_team_id AS h, m.away_team_id AS a,
               m.home_score AS hs, m.away_score AS as_
        FROM matches m JOIN seasons s ON s.id = m.season_id JOIN competitions c ON c.id = m.competition_id
        WHERE m.status = 'R' AND s.name IN :seasons
          AND c.name NOT LIKE '%%Final%%' AND c.name NOT LIKE '%%Play%%'
    """).bindparams(seasons=tuple(FULL + [NOW]))
    teams = text("SELECT t.id, t.name FROM teams t")
    with create_engine(P.DATABASE_URL).connect() as conn:
        df = pd.read_sql(sql, conn)
        names = pd.read_sql(teams, conn).set_index("id").name
    df["gender"] = np.where(df.comp.str.lower().str.contains("women"), "W", "M")
    df["league"] = (df.comp.str.replace(r"^(Open - |Vitality )", "", regex=True)
                    .str.replace("Womens", "Women's")
                    .str.replace(r" Phase 2.*$", "", regex=True))
    # one row per side per game
    home = pd.DataFrame({"season": df.season, "league": df.league, "gender": df.gender, "team": df.h,
                         "venue": "H", "gf": df.hs, "ga": df.as_})
    away = pd.DataFrame({"season": df.season, "league": df.league, "gender": df.gender, "team": df.a,
                         "venue": "A", "gf": df.as_, "ga": df.hs})
    g = pd.concat([home, away], ignore_index=True)
    g["pts"] = np.where(g.gf > g.ga, 3, np.where(g.gf == g.ga, 1, 0))
    g["gd"] = g.gf - g.ga
    g["name"] = g.team.map(names) + " (" + g.gender + ")"
    return g


def splits(g: pd.DataFrame) -> pd.DataFrame:
    """Per side and season: home/away record, PPG, GD/g and the split."""
    rows = []
    for (season, team), d in g.groupby(["season", "team"]):
        h, a = d[d.venue == "H"], d[d.venue == "A"]
        if len(h) == 0 or len(a) == 0:
            continue
        rec = lambda x: f"{(x.pts == 3).sum()}-{(x.pts == 1).sum()}-{(x.pts == 0).sum()}"
        rows.append({"season": season, "team": team, "name": d.name.iat[0], "league": d.league.mode().iat[0],
                     "gender": d.gender.iat[0], "nh": len(h), "na": len(a),
                     "h_rec": rec(h), "a_rec": rec(a),
                     "h_ppg": h.pts.mean(), "a_ppg": a.pts.mean(),
                     "h_gd": h.gd.mean(), "a_gd": a.gd.mean()})
    s = pd.DataFrame(rows)
    s["split"] = s.h_ppg - s.a_ppg
    s["gd_split"] = s.h_gd - s.a_gd
    return add_excess(s)


def tier(league: pd.Series) -> np.ndarray:
    return np.where(league.str.contains("Premier"), "Premier",
                    np.where(league.str.contains("Division 1"), "Division 1", "Conference"))


def add_excess(s: pd.DataFrame) -> pd.DataFrame:
    """Split minus the average split of that tier, gender and season."""
    s = s.copy()
    s["tier"] = tier(s.league)
    key = [s.season, s.tier, s.gender]
    s["excess"] = s.split - s.groupby(key).split.transform("mean")
    s["gd_excess"] = s.gd_split - s.groupby(key).gd_split.transform("mean")
    return s


def consistent(s: pd.DataFrame) -> pd.DataFrame:
    """Sides in all three full seasons: pooled split and whether every season points the same way."""
    f = s[s.season.isin(FULL)]
    f = f[f.groupby("team").season.transform("nunique") == len(FULL)]
    agg = f.groupby("team").agg(name=("name", "first"), gender=("gender", "first"),
                                split=("excess", "mean"), gd_split=("gd_excess", "mean"),
                                pos=("excess", lambda x: (x > 0).all()), neg=("excess", lambda x: (x < 0).all()))
    return agg


def count_consistent(agg: pd.DataFrame) -> tuple[int, int]:
    return int(((agg.pos) & (agg.split >= MIN_SPLIT)).sum()), int(((agg.neg) & (agg.split <= -MIN_SPLIT)).sum())


def shuffle_null(g: pd.DataFrame) -> np.ndarray:
    """Re-deal venue labels within each side-season (counts kept); how many 'consistent' sides appear."""
    f = g[g.season.isin(FULL)].copy()
    keep = f.groupby("team").season.transform("nunique") == len(FULL)
    f = f[keep].reset_index(drop=True)
    grp = f.groupby(["season", "team"]).indices
    meta = f.assign(tier=tier(f.league)).groupby(["season", "team"]).agg(
        tier=("tier", "first"), gender=("gender", "first")).reset_index()
    out = []
    for _ in range(SHUFFLES):
        venue = f.venue.values.copy()
        for idx in grp.values():
            venue[idx] = RNG.permutation(venue[idx])
        f["venue"] = venue
        hp = f[f.venue == "H"].groupby(["season", "team"]).pts.mean()
        ap = f[f.venue == "A"].groupby(["season", "team"]).pts.mean()
        sp = (hp - ap).rename("split").reset_index().merge(meta, on=["season", "team"])
        sp["split"] -= sp.groupby(["season", "tier", "gender"]).split.transform("mean")
        agg = sp.groupby("team").split.agg(["mean", lambda x: (x > 0).all(), lambda x: (x < 0).all()])
        agg.columns = ["split", "pos", "neg"]
        out.append(count_consistent(agg))
    return np.array(out)


def line(r) -> str:
    return (f"   {r.season}  {r.league:<34} home {r.h_rec:>6} {r.h_ppg:.2f} ppg {r.h_gd:+.1f} gd   "
            f"away {r.a_rec:>6} {r.a_ppg:.2f} ppg {r.a_gd:+.1f} gd   split {r.split:+.2f} (v tier {r.excess:+.2f})")


def main() -> None:
    g = load()
    s = splits(g)

    print("=== League norm: average side's home PPG minus away PPG, full seasons ===")
    norm = s[s.season.isin(FULL)]
    for (tier, gen), d in norm.groupby(["tier", "gender"]):
        print(f"   {gen} {tier:<11} {d.split.mean():+.2f} ppg   {d.gd_split.mean():+.2f} gd/g   ({len(d)} side-seasons)")

    print("\n=== Is a side's split repeatable? Correlation of excess split, season t v t+1 (same side) ===")
    piv = s.pivot_table(index="team", columns="season", values="excess")
    for a, b in zip(FULL, FULL[1:] + [NOW]):
        both = piv[[a, b]].dropna()
        print(f"   {a} -> {b}: r = {both[a].corr(both[b]):+.2f}  ({len(both)} sides)")

    agg = consistent(s)
    real_pos, real_neg = count_consistent(agg)
    null = shuffle_null(g)
    print(f"\n=== Shuffle test: sides in all three seasons = {len(agg)} ===")
    for lab, real, col in (("home-strong", real_pos, 0), ("away-strong", real_neg, 1)):
        print(f"   Consistent {lab}: real {real}, chance average {null[:, col].mean():.1f} "
              f"(95% range {np.percentile(null[:, col], 2.5):.0f} to {np.percentile(null[:, col], 97.5):.0f}); "
              f"share of shuffles with at least as many: {(null[:, col] >= real).mean():.3f}")

    now = s[s.season == NOW].set_index("team")
    for lab, mask, asc in (("BETTER AT HOME THAN AWAY", (agg.pos) & (agg.split >= MIN_SPLIT), False),
                           ("BETTER AWAY THAN AT HOME", (agg.neg) & (agg.split <= -MIN_SPLIT), True)):
        sel = agg[mask].sort_values("split", ascending=asc)
        print(f"\n=== {lab}: same direction in all three seasons, pooled excess {'>=' if not asc else '<='} "
              f"{'+' if not asc else '-'}{MIN_SPLIT} ppg ({len(sel)} sides) ===")
        for team, r in sel.iterrows():
            print(f"\n{r['name']}  pooled excess over tier norm {r.split:+.2f} ppg, {r.gd_split:+.2f} gd/g")
            for _, row in s[(s.team == team)].sort_values("season").iterrows():
                print(line(row) + ("   <- so far" if row.season == NOW else ""))
            if team not in now.index:
                print(f"   {NOW}  no home and away games yet")
            else:
                n = now.loc[team]
                agrees = (n.excess > 0) == (r.split > 0) and n.excess != 0
                print(f"   -> {NOW} {'AGREES' if agrees else 'does not agree'} so far "
                      f"({n.nh} home, {n.na} away games)")

    print("\nLeague games only (Prem Phase 1 + 2, lower-tier round robins). Finals, pools and play-offs excluded.")


if __name__ == "__main__":
    main()
