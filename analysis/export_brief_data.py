"""Export the datasets behind the social posts as flat CSVs.

Written for handing to a designer / another model that has no database access:
every file is self-contained, already filtered to played matches, and uses the
plain-English column names the brief refers to.

Run:  docker compose exec jupyter python3 /home/jovyan/work/export_brief_data.py
Output: /home/jovyan/work/brief_data/  (moved to content/brief/data/ by the caller)
"""
import os
import pandas as pd
from sqlalchemy import create_engine, text

OUT = "/home/jovyan/work/brief_data"
SEASON = "2025/26"
os.makedirs(OUT, exist_ok=True)
eng = create_engine("mysql+pymysql://hockey_stats:hockey_stats@db:3306/hockey_stats?charset=utf8mb4")

# Every Premier Division competition row, tagged with the division and whether it is
# part of the league phase (regular season + League Finals pools + Phase 2).
PREM = """
  c.name LIKE '%%Premier Division%%'
"""
LEAGUE_PHASE = """
  AND c.name NOT LIKE '%%Finals Weekend%%' AND c.name NOT LIKE '%%Relegation%%'
  AND c.name NOT LIKE '%%Play Offs%%'
"""
DIV = "IF(c.name LIKE 'Women%%','Women''s','Men''s/Open')"


def q(sql, **kw):
    with eng.connect() as conn:
        return pd.read_sql(text(sql), conn, params=kw)


def save(df, name):
    df.to_csv(f"{OUT}/{name}", index=False)
    print(f"{name:28s} {len(df):5d} rows")


# ---------------------------------------------------------------- matches
matches = q(f"""
    SELECT {DIV} division, c.name competition, DATE(m.starts_at) date,
           ht.name home_team, m.home_score, m.away_score, at.name away_team,
           m.home_shootout, m.away_shootout, v.name venue
    FROM matches m JOIN seasons s ON s.id=m.season_id JOIN competitions c ON c.id=m.competition_id
    JOIN teams ht ON ht.id=m.home_team_id JOIN teams at ON at.id=m.away_team_id
    LEFT JOIN venues v ON v.id=m.venue_id
    WHERE m.status='R' AND s.name=:s AND {PREM}
    ORDER BY m.starts_at""", s=SEASON)
save(matches, "matches_2025-26.csv")

# ------------------------------------------------------- season summary
summary = q(f"""
    SELECT {DIV} division, COUNT(*) matches, SUM(m.home_score+m.away_score) goals,
           ROUND(SUM(m.home_score+m.away_score)/COUNT(*),2) goals_per_game,
           SUM(m.home_score>m.away_score) home_wins, SUM(m.home_score=m.away_score) draws,
           SUM(m.home_score<m.away_score) away_wins
    FROM matches m JOIN seasons s ON s.id=m.season_id JOIN competitions c ON c.id=m.competition_id
    WHERE m.status='R' AND s.name=:s AND {PREM} {LEAGUE_PHASE}
    GROUP BY division""", s=SEASON)
cards_tot = q(f"""
    SELECT {DIV} division, COUNT(*) cards, SUM(e.card='green') green,
           SUM(e.card='yellow') yellow, SUM(e.card='red') red
    FROM match_events e JOIN matches m ON m.id=e.match_id JOIN seasons s ON s.id=m.season_id
    JOIN competitions c ON c.id=m.competition_id
    WHERE m.status='R' AND s.name=:s AND {PREM} {LEAGUE_PHASE} AND e.card IS NOT NULL
    GROUP BY division""", s=SEASON)
summary = summary.merge(cards_tot, on="division")
summary["cards_per_game"] = (summary.cards / summary.matches).round(2)
save(summary, "season_summary.csv")

# -------------------------------------------------------- team records
team = q(f"""
    SELECT {DIV} division, t.name team, cl.name club, COUNT(*) played,
           SUM(gf>ga) won, SUM(gf=ga) drawn, SUM(gf<ga) lost,
           SUM(gf) goals_for, SUM(ga) goals_against, SUM(gf)-SUM(ga) goal_difference,
           SUM(gf>ga)*3+SUM(gf=ga) points, SUM(ga=0) clean_sheets
    FROM (
      SELECT m.id, m.competition_id, m.season_id, m.home_team_id team_id, m.home_score gf, m.away_score ga FROM matches m WHERE m.status='R'
      UNION ALL
      SELECT m.id, m.competition_id, m.season_id, m.away_team_id, m.away_score, m.home_score FROM matches m WHERE m.status='R'
    ) x JOIN matches m ON m.id=x.id JOIN seasons s ON s.id=x.season_id
    JOIN competitions c ON c.id=x.competition_id JOIN teams t ON t.id=x.team_id JOIN clubs cl ON cl.id=t.club_id
    WHERE s.name=:s AND {PREM} {LEAGUE_PHASE}
    GROUP BY division, t.id, team, club ORDER BY division, points DESC""", s=SEASON)
save(team, "team_records_2025-26.csv")

# --------------------------------------------------- goals / cards by minute
by_min = q(f"""
    SELECT {DIV} division, e.minute,
           SUM(e.event_type='FG') field_goals, SUM(e.event_type='PC') corner_goals,
           SUM(e.event_type='PS') stroke_goals, SUM(e.card='green') green_cards,
           SUM(e.card='yellow') yellow_cards, SUM(e.card='red') red_cards
    FROM match_events e JOIN matches m ON m.id=e.match_id JOIN seasons s ON s.id=m.season_id
    JOIN competitions c ON c.id=m.competition_id
    WHERE m.status='R' AND s.name=:s AND {PREM} {LEAGUE_PHASE} AND e.minute BETWEEN 1 AND 70
    GROUP BY division, e.minute ORDER BY division, e.minute""", s=SEASON)
save(by_min, "events_by_minute_2025-26.csv")

# ------------------------------------------------------ per-team card shape
tc = q(f"""
    SELECT {DIV} division, t.name team, cl.name club, e.minute, e.card
    FROM match_events e JOIN matches m ON m.id=e.match_id JOIN seasons s ON s.id=m.season_id
    JOIN competitions c ON c.id=m.competition_id JOIN teams t ON t.id=e.team_id JOIN clubs cl ON cl.id=t.club_id
    WHERE m.status='R' AND s.name=:s AND {PREM} {LEAGUE_PHASE} AND e.card IS NOT NULL
      AND e.minute BETWEEN 1 AND 70""", s=SEASON)
QUARTERS = [("q1_cards", 1, 17), ("q2_cards", 18, 35), ("q3_cards", 36, 52), ("q4_cards", 53, 70)]
rows = []
for (div, tm, club), g in tc.groupby(["division", "team", "club"]):
    r = {"division": div, "team": tm, "club": club, "cards": len(g),
         "green": int((g.card == "green").sum()), "yellow": int((g.card == "yellow").sum()),
         "red": int((g.card == "red").sum()), "median_minute": float(g.minute.median()),
         "first_half_cards": int((g.minute <= 35).sum())}
    for name, a, b in QUARTERS:
        r[name] = int(g.minute.between(a, b).sum())
    rows.append(r)
team_cards = pd.DataFrame(rows).merge(
    team[["division", "team", "played"]], on=["division", "team"], how="left")
team_cards["cards_per_match"] = (team_cards.cards / team_cards.played).round(2)
save(team_cards.sort_values(["division", "cards_per_match"], ascending=[True, False]), "team_cards_2025-26.csv")

# ------------------------------------------------------------- players
scorers = q(f"""
    SELECT {DIV} division, CONCAT(p.first_name,' ',p.last_name) player, t.name team, cl.name club,
           COUNT(*) goals, SUM(e.event_type='FG') open_play,
           SUM(e.event_type='PC') corners, SUM(e.event_type='PS') strokes
    FROM match_events e JOIN matches m ON m.id=e.match_id JOIN seasons s ON s.id=m.season_id
    JOIN competitions c ON c.id=m.competition_id JOIN players p ON p.id=e.player_id
    JOIN teams t ON t.id=e.team_id JOIN clubs cl ON cl.id=t.club_id
    WHERE m.status='R' AND s.name=:s AND {PREM} {LEAGUE_PHASE} AND e.event_type IN ('FG','PC','PS')
    GROUP BY division, e.player_id, player, team, club
    HAVING goals >= 3 ORDER BY division, goals DESC""", s=SEASON)
save(scorers, "player_goals_2025-26.csv")

pcards = q(f"""
    SELECT {DIV} division, CONCAT(p.first_name,' ',p.last_name) player, t.name team,
           COUNT(*) cards, SUM(e.card='green') green, SUM(e.card='yellow') yellow, SUM(e.card='red') red
    FROM match_events e JOIN matches m ON m.id=e.match_id JOIN seasons s ON s.id=m.season_id
    JOIN competitions c ON c.id=m.competition_id JOIN players p ON p.id=e.player_id JOIN teams t ON t.id=e.team_id
    WHERE m.status='R' AND s.name=:s AND {PREM} {LEAGUE_PHASE} AND e.card IS NOT NULL
    GROUP BY division, e.player_id, player, team HAVING cards >= 3 ORDER BY division, cards DESC""", s=SEASON)
save(pcards, "player_cards_2025-26.csv")

# ------------------------------------------------------- finals weekend
finals = q(f"""
    SELECT {DIV} division, DATE(m.starts_at) date, ht.name home_team, m.home_score,
           m.away_score, at.name away_team, m.home_shootout, m.away_shootout
    FROM matches m JOIN seasons s ON s.id=m.season_id JOIN competitions c ON c.id=m.competition_id
    JOIN teams ht ON ht.id=m.home_team_id JOIN teams at ON at.id=m.away_team_id
    WHERE m.status='R' AND s.name=:s AND c.name LIKE '%%Finals Weekend%%' ORDER BY division, m.starts_at""", s=SEASON)
save(finals, "finals_weekend_2025-26.csv")

# ------------------------------------------- head to head: every meeting ever
h2h = q("""
    SELECT s.name season, c.name competition, DATE(m.starts_at) date,
           ht.name home_team, m.home_score, m.away_score, at.name away_team
    FROM matches m JOIN seasons s ON s.id=m.season_id JOIN competitions c ON c.id=m.competition_id
    JOIN teams ht ON ht.id=m.home_team_id JOIN teams at ON at.id=m.away_team_id
    WHERE m.status='R' AND ht.name IN ('Surbiton M1','Old Georgians M1')
      AND at.name IN ('Surbiton M1','Old Georgians M1') ORDER BY m.starts_at""")
save(h2h, "h2h_surbiton_v_old_georgians.csv")

# ------------------------------------------------------------- clubs
clubs = q("""
    SELECT DISTINCT cl.name club, cl.logo_url,
           LOWER(REGEXP_REPLACE(cl.name,'[^A-Za-z0-9]+','-')) badge_slug
    FROM clubs cl JOIN teams t ON t.club_id=cl.id
    JOIN matches m ON t.id IN (m.home_team_id, m.away_team_id)
    JOIN seasons s ON s.id=m.season_id JOIN competitions c ON c.id=m.competition_id
    WHERE s.name=:s AND c.name LIKE '%%Premier Division%%' ORDER BY club""", s=SEASON)
save(clubs, "clubs.csv")

print("\ndone ->", OUT)
