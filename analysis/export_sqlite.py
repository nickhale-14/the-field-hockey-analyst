"""Export the whole hockey database to a single portable SQLite file.

Intended for handing to someone (or some model) with no access to the Docker stack.
Copies every table as-is, adds the indexes you'd want, and adds a set of views that
bake in the gotchas documented in content/brief/BRIEF.md — so the easy query is also
the correct one.

Run:  docker compose exec jupyter python3 /home/jovyan/work/export_sqlite.py
Out:  /data/exports/hockey_stats.sqlite  (host: data/exports/)
"""
import os
import sqlite3
import pandas as pd
from sqlalchemy import create_engine, text

OUT = "/data/exports/hockey_stats.sqlite"  # host: data/exports/ (gitignored, 15MB)
SRC = create_engine("mysql+pymysql://hockey_stats:hockey_stats@db:3306/hockey_stats?charset=utf8mb4")
TABLES = ["seasons", "competitions", "clubs", "teams", "venues", "players",
          "matches", "match_events", "match_players", "match_officials"]

if os.path.exists(OUT):
    os.remove(OUT)
os.makedirs(os.path.dirname(OUT), exist_ok=True)
con = sqlite3.connect(OUT)

for t in TABLES:
    df = pd.read_sql(text(f"SELECT * FROM {t}"), SRC)
    for c in df.columns:                       # SQLite has no datetime type
        if str(df[c].dtype).startswith("datetime"):
            df[c] = df[c].astype(str).replace("NaT", None)
    df.to_sql(t, con, index=False)
    print(f"{t:18s} {len(df):6d} rows")

cur = con.cursor()
for stmt in [
    "CREATE INDEX ix_matches_season ON matches(season_id)",
    "CREATE INDEX ix_matches_comp ON matches(competition_id)",
    "CREATE INDEX ix_matches_home ON matches(home_team_id)",
    "CREATE INDEX ix_matches_away ON matches(away_team_id)",
    "CREATE INDEX ix_matches_status ON matches(status)",
    "CREATE INDEX ix_events_match ON match_events(match_id)",
    "CREATE INDEX ix_events_team ON match_events(team_id)",
    "CREATE INDEX ix_events_player ON match_events(player_id)",
    "CREATE INDEX ix_events_type ON match_events(event_type)",
    "CREATE INDEX ix_teams_club ON teams(club_id)",
    "CREATE INDEX ix_officials_match ON match_officials(match_id)",
]:
    cur.execute(stmt)

# ---- views -------------------------------------------------------------
# Division: every women's competition has "Women" in the name; the men's are
# "Men's ..." or "Open - Men's ...". Tier and phase are parsed the same way.
DIV = "CASE WHEN c.name LIKE '%Women%' THEN 'Women''s' ELSE 'Men''s/Open' END"
TIER = """CASE WHEN c.name LIKE '%Premier%' THEN 'Premier'
               WHEN c.name LIKE '%Division 1%' THEN 'Division 1'
               WHEN c.name LIKE '%Conference%' THEN 'Conference' ELSE 'Other' END"""
PHASE = """CASE WHEN c.name LIKE '%Finals Weekend%' THEN 'Finals Weekend'
                WHEN c.name LIKE '%Relegation%' OR c.name LIKE '%Play Offs%' THEN 'Relegation play-offs'
                ELSE 'League phase' END"""

cur.executescript(f"""
-- Every PLAYED match, with names resolved and the labels you actually want to group by.
CREATE VIEW v_matches AS
SELECT m.id AS match_id, s.name AS season, c.name AS competition,
       {DIV} AS division, {TIER} AS tier, {PHASE} AS phase,
       date(m.starts_at) AS date, m.starts_at,
       ht.name AS home_team, hc.name AS home_club, m.home_score,
       m.away_score, at.name AS away_team, ac.name AS away_club,
       m.home_shootout, m.away_shootout, v.name AS venue,
       CASE WHEN c.name LIKE '%Finals Weekend%' THEN 60 ELSE 70 END AS clock_max
FROM matches m
JOIN seasons s ON s.id = m.season_id
JOIN competitions c ON c.id = m.competition_id
JOIN teams ht ON ht.id = m.home_team_id JOIN clubs hc ON hc.id = ht.club_id
JOIN teams at ON at.id = m.away_team_id JOIN clubs ac ON ac.id = at.club_id
LEFT JOIN venues v ON v.id = m.venue_id
WHERE m.status = 'R';

-- One row per team per played match: the basis of any league table.
CREATE VIEW v_team_match AS
SELECT match_id, season, competition, division, tier, phase, date, home_team AS team,
       home_club AS club, away_team AS opponent, 1 AS is_home,
       home_score AS goals_for, away_score AS goals_against,
       CASE WHEN home_score > away_score THEN 3 WHEN home_score = away_score THEN 1 ELSE 0 END AS points
FROM v_matches
UNION ALL
SELECT match_id, season, competition, division, tier, phase, date, away_team, away_club,
       home_team, 0, away_score, home_score,
       CASE WHEN away_score > home_score THEN 3 WHEN away_score = home_score THEN 1 ELSE 0 END
FROM v_matches;

-- Goals. FG / PC / PS are ALL goals; goal_type spells that out.
CREATE VIEW v_goals AS
SELECT e.id AS event_id, vm.match_id, vm.season, vm.competition, vm.division, vm.tier,
       vm.phase, vm.date, vm.clock_max, t.name AS team, cl.name AS club,
       p.first_name || ' ' || p.last_name AS player, e.minute, e.event_type,
       CASE e.event_type WHEN 'FG' THEN 'Open play' WHEN 'PC' THEN 'Penalty corner'
                         WHEN 'PS' THEN 'Penalty stroke' END AS goal_type
FROM match_events e
JOIN v_matches vm ON vm.match_id = e.match_id
JOIN teams t ON t.id = e.team_id JOIN clubs cl ON cl.id = t.club_id
LEFT JOIN players p ON p.id = e.player_id
WHERE e.event_type IN ('FG','PC','PS');

-- Cards.
CREATE VIEW v_cards AS
SELECT e.id AS event_id, vm.match_id, vm.season, vm.competition, vm.division, vm.tier,
       vm.phase, vm.date, vm.clock_max, t.name AS team, cl.name AS club,
       p.first_name || ' ' || p.last_name AS player, e.minute, e.card
FROM match_events e
JOIN v_matches vm ON vm.match_id = e.match_id
JOIN teams t ON t.id = e.team_id JOIN clubs cl ON cl.id = t.club_id
LEFT JOIN players p ON p.id = e.player_id
WHERE e.card IS NOT NULL;

-- Scheduled fixtures that have not been played yet (the live 2026/27 season).
CREATE VIEW v_fixtures AS
SELECT m.id AS match_id, s.name AS season, c.name AS competition, {DIV} AS division,
       {TIER} AS tier, m.starts_at, ht.name AS home_team, at.name AS away_team, v.name AS venue
FROM matches m
JOIN seasons s ON s.id = m.season_id JOIN competitions c ON c.id = m.competition_id
JOIN teams ht ON ht.id = m.home_team_id JOIN teams at ON at.id = m.away_team_id
LEFT JOIN venues v ON v.id = m.venue_id
WHERE m.status = 'S';
""")

# ---- notes table: the gotchas travel with the file ----------------------
notes = pd.DataFrame([
    ("start_here", "Use the v_* views. They filter to played matches and resolve names, divisions and phases for you."),
    ("goals", "event_type FG, PC and PS are ALL goals (open play / penalty corner / penalty stroke). PC does NOT mean a corner was awarded — we have no data on corners that did not score."),
    ("cards", "match_events.card is green, yellow or red. GC/YC/RC in the raw event_type column mean the same thing."),
    ("played_only", "matches.status: R=result (played), S=scheduled, A=abandoned, P=postponed, W=walkover. Filter to 'R' for anything historical — the views already do."),
    ("two_clocks", "League minutes run 1-70 (club app running clock, no stopped time). Finals Weekend minutes run 1-60 (different source). Never mix them: see clock_max on every view. Quarters on the 70 scale are 1-17, 18-35, 36-52, 53-70."),
    ("minute_36", "Minute 36 is nearly empty in league timing charts: the app clock restarts at half-time. Real, but an artefact."),
    ("team_names", "About 18 clubs use the same team name for their men's and women's sides. Always group by division as well as team, or join on teams.id."),
    ("player_club", "players.club_id is wherever the person first appeared and does NOT follow transfers. Attribute players via the event's team (v_goals.team / v_cards.team), never players.club_id."),
    ("coverage", "2022/23 has scores but NO events at all. 2023/24 event coverage is patchy. Use 2024/25 onward for anything about goal times, scorers or cards. 2026/27 is in progress."),
    ("competition_names", "Sponsor prefixes and spelling drift between seasons ('Vitality Women's Premier Division', 'Open - Men's Premier Division', 'Womens Premier Division'). Use the division / tier / phase columns on the views instead of matching names."),
    ("squads", "match_players is each team's matchday squad (~16), 2023/24 onward, league API matches only (no Finals Weekend). Rolling subs, so the squad is who played; there is no starting XI or minutes. player_id is NULL for a player who has not consented to be named."),
    ("shootouts", "matches.home_shootout / away_shootout hold shootout scores; the match score stays the drawn score so goal totals stay correct."),
    ("scope", "England Hockey national leagues only: Premier, Division 1 North/South, Conference North/West/East/Midlands, plus play-offs and Finals Weekend. Seasons 2019/20-2021/22 are Premier scores only (no events)."),
], columns=["topic", "note"])
notes.to_sql("_readme", con, index=False)

con.commit()
cur.execute("VACUUM")
con.close()
print(f"\n{OUT}  {os.path.getsize(OUT)/1e6:.1f} MB")
