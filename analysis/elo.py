"""
Elo ratings for England Hockey Men's national leagues (Premier -> Conference),
carried continuously across all ingested seasons.

- Expected score: standard logistic Elo formula.
- Update: standard Elo update, scaled by a goal-margin multiplier (bigger wins
  move ratings more than narrow ones). A draw always uses multiplier 1.
- No home-advantage adjustment baked in here on purpose - that's a separate
  analysis to run on top of these ratings, not something to fold into the
  rating model itself.
- Starting rating is anchored to the tier a team is first seen in, not a flat
  1500 for everyone. Division/tier placement is itself strong evidence of
  quality (that's the entire point of promotion/relegation) - treating a
  Conference team as equally likely to be elite as a Premier team ignores
  information we already have, and lets a team that's merely dominant within
  a weakly-connected lower tier out-rate genuine Premier teams it never faced.
"""
import math
import os

from sqlalchemy import create_engine, text

DATABASE_URL = os.getenv(
    "DATABASE_URL", "mysql+pymysql://hockey_stats:hockey_stats@db:3306/hockey_stats?charset=utf8mb4"
)
K = 20

TIER_START_RATING = {
    "Premier": 1600.0,
    "Division 1": 1450.0,
    "Conference": 1300.0,
}
DEFAULT_START_RATING = 1500.0


def tier_of(competition_name: str) -> str | None:
    if "Premier" in competition_name:
        return "Premier"
    if "Division 1" in competition_name:
        return "Division 1"
    if "Conference" in competition_name:
        return "Conference"
    return None


def expected_score(rating_a: float, rating_b: float) -> float:
    return 1 / (1 + 10 ** ((rating_b - rating_a) / 400))


def margin_multiplier(goal_diff: int) -> float:
    if goal_diff == 0:
        return 1.0
    return math.log(1 + goal_diff) / math.log(2)


def main() -> None:
    engine = create_engine(DATABASE_URL)
    query = text(
        """
        SELECT m.starts_at, m.home_team_id, m.away_team_id, m.home_score, m.away_score,
               ht.name as home_name, at.name as away_name, c.name as competition_name
        FROM matches m
        JOIN teams ht ON ht.id = m.home_team_id
        JOIN teams at ON at.id = m.away_team_id
        JOIN competitions c ON c.id = m.competition_id
        WHERE m.status = 'R'
          AND c.name LIKE '%Men%'
          AND c.name NOT LIKE '%Women%'
          AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
        ORDER BY m.starts_at
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(query).fetchall()

    ratings: dict[int, float] = {}
    names: dict[int, str] = {}
    games_played: dict[int, int] = {}
    first_tier: dict[int, str] = {}

    for row in rows:
        home_id, away_id = row.home_team_id, row.away_team_id
        names[home_id] = row.home_name
        names[away_id] = row.away_name

        tier = tier_of(row.competition_name)
        if home_id not in ratings:
            start = TIER_START_RATING.get(tier, DEFAULT_START_RATING)
            ratings[home_id] = start
            first_tier[home_id] = tier or "Unknown"
        if away_id not in ratings:
            start = TIER_START_RATING.get(tier, DEFAULT_START_RATING)
            ratings[away_id] = start
            first_tier[away_id] = tier or "Unknown"

        r_home = ratings[home_id]
        r_away = ratings[away_id]

        e_home = expected_score(r_home, r_away)
        e_away = 1 - e_home

        if row.home_score > row.away_score:
            s_home, s_away = 1.0, 0.0
        elif row.home_score < row.away_score:
            s_home, s_away = 0.0, 1.0
        else:
            s_home, s_away = 0.5, 0.5

        mult = margin_multiplier(abs(row.home_score - row.away_score))

        ratings[home_id] = r_home + K * mult * (s_home - e_home)
        ratings[away_id] = r_away + K * mult * (s_away - e_away)
        games_played[home_id] = games_played.get(home_id, 0) + 1
        games_played[away_id] = games_played.get(away_id, 0) + 1

    print(f"Processed {len(rows)} matches, {len(ratings)} teams\n")

    final = sorted(ratings.items(), key=lambda x: -x[1])
    print(f"{'Rank':<5}{'Team':<40}{'Rating':>8}{'Games':>8}  {'First seen in'}")
    for i, (team_id, rating) in enumerate(final, 1):
        print(f"{i:<5}{names[team_id]:<40}{rating:>8.1f}{games_played[team_id]:>8}  {first_tier[team_id]}")


if __name__ == "__main__":
    main()
