"""
Does a card actually cost goals, or is it just the penalty corner that came with it?

The worry: a big tackle in the circle gives away a penalty corner AND earns a
green. The corner gets converted. The goal then looks like "punished while a
man down" when really the card and the goal share a cause and the man-down
period did no work at all.

Two things separate those stories, and this script measures both:

  TYPE  If the effect is the corner, window goals should be overwhelmingly PC.
        If it's the extra man, the PC share inside the window should look like
        the PC share everywhere else.
  LAG   The corner that came with the card is taken within a minute of it.
        A genuine man-down effect is spread across the whole sin-bin window.

Lag 0-1 PC goals are the suspicious bucket. Everything else is the real effect.

Run: docker compose exec jupyter python3 /home/jovyan/work/card_impact.py
"""
from __future__ import annotations

import argparse

import pandas as pd
from sqlalchemy import create_engine, text

from predictor import DATABASE_URL

SEASONS = ("2024/25", "2025/26")
GOALS = ("FG", "PC", "PS")
# Sin-bin length on the 70-minute app clock. A yellow is "5 minutes or more" and
# the source never records which, so 6 is a floor - see CLAUDE.md.
WINDOWS = {"GC": 2, "YC": 6}


def load(engine) -> tuple[pd.DataFrame, pd.DataFrame]:
    sql = text(
        """
        SELECT e.match_id, e.team_id, e.event_type, e.minute,
               m.home_team_id, m.away_team_id, th.name AS home, ta.name AS away
        FROM match_events e
        JOIN matches m ON m.id = e.match_id
        JOIN seasons s ON s.id = m.season_id
        JOIN competitions c ON c.id = m.competition_id
        JOIN teams th ON th.id = m.home_team_id
        JOIN teams ta ON ta.id = m.away_team_id
        WHERE m.status = 'R' AND s.name IN :seasons
          AND m.source_id NOT LIKE 'altius:%%'
          AND e.minute IS NOT NULL AND e.minute BETWEEN 1 AND 70
        """
    ).bindparams(__import__("sqlalchemy").bindparam("seasons", expanding=True))
    with engine.connect() as conn:
        ev = pd.read_sql(sql, conn, params={"seasons": list(SEASONS)})
    ev["minute"] = ev["minute"].astype(int)
    goals = ev[ev.event_type.isin(GOALS)].copy()
    cards = ev[ev.event_type.isin(WINDOWS)].copy()
    return goals, cards


def window_goals(goals: pd.DataFrame, cards: pd.DataFrame) -> pd.DataFrame:
    """One row per (card, goal-in-its-window), with the lag and who scored."""
    g = goals.rename(columns={"team_id": "goal_team", "event_type": "goal_type",
                              "minute": "goal_minute"})[
        ["match_id", "goal_team", "goal_type", "goal_minute"]]
    c = cards.rename(columns={"team_id": "card_team", "event_type": "card_type",
                              "minute": "card_minute"})[
        ["match_id", "card_team", "card_type", "card_minute"]]
    j = c.merge(g, on="match_id", how="inner")
    j["window"] = j.card_type.map(WINDOWS)
    j["lag"] = j.goal_minute - j.card_minute
    j = j[(j.lag >= 0) & (j.lag <= j.window)].copy()
    # Who benefited: the team NOT carrying the card is "a man up".
    j["side"] = j.apply(lambda r: "man up" if r.goal_team != r.card_team else "man down", axis=1)
    return j


def pct(n: int, d: int) -> str:
    return f"{n / d:.0%}" if d else "-"


def report_block(title: str, w: pd.DataFrame, baseline: pd.DataFrame) -> str:
    out = [f"\n{'=' * 84}\n{title}\n{'=' * 84}"]
    base_pc = (baseline.event_type == "PC").mean() if len(baseline) else 0
    out.append(f"baseline PC share of all goals in this scope: {base_pc:.0%}  (n={len(baseline)})")

    for card_type in ("GC", "YC"):
        for side in ("man up", "man down"):
            sub = w[(w.card_type == card_type) & (w.side == side)]
            if not len(sub):
                continue
            pc = (sub.goal_type == "PC").sum()
            out.append(
                f"\n  {card_type} / team {side}: {len(sub)} goals   "
                f"PC {pc} ({pct(pc, len(sub))})   FG {(sub.goal_type == 'FG').sum()}   "
                f"PS {(sub.goal_type == 'PS').sum()}"
            )
            lag = sub.groupby("lag").agg(
                goals=("goal_type", "size"),
                pc=("goal_type", lambda s: (s == "PC").sum()),
            )
            out.append("      by minutes after the card:")
            for l, r in lag.iterrows():
                out.append(f"        +{int(l)} min: {int(r.goals)} goals ({int(r.pc)} PC)")
            early_pc = len(sub[(sub.lag <= 1) & (sub.goal_type == "PC")])
            out.append(
                f"      -> suspicious (PC within 1 min of the card): {early_pc} of {len(sub)} "
                f"= {pct(early_pc, len(sub))}; the other {len(sub) - early_pc} are not "
                "explained by the card's own corner"
            )
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--teams", nargs="*", default=["Oxted M1", "East Grinstead M1"])
    args = ap.parse_args()

    engine = create_engine(DATABASE_URL)
    goals, cards = load(engine)
    w = window_goals(goals, cards)

    print(f"Card-window goal decomposition, {' + '.join(SEASONS)}, league matches only")
    print(f"{len(goals)} goals, {len(cards)} green/yellow cards, "
          f"{len(w)} goal-in-window pairs")
    print(report_block("ALL NATIONAL LEAGUES", w, goals))

    # Per-team: the numbers that went into the scouting reports.
    names = pd.concat([
        goals[["home_team_id", "home"]].rename(columns={"home_team_id": "tid", "home": "name"}),
        goals[["away_team_id", "away"]].rename(columns={"away_team_id": "tid", "away": "name"}),
    ]).drop_duplicates()
    name_to_id = dict(zip(names.name, names.tid))
    for team in args.teams:
        tid = name_to_id.get(team)
        if tid is None:
            print(f"\n!! no team called {team!r} in this window")
            continue
        theirs = goals[(goals.home_team_id == tid) | (goals.away_team_id == tid)]
        # The two numbers the scouting reports actually quote:
        #   they SCORE while the opposition is a man down  (the "punisher" rate)
        #   they CONCEDE while they are a man down         (the "punished" rate)
        for label, sub in (
            (f"{team}: goals THEY SCORED while the opposition was carded",
             w[(w.card_team != tid) & (w.goal_team == tid)]),
            (f"{team}: goals THEY CONCEDED while a man down",
             w[(w.card_team == tid) & (w.goal_team != tid)]),
        ):
            print(report_block(label, sub, theirs))


if __name__ == "__main__":
    main()
