"""
Opposition scouting report for one team, from everything in the DB.

    docker compose exec jupyter python3 /home/jovyan/work/scout.py \
        --team "Oxted M1" --opponent "Reading M1"

Sections: season-by-season record, form, home/away, goal types for and
against, goal timing by quarter, the first-goal effect, discipline, who
scores and who gets carded, scorer involvement in wins vs losses, and the
head-to-head with --opponent.

Data caveats this script enforces rather than leaves to the reader:
- 2022/23 has NO events and 2023/24 is partial, so every event-based
  section is restricted to EVENT_SEASONS and says so. Results-based
  sections use every season.
- League events run on a 70-minute app clock, Altius finals on a 60-minute
  one, so timing excludes Altius matches (`altius:` source ids).
- Teams are addressed by id, never name: ~18 names are shared by a club's
  men's and women's sides.
"""
from __future__ import annotations

import argparse

import pandas as pd
from sqlalchemy import bindparam, create_engine, text

from predictor import DATABASE_URL

EVENT_SEASONS = ("2024/25", "2025/26", "2026/27")
GOAL_TYPES = ("FG", "PC", "PS")
CARD_TYPES = ("GC", "YC", "RC")
# The five clubs that have filled the men's Prem top five in every season on
# record (see prem_tiers.py). They beat everyone else 79 times in 80, so any
# team's raw averages are mostly a function of how often they played them -
# every event section is therefore also reported split by opponent tier.
BIG5 = ("Old Georgians M1", "Surbiton M1", "Wimbledon M1",
        "Hampstead & Westminster M1", "Holcombe M1")
# 70-minute app clock, quartered.
QUARTERS = [(1, 17, "Q1 1-17"), (18, 35, "Q2 18-35"), (36, 52, "Q3 36-52"), (53, 70, "Q4 53-70")]


def find_team(engine, name: str) -> tuple[int, str]:
    sql = text(
        """
        SELECT t.id, t.name, COUNT(*) AS played
        FROM teams t JOIN matches m ON t.id IN (m.home_team_id, m.away_team_id)
        JOIN competitions c ON c.id = m.competition_id
        WHERE t.name = :name AND m.status = 'R' AND c.name NOT LIKE '%%Women%%'
        GROUP BY t.id, t.name ORDER BY played DESC
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(sql, {"name": name}).fetchall()
    if not rows:
        raise SystemExit(f"No men's team called {name!r} with played matches")
    return int(rows[0][0]), rows[0][1]


def load_matches(engine, team_id: int) -> pd.DataFrame:
    sql = text(
        """
        SELECT m.id AS match_id, m.source_id, s.name AS season, c.name AS competition,
               m.starts_at, m.home_team_id, m.away_team_id,
               ht.name AS home, at.name AS away, m.home_score, m.away_score
        FROM matches m
        JOIN seasons s ON s.id = m.season_id
        JOIN competitions c ON c.id = m.competition_id
        JOIN teams ht ON ht.id = m.home_team_id
        JOIN teams at ON at.id = m.away_team_id
        WHERE m.status = 'R' AND :tid IN (m.home_team_id, m.away_team_id)
        ORDER BY m.starts_at
        """
    )
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn, params={"tid": team_id})
    df["date"] = pd.to_datetime(df["starts_at"])
    df["at_home"] = df["home_team_id"] == team_id
    df["opponent"] = df["away"].where(df["at_home"], df["home"])
    df["gf"] = df["home_score"].where(df["at_home"], df["away_score"])
    df["ga"] = df["away_score"].where(df["at_home"], df["home_score"])
    df["result"] = df.apply(lambda r: "W" if r.gf > r.ga else ("D" if r.gf == r.ga else "L"), axis=1)
    df["venue"] = df["at_home"].map({True: "H", False: "A"})
    return df


def load_events(engine, match_ids: list[int]) -> pd.DataFrame:
    if not match_ids:
        return pd.DataFrame()
    sql = text(
        """
        SELECT e.match_id, e.team_id, e.event_type, e.minute,
               TRIM(CONCAT(COALESCE(p.first_name, ''), ' ', COALESCE(p.last_name, ''))) AS player
        FROM match_events e LEFT JOIN players p ON p.id = e.player_id
        WHERE e.match_id IN :ids
        """
    ).bindparams(bindparam("ids", expanding=True))
    with engine.connect() as conn:
        return pd.read_sql(sql, conn, params={"ids": match_ids})


def pct(n: int, d: int) -> str:
    return f"{n / d:.0%}" if d else "-"


def record(df: pd.DataFrame) -> str:
    w, d, l = (df["result"] == "W").sum(), (df["result"] == "D").sum(), (df["result"] == "L").sum()
    return f"{w}-{d}-{l}"


def section_record(m: pd.DataFrame, name: str) -> None:
    print(f"\n{'=' * 78}\n{name}: season by season (all competitions, {len(m)} matches)\n{'=' * 78}")
    rows = []
    for season, g in m.groupby("season"):
        rows.append({
            "season": season, "P": len(g), "W-D-L": record(g),
            "GF": g.gf.sum(), "GA": g.ga.sum(), "GD": g.gf.sum() - g.ga.sum(),
            "GF/g": round(g.gf.mean(), 2), "GA/g": round(g.ga.mean(), 2),
            "win%": pct((g.result == "W").sum(), len(g)),
        })
    print(pd.DataFrame(rows).to_string(index=False))

    print(f"\nHome vs away (last 2 seasons):")
    recent = m[m.season.isin(EVENT_SEASONS)]
    rows = []
    for venue, g in recent.groupby("venue"):
        rows.append({"venue": venue, "P": len(g), "W-D-L": record(g),
                     "GF/g": round(g.gf.mean(), 2), "GA/g": round(g.ga.mean(), 2),
                     "win%": pct((g.result == "W").sum(), len(g))})
    print(pd.DataFrame(rows).to_string(index=False))

    print("\nLast 10:")
    last = m.tail(10)[["date", "competition", "venue", "opponent", "gf", "ga", "result"]].copy()
    last["date"] = last["date"].dt.strftime("%Y-%m-%d")
    last["competition"] = last["competition"].str.replace("Open - Men's ", "", regex=False).str.replace("Men's ", "", regex=False)
    print(last.to_string(index=False))

    print(f"\nScoreline shape (last 2 seasons, {len(recent)} matches):")
    print(f"  clean sheets {(recent.ga == 0).sum()}  |  failed to score {(recent.gf == 0).sum()}  |  "
          f"blown out (lost by 3+) {((recent.ga - recent.gf) >= 3).sum()}  |  "
          f"tight (1 goal either way) {((recent.gf - recent.ga).abs() <= 1).sum()}")


def section_goal_types(m: pd.DataFrame, ev: pd.DataFrame, team_id: int, name: str) -> None:
    print(f"\n{'=' * 78}\n{name}: goal types, {' + '.join(EVENT_SEASONS[:2])} only (event data)\n{'=' * 78}")
    goals = ev[ev.event_type.isin(GOAL_TYPES)]
    for label, mask in (("scored", goals.team_id == team_id), ("conceded", goals.team_id != team_id)):
        g = goals[mask]
        tot = len(g)
        if not tot:
            continue
        by = g.event_type.value_counts()
        parts = ", ".join(f"{t} {by.get(t, 0)} ({pct(by.get(t, 0), tot)})" for t in GOAL_TYPES)
        print(f"  {label:9} {tot:3d} goals: {parts}")
    print("\n  (FG = field goal, PC = penalty-corner goal, PS = penalty stroke. Men's Prem baseline PC share"
          " is 34.8% of goals, 2024/25+2025/26.)")


def section_timing(m: pd.DataFrame, ev: pd.DataFrame, team_id: int, name: str) -> None:
    print(f"\n{'=' * 78}\n{name}: goal timing by quarter (70-min league clock; Altius finals excluded)\n{'=' * 78}")
    goals = ev[ev.event_type.isin(GOAL_TYPES) & ev.minute.notna()].copy()
    if goals.empty:
        print("  no timed goals")
        return
    goals["minute"] = goals["minute"].astype(int)
    rows = []
    for lo, hi, label in QUARTERS:
        q = goals[(goals.minute >= lo) & (goals.minute <= hi)]
        sc = (q.team_id == team_id).sum()
        co = (q.team_id != team_id).sum()
        rows.append({"quarter": label, "scored": sc, "conceded": co, "diff": sc - co})
    t = pd.DataFrame(rows)
    t.loc[len(t)] = {"quarter": "TOTAL", "scored": t.scored.sum(), "conceded": t.conceded.sum(),
                     "diff": t.scored.sum() - t.conceded.sum()}
    print(t.to_string(index=False))

    # Opening and closing 10 minutes - the bits that decide games
    for lo, hi, label in ((1, 10, "first 10 min"), (61, 70, "last 10 min")):
        q = goals[(goals.minute >= lo) & (goals.minute <= hi)]
        print(f"  {label}: scored {(q.team_id == team_id).sum()}, conceded {(q.team_id != team_id).sum()}")


def section_first_goal(m: pd.DataFrame, ev: pd.DataFrame, team_id: int, name: str) -> None:
    print(f"\n{'=' * 78}\n{name}: the first-goal effect\n{'=' * 78}")
    goals = ev[ev.event_type.isin(GOAL_TYPES) & ev.minute.notna()].copy()
    if goals.empty:
        print("  no timed goals")
        return
    goals["minute"] = goals["minute"].astype(int)
    first = goals.sort_values("minute").groupby("match_id").first()
    joined = m.set_index("match_id").join(first[["team_id", "minute"]], how="inner")
    joined = joined[joined.gf + joined.ga > 0]
    for label, mask in (("scored first", joined.team_id == team_id), ("conceded first", joined.team_id != team_id)):
        g = joined[mask]
        if len(g):
            print(f"  {label:14} {len(g):2d} matches: {record(g)}  "
                  f"({pct((g.result == 'W').sum(), len(g))} wins, avg first goal min {g.minute.mean():.0f})")
    # Did they come back?
    behind = joined[(joined.team_id != team_id)]
    if len(behind):
        print(f"  after conceding first they still won {(behind.result == 'W').sum()} and drew {(behind.result == 'D').sum()}")


def section_cards(m: pd.DataFrame, ev: pd.DataFrame, team_id: int, name: str) -> None:
    print(f"\n{'=' * 78}\n{name}: discipline\n{'=' * 78}")
    cards = ev[ev.event_type.isin(CARD_TYPES)]
    theirs = cards[cards.team_id == team_id]
    opp = cards[cards.team_id != team_id]
    n = m[m.season.isin(EVENT_SEASONS)].shape[0]
    by = theirs.event_type.value_counts()
    print(f"  taken   {len(theirs):3d} cards in {n} matches = {len(theirs) / n:.2f}/game  "
          f"(green {by.get('GC', 0)}, yellow {by.get('YC', 0)}, red {by.get('RC', 0)})")
    byo = opp.event_type.value_counts()
    print(f"  drawn   {len(opp):3d} cards from opponents = {len(opp) / n:.2f}/game  "
          f"(green {byo.get('GC', 0)}, yellow {byo.get('YC', 0)}, red {byo.get('RC', 0)})")
    print("  (Men's Prem baseline is 1.19 cards per team per game. A green = 2 min off, yellow = 5+.)")

    timed = theirs[theirs.minute.notna()].copy()
    if len(timed):
        timed["minute"] = timed["minute"].astype(int)
        rows = []
        for lo, hi, label in QUARTERS:
            rows.append({"quarter": label, "cards": ((timed.minute >= lo) & (timed.minute <= hi)).sum()})
        print("\n  when they get carded:")
        print(pd.DataFrame(rows).to_string(index=False))

    named = theirs[theirs.player.notna() & (theirs.player != "")]
    if len(named):
        top = named.player.value_counts().head(6)
        print("\n  most carded:")
        for p, c in top.items():
            kinds = named[named.player == p].event_type.value_counts().to_dict()
            print(f"    {p:28} {c:2d}  {kinds}")

    # Cards in wins vs losses
    per = theirs.groupby("match_id").size().rename("cards")
    j = m.set_index("match_id").join(per).fillna({"cards": 0})
    j = j[j.season.isin(EVENT_SEASONS)]
    print("\n  cards per game by result:", ", ".join(
        f"{r} {g.cards.mean():.2f}" for r, g in j.groupby("result")))


def section_players(m: pd.DataFrame, ev: pd.DataFrame, team_id: int, name: str) -> None:
    print(f"\n{'=' * 78}\n{name}: who scores\n{'=' * 78}")
    goals = ev[ev.event_type.isin(GOAL_TYPES) & (ev.team_id == team_id)]
    named = goals[goals.player.notna() & (goals.player != "")]
    if named.empty:
        print("  no named scorers")
        return
    tbl = named.groupby("player").agg(
        goals=("event_type", "size"),
        FG=("event_type", lambda s: (s == "FG").sum()),
        PC=("event_type", lambda s: (s == "PC").sum()),
        PS=("event_type", lambda s: (s == "PS").sum()),
    ).sort_values("goals", ascending=False).head(10)
    tbl["share"] = (tbl.goals / len(goals)).map(lambda x: f"{x:.0%}")
    print(tbl.to_string())
    top = tbl.index[0]
    print(f"\n  reliance: top scorer has {tbl.goals.iloc[0]} of {len(goals)} goals ({tbl['share'].iloc[0]}); "
          f"top 3 have {tbl.goals.head(3).sum()} ({tbl.goals.head(3).sum() / len(goals):.0%})")

    # Did the team win when the top scorers scored?
    print("\n  team record in matches where each scored:")
    res = m.set_index("match_id")["result"]
    for p in tbl.index[:5]:
        mids = named[named.player == p].match_id.unique()
        g = res.reindex(mids).dropna()
        if len(g):
            w, d, l = (g == "W").sum(), (g == "D").sum(), (g == "L").sum()
            print(f"    {p:28} {len(g):2d} matches  {w}-{d}-{l}  ({pct(w, len(g))} wins)")

    # Spread: how many different players score
    print(f"\n  {named.player.nunique()} different scorers; "
          f"{(named.player.value_counts() == 1).sum()} scored just once")


def section_opponent_split(m: pd.DataFrame, ev: pd.DataFrame, team_id: int, name: str) -> None:
    """
    The same stats split by whether the opponent was one of the big five.

    Without this a mid-table side's averages are mostly a record of how often
    they were beaten by the top five, not of how they play the teams they
    actually compete with - and the two pictures can be opposite.
    """
    print(f"\n{'=' * 78}\n{name}: vs the big five vs everyone else ({' + '.join(EVENT_SEASONS[:2])})\n{'=' * 78}")
    if not m.opponent.isin(BIG5).any():
        print("  never played a big-five side in this window")
        return
    rows = []
    for label, g in (("vs big 5", m[m.opponent.isin(BIG5)]), ("vs peers", m[~m.opponent.isin(BIG5)])):
        if not len(g):
            continue
        e = ev[ev.match_id.isin(g.match_id)]
        goals = e[e.event_type.isin(GOAL_TYPES)]
        sc, co = goals[goals.team_id == team_id], goals[goals.team_id != team_id]
        cards = e[e.event_type.isin(CARD_TYPES) & (e.team_id == team_id)]
        q1 = sc[sc.minute.notna()], co[co.minute.notna()]
        rows.append({
            "split": label, "P": len(g), "W-D-L": record(g),
            "GF/g": round(g.gf.mean(), 2), "GA/g": round(g.ga.mean(), 2),
            "PC% scored": pct((sc.event_type == "PC").sum(), len(sc)),
            "PC% conceded": pct((co.event_type == "PC").sum(), len(co)),
            "cards/g": round(len(cards) / len(g), 2),
            "Q1 for-against": f"{((q1[0].minute >= 1) & (q1[0].minute <= 17)).sum()}-"
                             f"{((q1[1].minute >= 1) & (q1[1].minute <= 17)).sum()}",
            "Q4 for-against": f"{((q1[0].minute >= 53) & (q1[0].minute <= 70)).sum()}-"
                              f"{((q1[1].minute >= 53) & (q1[1].minute <= 70)).sum()}",
        })
    print(pd.DataFrame(rows).to_string(index=False))
    print("\n  Read every other section against this. A team that played the big five often has averages\n"
          "  describing those maulings, not the games they contest.")


def section_h2h(m: pd.DataFrame, ev: pd.DataFrame, team_id: int, name: str, opponent: str) -> None:
    h = m[m.opponent == opponent]
    print(f"\n{'=' * 78}\n{name} vs {opponent}: head to head ({len(h)} matches in the DB)\n{'=' * 78}")
    if h.empty:
        print("  never met in the data")
        return
    show = h[["date", "season", "competition", "venue", "gf", "ga", "result"]].copy()
    show["date"] = show["date"].dt.strftime("%Y-%m-%d")
    show["competition"] = show["competition"].str.replace("Open - Men's ", "", regex=False).str.replace("Men's ", "", regex=False)
    show = show.rename(columns={"gf": f"{name[:12]}", "ga": opponent[:12]})
    print(show.to_string(index=False))
    print(f"\n  {name} record: {record(h)}; goals {h.gf.sum()}-{h.ga.sum()}")
    hg = ev[ev.match_id.isin(h.match_id) & ev.event_type.isin(GOAL_TYPES)]
    named = hg[(hg.team_id == team_id) & hg.player.notna() & (hg.player != "")]
    if len(named):
        print("  their scorers in this fixture:", ", ".join(
            f"{p} {c}" for p, c in named.player.value_counts().head(6).items()))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--team", required=True)
    ap.add_argument("--opponent")
    args = ap.parse_args()

    engine = create_engine(DATABASE_URL)
    team_id, name = find_team(engine, args.team)
    m = load_matches(engine, team_id)
    ev_all = load_events(engine, m.match_id.tolist())

    # Event-based sections: clean seasons only, and no Altius (60-min clock).
    clean_ids = m[m.season.isin(EVENT_SEASONS) & ~m.source_id.str.startswith("altius:")].match_id
    ev = ev_all[ev_all.match_id.isin(clean_ids)]

    pd.set_option("display.width", 200)
    print(f"\n### SCOUTING REPORT: {name} (team id {team_id}) ###")
    print(f"Results from {len(m)} matches, {m.season.min()}-{m.season.max()}. "
          f"Event detail from {len(clean_ids)} matches in {', '.join(EVENT_SEASONS[:2])}.")

    section_record(m, name)
    section_opponent_split(m[m.match_id.isin(clean_ids)], ev, team_id, name)
    section_goal_types(m, ev, team_id, name)
    section_timing(m, ev, team_id, name)
    section_first_goal(m, ev, team_id, name)
    section_cards(m, ev, team_id, name)
    section_players(m, ev, team_id, name)
    if args.opponent:
        section_h2h(m, ev_all, team_id, name, args.opponent)


if __name__ == "__main__":
    main()
