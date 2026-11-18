"""
Systematic integrity checks over everything already in the database.

Every check returns (name, severity, count, sample rows). Severity:
  ERROR - a real defect: the data contradicts itself or the source.
  WARN  - suspicious, usually explainable; look before acting.
  INFO  - known/accepted limitation, reported so it stays visible.

Nothing here writes. `ingest audit` prints the report; `ingest backfill-events`
is the tool that fixes the one class of problem we can fix automatically.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

# A complete Prem season 2022/23-2025/26 is 118 matches per gender (66 regular
# + 12 League Finals Pool + 30 Phase 2 + 6 relegation play-off + 4 finals).
EXPECTED_PREM_MATCHES = 118
LEAGUE_MAX_MINUTE = 70   # club scoring app's running clock
ALTIUS_MAX_MINUTE = 60   # technical table, stopped clock


@dataclass
class Check:
    name: str
    severity: str
    count: int
    sample: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""


def _rows(session: Session, sql: str, **params) -> list[dict[str, Any]]:
    return [dict(r) for r in session.execute(text(sql), params).mappings().all()]


def _check(session: Session, name: str, severity: str, sql: str, note: str = "", **params) -> Check:
    rows = _rows(session, sql, **params)
    return Check(name, severity, len(rows), rows[:8], note)


def run_all(session: Session) -> list[Check]:
    checks: list[Check] = []

    # --- scores vs events -------------------------------------------------
    checks.append(_check(
        session, "Played matches whose goal events don't equal the scoreline", "ERROR",
        """
        SELECT s.name AS season, c.name AS competition, th.name AS home, ta.name AS away,
               CONCAT(m.home_score,'-',m.away_score) AS score,
               (SELECT COUNT(*) FROM match_events e
                 WHERE e.match_id=m.id AND e.event_type IN ('FG','PC','PS')) AS goal_events
        FROM matches m
        JOIN seasons s ON s.id=m.season_id JOIN competitions c ON c.id=m.competition_id
        JOIN teams th ON th.id=m.home_team_id JOIN teams ta ON ta.id=m.away_team_id
        WHERE m.status='R'
          AND (SELECT COUNT(*) FROM match_events e
                WHERE e.match_id=m.id AND e.event_type IN ('FG','PC','PS')) <> m.home_score+m.away_score
        ORDER BY s.name DESC, m.starts_at
        """,
        note="Run `ingest backfill-events --season <name>` - the club may have logged them since we ingested.",
    ))

    checks.append(_check(
        session, "Goal events credited to a team not playing in that match", "ERROR",
        """
        SELECT s.name AS season, th.name AS home, ta.name AS away, t.name AS event_team, e.event_type, e.minute
        FROM match_events e
        JOIN matches m ON m.id=e.match_id JOIN seasons s ON s.id=m.season_id
        JOIN teams t ON t.id=e.team_id
        JOIN teams th ON th.id=m.home_team_id JOIN teams ta ON ta.id=m.away_team_id
        WHERE e.team_id NOT IN (m.home_team_id, m.away_team_id)
        """,
    ))

    checks.append(_check(
        session, "Played matches with no score recorded", "ERROR",
        """
        SELECT s.name AS season, c.name AS competition, th.name AS home, ta.name AS away, m.starts_at
        FROM matches m JOIN seasons s ON s.id=m.season_id JOIN competitions c ON c.id=m.competition_id
        JOIN teams th ON th.id=m.home_team_id JOIN teams ta ON ta.id=m.away_team_id
        WHERE m.status='R' AND (m.home_score IS NULL OR m.away_score IS NULL)
        """,
    ))

    # --- events on matches that were never played -------------------------
    checks.append(_check(
        session, "Events attached to a match that isn't a result (status <> 'R')", "WARN",
        """
        SELECT s.name AS season, m.status, th.name AS home, ta.name AS away, COUNT(*) AS events
        FROM match_events e JOIN matches m ON m.id=e.match_id
        JOIN seasons s ON s.id=m.season_id
        JOIN teams th ON th.id=m.home_team_id JOIN teams ta ON ta.id=m.away_team_id
        WHERE m.status <> 'R'
        GROUP BY s.name, m.status, th.name, ta.name, m.id
        """,
        note="Usually the abandoned-fixture/replay mix-up; `ingest repair` moves those across.",
    ))

    # --- clocks -----------------------------------------------------------
    checks.append(_check(
        session, "Event minutes outside the match clock", "ERROR",
        f"""
        SELECT s.name AS season, th.name AS home, ta.name AS away, e.event_type, e.minute,
               m.source_id LIKE 'altius:%%' AS altius
        FROM match_events e JOIN matches m ON m.id=e.match_id JOIN seasons s ON s.id=m.season_id
        JOIN teams th ON th.id=m.home_team_id JOIN teams ta ON ta.id=m.away_team_id
        WHERE e.minute IS NOT NULL
          AND (e.minute < 1
               OR (m.source_id LIKE 'altius:%%' AND e.minute > {ALTIUS_MAX_MINUTE})
               OR (m.source_id NOT LIKE 'altius:%%' AND e.minute > {LEAGUE_MAX_MINUTE}))
        """,
    ))

    checks.append(_check(
        session, "Events with no minute recorded", "INFO",
        """
        SELECT s.name AS season, e.event_type, COUNT(*) AS n
        FROM match_events e JOIN matches m ON m.id=e.match_id JOIN seasons s ON s.id=m.season_id
        WHERE e.minute IS NULL GROUP BY s.name, e.event_type
        """,
        note="Timing analysis must exclude these.",
    ))

    # --- duplicates -------------------------------------------------------
    checks.append(_check(
        session, "The same fixture stored twice (same teams, same day, same competition)", "ERROR",
        """
        SELECT s.name AS season, c.name AS competition, th.name AS home, ta.name AS away,
               DATE(m.starts_at) AS d, COUNT(*) AS copies, GROUP_CONCAT(m.source_id) AS source_ids
        FROM matches m JOIN seasons s ON s.id=m.season_id JOIN competitions c ON c.id=m.competition_id
        JOIN teams th ON th.id=m.home_team_id JOIN teams ta ON ta.id=m.away_team_id
        GROUP BY s.name, c.name, th.name, ta.name, DATE(m.starts_at)
        HAVING COUNT(*) > 1
        """,
        note="A legitimate double-header is possible but rare; check before deleting anything.",
    ))

    checks.append(_check(
        session, "Duplicate event rows (same match, team, player, type and minute)", "WARN",
        """
        SELECT m.id AS match_id, e.event_type, e.minute, e.team_id, e.player_id, COUNT(*) AS copies
        FROM match_events e JOIN matches m ON m.id=e.match_id
        GROUP BY m.id, e.event_type, e.minute, e.team_id, e.player_id
        HAVING COUNT(*) > 2
        """,
        note="Two identical rows is normal (two goals in the same minute); three-plus is worth a look.",
    ))

    # --- identity ---------------------------------------------------------
    checks.append(_check(
        session, "Names with leading/trailing whitespace", "ERROR",
        """
        SELECT 'team' AS kind, name FROM teams WHERE name <> TRIM(name)
        UNION ALL SELECT 'club', name FROM clubs WHERE name <> TRIM(name)
        UNION ALL SELECT 'competition', name FROM competitions WHERE name <> TRIM(name)
        UNION ALL SELECT 'player', CONCAT(first_name,' ',last_name) FROM players
          WHERE first_name <> TRIM(first_name) OR last_name <> TRIM(last_name)
        """,
        note="`ingest repair` trims these; the schemas now trim on the way in.",
    ))

    checks.append(_check(
        session, "Same team name under two team ids in one season", "WARN",
        """
        SELECT s.name AS season, t.name, COUNT(DISTINCT t.id) AS ids, GROUP_CONCAT(DISTINCT t.source_id) AS source_ids
        FROM matches m JOIN seasons s ON s.id=m.season_id
        JOIN teams t ON t.id IN (m.home_team_id, m.away_team_id)
        JOIN competitions c ON c.id=m.competition_id
        GROUP BY s.name, t.name, c.name LIKE '%%Women%%'
        HAVING COUNT(DISTINCT t.id) > 1
        """,
        note="A club rename or a split identity - candidate for aliases.py.",
    ))

    checks.append(_check(
        session, "Players scoring or carded for more than one team in a season", "WARN",
        """
        SELECT s.name AS season, CONCAT(p.first_name,' ',p.last_name) AS player,
               COUNT(DISTINCT e.team_id) AS teams, GROUP_CONCAT(DISTINCT t.name) AS team_names
        FROM match_events e JOIN matches m ON m.id=e.match_id JOIN seasons s ON s.id=m.season_id
        JOIN players p ON p.id=e.player_id JOIN teams t ON t.id=e.team_id
        WHERE e.player_id IS NOT NULL
        GROUP BY s.name, e.player_id HAVING COUNT(DISTINCT e.team_id) > 1
        """,
        note="Legitimate for a 1st/2nd XI player; two different clubs mid-season is worth checking.",
    ))

    # --- shootouts --------------------------------------------------------
    checks.append(_check(
        session, "Shootout recorded on a match that wasn't drawn", "ERROR",
        """
        SELECT s.name AS season, th.name AS home, ta.name AS away,
               CONCAT(m.home_score,'-',m.away_score) AS score,
               CONCAT(m.home_shootout,'-',m.away_shootout) AS shootout
        FROM matches m JOIN seasons s ON s.id=m.season_id
        JOIN teams th ON th.id=m.home_team_id JOIN teams ta ON ta.id=m.away_team_id
        WHERE m.home_shootout IS NOT NULL AND m.home_score <> m.away_score
        """,
    ))

    checks.append(_check(
        session, "Shootout that ended level", "ERROR",
        """
        SELECT s.name AS season, th.name AS home, ta.name AS away,
               CONCAT(m.home_shootout,'-',m.away_shootout) AS shootout
        FROM matches m JOIN seasons s ON s.id=m.season_id
        JOIN teams th ON th.id=m.home_team_id JOIN teams ta ON ta.id=m.away_team_id
        WHERE m.home_shootout IS NOT NULL AND m.home_shootout = m.away_shootout
        """,
    ))

    # --- structure --------------------------------------------------------
    checks.append(_check(
        session, "Premier Division seasons that aren't 118 PLAYED matches per gender", "WARN",
        f"""
        SELECT s.name AS season,
               CASE WHEN c.name LIKE '%%Women%%' THEN 'W' ELSE 'M' END AS gender,
               COUNT(*) AS played
        FROM matches m JOIN seasons s ON s.id=m.season_id JOIN competitions c ON c.id=m.competition_id
        WHERE c.name LIKE '%%Premier Division%%' AND m.status='R'
          AND s.name IN ('2022/23','2023/24','2024/25','2025/26')
        GROUP BY s.name, gender HAVING COUNT(*) <> {EXPECTED_PREM_MATCHES}
        """,
        note=f"Expected {EXPECTED_PREM_MATCHES} results: 66 regular + 12 finals pool + 30 phase 2 + 6 relegation + 4 finals."
             " Abandoned fixtures sit alongside these and are counted separately.",
    ))

    checks.append(_check(
        session, "A team playing itself", "ERROR",
        "SELECT id, source_id FROM matches WHERE home_team_id = away_team_id",
    ))

    checks.append(_check(
        session, "Matches whose teams' genders disagree with the competition", "WARN",
        """
        SELECT s.name AS season, c.name AS competition, th.name AS home, ta.name AS away
        FROM matches m JOIN seasons s ON s.id=m.season_id JOIN competitions c ON c.id=m.competition_id
        JOIN teams th ON th.id=m.home_team_id JOIN teams ta ON ta.id=m.away_team_id
        WHERE (c.name LIKE '%%Women%%' AND (th.name LIKE '%%M1' OR th.name LIKE '%%M2'))
           OR (c.name NOT LIKE '%%Women%%' AND (th.name LIKE '%%W1' OR th.name LIKE '%%W2'))
        """,
        note="Team names aren't a reliable gender marker, so a few hits here are expected.",
    ))

    checks.append(_check(
        session, "Status codes in use", "INFO",
        "SELECT status, COUNT(*) AS n FROM matches GROUP BY status ORDER BY n DESC",
        note="R=result, S=scheduled, A=abandoned, P=postponed, W=walkover. Always filter status='R'.",
    ))

    checks.append(_check(
        session, "Goal and card events with no player attached", "INFO",
        """
        SELECT s.name AS season,
               SUM(e.event_type IN ('FG','PC','PS')) AS goals_no_player,
               SUM(e.event_type IN ('GC','YC','RC')) AS cards_no_player
        FROM match_events e JOIN matches m ON m.id=e.match_id JOIN seasons s ON s.id=m.season_id
        WHERE e.player_id IS NULL GROUP BY s.name HAVING goals_no_player + cards_no_player > 0
        """,
        note="The source withholds the player when consent isn't given - not our bug.",
    ))

    # --- squads -----------------------------------------------------------
    checks.append(_check(
        session, "Played league-API matches from 2023/24 with no squad stored", "WARN",
        """
        SELECT s.name AS season, c.name AS competition, th.name AS home, ta.name AS away,
               DATE(m.starts_at) AS date
        FROM matches m
        JOIN seasons s ON s.id=m.season_id JOIN competitions c ON c.id=m.competition_id
        JOIN teams th ON th.id=m.home_team_id JOIN teams ta ON ta.id=m.away_team_id
        WHERE m.status='R' AND s.name >= '2023/24'
          AND m.source_id NOT LIKE 'altius:%' AND m.source_id NOT LIKE 'flashscore:%'
          AND NOT EXISTS (SELECT 1 FROM match_players mp WHERE mp.match_id=m.id)
        ORDER BY s.name DESC, m.starts_at
        """,
        note="Run `ingest backfill-squads --season <name>`; squads don't exist upstream before 2023/24.",
    ))

    checks.append(_check(
        session, "Squad rows for a team not playing in that match", "ERROR",
        """
        SELECT s.name AS season, th.name AS home, ta.name AS away, t.name AS squad_team
        FROM match_players mp
        JOIN matches m ON m.id=mp.match_id JOIN seasons s ON s.id=m.season_id
        JOIN teams t ON t.id=mp.team_id
        JOIN teams th ON th.id=m.home_team_id JOIN teams ta ON ta.id=m.away_team_id
        WHERE mp.team_id NOT IN (m.home_team_id, m.away_team_id)
        """,
    ))

    checks.append(_check(
        session, "Squads outside 11-18 players", "WARN",
        """
        SELECT s.name AS season, t.name AS team, DATE(m.starts_at) AS date, COUNT(*) AS squad
        FROM match_players mp
        JOIN matches m ON m.id=mp.match_id JOIN seasons s ON s.id=m.season_id
        JOIN teams t ON t.id=mp.team_id
        GROUP BY mp.match_id, mp.team_id, s.name, t.name, m.starts_at
        HAVING COUNT(*) NOT BETWEEN 11 AND 18
        ORDER BY s.name DESC, m.starts_at
        """,
    ))

    checks.append(_check(
        session, "Named goal/card events by a player not in their team's squad", "WARN",
        """
        SELECT s.name AS season, t.name AS team, DATE(m.starts_at) AS date,
               e.event_type, e.minute, e.player_id
        FROM match_events e
        JOIN matches m ON m.id=e.match_id JOIN seasons s ON s.id=m.season_id
        JOIN teams t ON t.id=e.team_id
        WHERE e.player_id IS NOT NULL
          AND EXISTS (SELECT 1 FROM match_players mp WHERE mp.match_id=e.match_id AND mp.team_id=e.team_id)
          AND NOT EXISTS (SELECT 1 FROM match_players mp
                          WHERE mp.match_id=e.match_id AND mp.team_id=e.team_id AND mp.player_id=e.player_id)
        ORDER BY s.name DESC, m.starts_at
        """,
        note="Usually a scorer-app slip or a player merge that missed the squad; check before trusting player stats.",
    ))

    return checks


def format_report(checks: list[Check]) -> str:
    out: list[str] = []
    errors = [c for c in checks if c.severity == "ERROR" and c.count]
    warns = [c for c in checks if c.severity == "WARN" and c.count]
    out.append("=" * 100)
    out.append(f"DATA AUDIT - {len(errors)} error check(s) firing, {len(warns)} warning check(s) firing, "
               f"{len(checks)} checks run")
    out.append("=" * 100)
    for sev in ("ERROR", "WARN", "INFO"):
        for c in checks:
            if c.severity != sev:
                continue
            flag = "FAIL" if (sev == "ERROR" and c.count) else ("look" if (sev == "WARN" and c.count) else "ok")
            out.append(f"\n[{sev}/{flag}] {c.name}: {c.count}")
            if c.note:
                out.append(f"    note: {c.note}")
            for r in c.sample:
                out.append("    " + ", ".join(f"{k}={v}" for k, v in r.items()))
            if c.count > len(c.sample):
                out.append(f"    ... and {c.count - len(c.sample)} more")
    return "\n".join(out)
