"""
Is the men's Premier Division really a "big 5 plus everyone else"?

Builds a season table per year (Phase 1 + Phase 2 = the 16 games that decide
the standings), marks which teams made the Top 6 bracket, and then asks:

1. Is there a repeatable top tier, and where does the gap actually fall?
2. Who takes the last top-6 place, and is Oxted closing on it?
3. What do top-6 teams do better than the rest - attack, defence or set pieces?

Run: docker compose exec jupyter python3 /home/jovyan/work/prem_tiers.py

Caveats enforced here: League Finals Pools and relegation play-offs are
excluded from the table (they are post-season pools, not the league);
2019/20 and 2020/21 are Flashscore scores-only seasons with no phase split;
event-based comparisons use 2024/25+2025/26, the only clean event seasons.
"""
from __future__ import annotations

import pandas as pd
from sqlalchemy import create_engine, text

from predictor import DATABASE_URL

EVENT_SEASONS = ("2024/25", "2025/26")
GOALS = ("FG", "PC", "PS")
CARDS = ("GC", "YC", "RC")
QUARTERS = [(1, 17, "Q1"), (18, 35, "Q2"), (36, 52, "Q3"), (53, 70, "Q4")]

MATCHES_SQL = text(
    """
    SELECT m.id AS match_id, m.source_id, s.name AS season, c.name AS competition,
           m.starts_at, m.home_team_id, m.away_team_id,
           ht.name AS home, at.name AS away, m.home_score, m.away_score
    FROM matches m
    JOIN seasons s ON s.id = m.season_id
    JOIN competitions c ON c.id = m.competition_id
    JOIN teams ht ON ht.id = m.home_team_id
    JOIN teams at ON at.id = m.away_team_id
    WHERE m.status = 'R'
      AND c.name LIKE '%%Premier Division%%'
      AND c.name NOT LIKE '%%Women%%'
      AND c.name NOT LIKE '%%Relegation%%'
      AND c.name NOT LIKE '%%Play Offs Phase 3%%'
      AND c.name NOT LIKE '%%Finals Weekend%%'
      AND c.name NOT LIKE '%%League Finals Pool%%'
    """
)


def load() -> pd.DataFrame:
    with create_engine(DATABASE_URL).connect() as conn:
        df = pd.read_sql(MATCHES_SQL, conn)
    df["phase2"] = df.competition.str.contains("Phase 2")
    df["bracket"] = None
    df.loc[df.competition.str.contains("Top 6"), "bracket"] = "Top 6"
    df.loc[df.competition.str.contains("Lower 6"), "bracket"] = "Lower 6"
    return df


def long_form(df: pd.DataFrame) -> pd.DataFrame:
    """One row per team per match."""
    home = df.assign(team_id=df.home_team_id, team=df.home, opp_id=df.away_team_id,
                     gf=df.home_score, ga=df.away_score, venue="H")
    away = df.assign(team_id=df.away_team_id, team=df.away, opp_id=df.home_team_id,
                     gf=df.away_score, ga=df.home_score, venue="A")
    out = pd.concat([home, away], ignore_index=True)
    out["pts"] = out.apply(lambda r: 3 if r.gf > r.ga else (1 if r.gf == r.ga else 0), axis=1)
    out["win"] = out.gf > out.ga
    return out


def season_tables(lf: pd.DataFrame) -> dict[str, pd.DataFrame]:
    tables = {}
    for season, g in lf.groupby("season"):
        bracket = (g[g.bracket.notna()].groupby("team_id")["bracket"].first())
        t = g.groupby(["team_id", "team"]).agg(
            P=("pts", "size"), W=("win", "sum"),
            D=("pts", lambda s: (s == 1).sum()),
            GF=("gf", "sum"), GA=("ga", "sum"), PTS=("pts", "sum"),
        ).reset_index()
        t["L"] = t.P - t.W - t.D
        t["GD"] = t.GF - t.GA
        t["PPG"] = (t.PTS / t.P).round(2)
        t["bracket"] = t.team_id.map(bracket).fillna("-")
        # Bracket first (Top 6 occupy 1-6), then points within the bracket.
        t["_order"] = t.bracket.map({"Top 6": 0, "Lower 6": 1}).fillna(0)
        t = t.sort_values(["_order", "PTS", "GD"], ascending=[True, False, False])
        t.insert(0, "pos", range(1, len(t) + 1))
        tables[season] = t.drop(columns="_order")
    return tables


def main() -> None:
    pd.set_option("display.width", 220)
    df = load()
    lf = long_form(df)
    tables = season_tables(lf)

    print("=" * 100)
    print("MEN'S PREMIER DIVISION, SEASON BY SEASON (Phase 1 + Phase 2; pools and play-offs excluded)")
    print("=" * 100)
    for season in sorted(tables):
        t = tables[season]
        print(f"\n--- {season} ---")
        print(t[["pos", "team", "P", "W", "D", "L", "GF", "GA", "GD", "PTS", "PPG", "bracket"]].to_string(index=False))

    # 1. Who keeps making the top 6?
    print("\n" + "=" * 100)
    print("1. HOW OFTEN EACH CLUB MAKES THE TOP 6 BRACKET")
    print("=" * 100)
    rows = []
    seasons_with_bracket = [s for s, t in tables.items() if (t.bracket != "-").any()]
    for season in sorted(seasons_with_bracket):
        t = tables[season]
        for _, r in t.iterrows():
            rows.append({"season": season, "team": r.team, "top6": r.bracket == "Top 6",
                         "pos": r.pos, "PPG": r.PPG})
    b = pd.DataFrame(rows)
    summary = b.groupby("team").agg(seasons=("top6", "size"), top6=("top6", "sum"),
                                    best=("pos", "min"), worst=("pos", "max"),
                                    ppg=("PPG", "mean")).sort_values(["top6", "ppg"], ascending=False)
    summary["ppg"] = summary.ppg.round(2)
    print(f"(bracket seasons: {', '.join(sorted(seasons_with_bracket))})")
    print(summary.to_string())

    # 2. Where does the gap fall? Average PPG by finishing position.
    print("\n" + "=" * 100)
    print("2. WHERE THE GAP ACTUALLY FALLS - average PPG by finishing position, and the step down")
    print("=" * 100)
    pos = b.groupby("pos")["PPG"].agg(["mean", "size"]).round(2)
    pos["step_from_above"] = (pos["mean"].shift(1) - pos["mean"]).round(2)
    print(pos.to_string())

    # 3. Head-to-head: the named big 5 vs the rest
    BIG5 = ["Old Georgians M1", "Surbiton M1", "Wimbledon M1",
            "Hampstead & Westminster M1", "Holcombe M1"]
    print("\n" + "=" * 100)
    print(f"3. THE CLAIMED BIG 5 ({', '.join(BIG5)}) vs THE REST")
    print("=" * 100)
    recent = lf[lf.season.isin(EVENT_SEASONS)]
    recent = recent.assign(is_big5=recent.team.isin(BIG5),
                           opp_big5=recent.opp_id.map(recent.groupby("team_id")["team"].first()).isin(BIG5))
    rows = []
    for (mine, theirs), g in recent.groupby(["is_big5", "opp_big5"]):
        rows.append({
            "team is": "big 5" if mine else "rest", "opponent is": "big 5" if theirs else "rest",
            "matches": len(g), "win%": f"{g.win.mean():.0%}", "PPG": round(g.pts.mean(), 2),
            "GF/g": round(g.gf.mean(), 2), "GA/g": round(g.ga.mean(), 2),
        })
    print(pd.DataFrame(rows).to_string(index=False))

    # 4. Oxted specifically: are they closing?
    print("\n" + "=" * 100)
    print("4. IS OXTED CLOSING THE GAP? Oxted's record vs the big 5, season by season")
    print("=" * 100)
    names = lf.groupby("team_id")["team"].first()
    ox = lf[lf.team == "Oxted M1"].copy()
    ox["opp"] = ox.opp_id.map(names)
    ox["vs_big5"] = ox.opp.isin(BIG5)
    rows = []
    for (season, vs), g in ox.groupby(["season", "vs_big5"]):
        rows.append({"season": season, "opponent": "big 5" if vs else "rest", "P": len(g),
                     "W-D-L": f"{g.win.sum()}-{(g.pts == 1).sum()}-{(g.pts == 0).sum()}",
                     "GF": g.gf.sum(), "GA": g.ga.sum(),
                     "avg margin": round((g.gf - g.ga).mean(), 2), "PPG": round(g.pts.mean(), 2)})
    print(pd.DataFrame(rows).to_string(index=False))

    # 5. What do top-6 teams do well?
    print("\n" + "=" * 100)
    print(f"5. WHAT TOP-6 TEAMS DO WELL ({' + '.join(EVENT_SEASONS)}, per team per game)")
    print("=" * 100)
    ev_sql = text(
        """
        SELECT e.match_id, e.team_id, e.event_type, e.minute
        FROM match_events e JOIN matches m ON m.id = e.match_id
        JOIN seasons s ON s.id = m.season_id JOIN competitions c ON c.id = m.competition_id
        WHERE m.status='R' AND s.name IN :seasons AND c.name LIKE '%%Premier Division%%'
          AND c.name NOT LIKE '%%Women%%' AND m.source_id NOT LIKE 'altius:%%'
        """
    ).bindparams(__import__("sqlalchemy").bindparam("seasons", expanding=True))
    with create_engine(DATABASE_URL).connect() as conn:
        ev = pd.read_sql(ev_sql, conn, params={"seasons": list(EVENT_SEASONS)})

    br = {}
    for season in EVENT_SEASONS:
        for _, r in tables[season].iterrows():
            br[(season, r.team_id)] = r.bracket
    recent2 = lf[lf.season.isin(EVENT_SEASONS)].copy()
    recent2["grp"] = [br.get((s, t), "-") for s, t in zip(recent2.season, recent2.team_id)]
    recent2 = recent2[recent2.grp != "-"]

    ev_g = ev[ev.event_type.isin(GOALS)]
    ev_c = ev[ev.event_type.isin(CARDS)]
    rows = []
    for grp, g in recent2.groupby("grp"):
        keys = set(zip(g.match_id, g.team_id))
        mine_g = ev_g[[(m, t) in keys for m, t in zip(ev_g.match_id, ev_g.team_id)]]
        mine_c = ev_c[[(m, t) in keys for m, t in zip(ev_c.match_id, ev_c.team_id)]]
        n = len(g)
        # conceded: goals in their matches by the other team
        opp_g = ev_g[ev_g.match_id.isin(g.match_id) & ~pd.Series(
            [(m, t) in keys for m, t in zip(ev_g.match_id, ev_g.team_id)], index=ev_g.index)]
        rows.append({
            "group": grp, "team-seasons": g.groupby(["season", "team_id"]).ngroups, "matches": n,
            "GF/g": round(g.gf.mean(), 2), "GA/g": round(g.ga.mean(), 2),
            "PPG": round(g.pts.mean(), 2), "win%": f"{g.win.mean():.0%}",
            "PC goals/g": round((mine_g.event_type == "PC").sum() / n, 2),
            "PC share": f"{(mine_g.event_type == 'PC').sum() / max(len(mine_g), 1):.0%}",
            "cards/g": round(len(mine_c) / n, 2),
            "clean sheet%": f"{(g.ga == 0).mean():.0%}",
            "scoreless%": f"{(g.gf == 0).mean():.0%}",
        })
    print(pd.DataFrame(rows).to_string(index=False))

    # Timing split by group
    print("\n  share of each group's goals (and goals conceded) by quarter:")
    rows = []
    for grp, g in recent2.groupby("grp"):
        keys = set(zip(g.match_id, g.team_id))
        mine = ev_g[[(m, t) in keys for m, t in zip(ev_g.match_id, ev_g.team_id)]]
        mine = mine[mine.minute.notna()]
        opp = ev_g[ev_g.match_id.isin(g.match_id)]
        is_opp = pd.Series([(m, t) not in keys for m, t in zip(opp.match_id, opp.team_id)], index=opp.index)
        opp = opp[is_opp & opp.minute.notna()]
        r = {"group": grp}
        for lo, hi, label in QUARTERS:
            r[f"{label} for"] = f"{((mine.minute >= lo) & (mine.minute <= hi)).sum() / max(len(mine), 1):.0%}"
            r[f"{label} against"] = f"{((opp.minute >= lo) & (opp.minute <= hi)).sum() / max(len(opp), 1):.0%}"
        rows.append(r)
    print(pd.DataFrame(rows).to_string(index=False))

    # 6. Does attack or defence predict position?
    print("\n" + "=" * 100)
    print("6. ATTACK OR DEFENCE? correlation of GF and GA with points, across every team-season")
    print("=" * 100)
    allt = pd.concat([t.assign(season=s) for s, t in tables.items()])
    allt = allt[allt.P >= 10]
    allt["GFpg"] = allt.GF / allt.P
    allt["GApg"] = allt.GA / allt.P
    print(f"  team-seasons: {len(allt)}")
    print(f"  corr(points, goals for)     = {allt.PTS.corr(allt.GFpg):+.2f}")
    print(f"  corr(points, goals against) = {allt.PTS.corr(allt.GApg):+.2f}")
    print(f"  corr(points, goal diff)     = {allt.PTS.corr(allt.GD / allt.P):+.2f}")
    print("\n  top-6 vs lower-6 by how far each sits from the league average that season:")
    z = allt[allt.bracket.isin(["Top 6", "Lower 6"])].copy()
    for col in ("GFpg", "GApg"):
        z[f"{col}_rel"] = z.groupby("season")[col].transform(lambda s: s - s.mean())
    print(z.groupby("bracket")[["GFpg_rel", "GApg_rel"]].mean().round(2).to_string())


if __name__ == "__main__":
    main()
