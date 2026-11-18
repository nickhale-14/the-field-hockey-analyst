"""
One-off / repeatable data-repair operations on rows already in the DB.

Everything here is idempotent: running it twice is a no-op the second time.
The loader already applies aliases on ingest; these exist for data that was
loaded before an alias or dedupe rule existed.
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.orm import Session

from ingest.aliases import CLUB_ALIASES, PLAYER_ALIASES, TEAM_ALIASES
from ingest.models import Club, Competition, Match, MatchEvent, MatchOfficial, MatchPlayer, Player, Team

SOURCE = "england_hockey"


def trim_names(session: Session) -> dict[str, int]:
    """
    Strip leading/trailing whitespace from every name column. The EH API sends
    a few names padded ("Fareham ", "Loughborough Students ") and any import
    that ran before the schemas trimmed on the way in wrote them verbatim -
    which silently breaks every name join (predictor output vs results, etc).
    """
    counts: dict[str, int] = {}
    for model, cols in (
        (Club, ("name",)),
        (Team, ("name",)),
        (Competition, ("name",)),
        (Player, ("first_name", "last_name")),
        (MatchOfficial, ("name",)),
    ):
        n = 0
        for col in cols:
            column = getattr(model, col)
            res = session.execute(
                update(model).where(column != func.trim(column)).values({col: func.trim(column)})
            )
            n += res.rowcount or 0
        counts[f"{model.__tablename__}_names_trimmed"] = n
    session.flush()
    return counts


def apply_score_corrections(session: Session) -> dict[str, int]:
    """
    Apply `corrections.SCORE_CORRECTIONS` - scores a better source proved wrong.
    Idempotent: a match already holding the corrected score is left alone.
    """
    from ingest.corrections import SCORE_CORRECTIONS

    fixed = 0
    for source_id, (home, away, _why) in SCORE_CORRECTIONS.items():
        res = session.execute(
            update(Match)
            .where(
                Match.source_id == source_id,
                (Match.home_score != home) | (Match.away_score != away),
            )
            .values(home_score=home, away_score=away)
        )
        fixed += res.rowcount or 0
    session.flush()
    return {"scores_corrected": fixed}


def remove_known_bad_events(session: Session) -> dict[str, int]:
    """
    Delete `corrections.EVENT_REMOVALS` - events the source logged that are not
    what they look like (e.g. a card shown to a coach rather than a player).
    Idempotent: an already-absent event is simply a no-op.
    """
    from ingest.corrections import EVENT_REMOVALS

    removed = 0
    for source_id, _why in EVENT_REMOVALS.items():
        res = session.execute(
            delete(MatchEvent).where(MatchEvent.source_id == source_id)
        )
        removed += res.rowcount or 0
    session.flush()
    return {"bad_events_removed": removed}


def identify_unnamed_squad_players(session: Session) -> dict[str, int]:
    """
    Apply `corrections.SQUAD_IDENTITIES` - unnamed squad places that the club's
    own team sheets identify. Creates the player (source "manual") if needed and
    points that team's NULL-player squad rows with the given shirt number in that
    season at them. Idempotent: rows already pointed are no longer NULL.
    """
    from ingest.corrections import SQUAD_IDENTITIES

    created = linked = 0
    for (team_sid, season, shirt), (player_sid, first, last, _why) in SQUAD_IDENTITIES.items():
        team = session.execute(
            select(Team).where(Team.source == SOURCE, Team.source_id == team_sid)
        ).scalar_one_or_none()
        if team is None:
            continue
        player = session.execute(
            select(Player).where(Player.source == "manual", Player.source_id == player_sid)
        ).scalar_one_or_none()
        if player is None:
            player = Player(source="manual", source_id=player_sid, club_id=team.club_id,
                            first_name=first, last_name=last)
            session.add(player)
            session.flush()
            created += 1
        res = session.execute(
            text(
                "UPDATE match_players mp JOIN matches m ON m.id = mp.match_id "
                "JOIN seasons s ON s.id = m.season_id "
                "SET mp.player_id = :pid "
                "WHERE mp.player_id IS NULL AND mp.team_id = :tid "
                "AND mp.shirt_number = :shirt AND s.name = :season"
            ),
            {"pid": player.id, "tid": team.id, "shirt": shirt, "season": season},
        )
        linked += res.rowcount or 0
    session.flush()
    return {"squad_players_identified": created, "squad_places_named": linked}


def remove_league_api_finals(session: Session) -> dict[str, int]:
    """
    Delete the league API's partial copies of the Premier Division Finals
    Weekend (competition names containing "Finals Weekend" whose source_id is
    a league-API GUID, not an altius: id). The Altius import is the complete
    record; keeping both would double-count the 2023/2024 men's finals. The
    importer no longer fetches these, so this only matters for a DB that was
    built before that change.
    """
    comps = session.scalars(
        select(Competition).where(
            Competition.source == SOURCE,
            Competition.name.like("%Finals Weekend%"),
            ~Competition.source_id.like("altius:%"),
        )
    ).all()
    matches_removed = events_removed = 0
    for comp in comps:
        match_ids = list(session.scalars(select(Match.id).where(Match.competition_id == comp.id)))
        if match_ids:
            res = session.execute(delete(MatchEvent).where(MatchEvent.match_id.in_(match_ids)))
            events_removed += res.rowcount or 0
            session.execute(delete(MatchOfficial).where(MatchOfficial.match_id.in_(match_ids)))
            session.execute(delete(MatchPlayer).where(MatchPlayer.match_id.in_(match_ids)))
            res = session.execute(delete(Match).where(Match.id.in_(match_ids)))
            matches_removed += res.rowcount or 0
        session.delete(comp)
    session.flush()
    return {
        "league_api_finals_competitions_removed": len(comps),
        "league_api_finals_matches_removed": matches_removed,
        "league_api_finals_events_removed": events_removed,
    }


def apply_aliases(session: Session) -> dict[str, int]:
    """Merge already-loaded rows for every entry in aliases.py."""
    merged_teams = renamed_teams = merged_clubs = renamed_clubs = 0
    merged_players = renamed_players = 0

    for old_sid, canon_sid in TEAM_ALIASES.items():
        old = session.scalar(select(Team).where(Team.source == SOURCE, Team.source_id == old_sid))
        if old is None:
            continue
        canon = session.scalar(select(Team).where(Team.source == SOURCE, Team.source_id == canon_sid))
        if canon is None:
            old.source_id = canon_sid  # nothing to merge into; adopt the canonical identity
            renamed_teams += 1
            continue
        session.execute(update(Match).where(Match.home_team_id == old.id).values(home_team_id=canon.id))
        session.execute(update(Match).where(Match.away_team_id == old.id).values(away_team_id=canon.id))
        session.execute(update(MatchEvent).where(MatchEvent.team_id == old.id).values(team_id=canon.id))
        session.execute(update(MatchPlayer).where(MatchPlayer.team_id == old.id).values(team_id=canon.id))
        session.delete(old)
        merged_teams += 1

    session.flush()

    for old_sid, canon_sid in CLUB_ALIASES.items():
        old = session.scalar(select(Club).where(Club.source == SOURCE, Club.source_id == old_sid))
        if old is None:
            continue
        canon = session.scalar(select(Club).where(Club.source == SOURCE, Club.source_id == canon_sid))
        if canon is None:
            old.source_id = canon_sid
            renamed_clubs += 1
            continue
        session.execute(update(Team).where(Team.club_id == old.id).values(club_id=canon.id))
        session.execute(update(Player).where(Player.club_id == old.id).values(club_id=canon.id))
        session.delete(old)
        merged_clubs += 1

    session.flush()

    for old_sid, canon_sid in PLAYER_ALIASES.items():
        old = session.scalar(select(Player).where(Player.source == SOURCE, Player.source_id == old_sid))
        if old is None:
            continue
        canon = session.scalar(select(Player).where(Player.source == SOURCE, Player.source_id == canon_sid))
        if canon is None:
            old.source_id = canon_sid
            renamed_players += 1
            continue
        session.execute(update(MatchEvent).where(MatchEvent.player_id == old.id).values(player_id=canon.id))
        session.execute(update(MatchPlayer).where(MatchPlayer.player_id == old.id).values(player_id=canon.id))
        session.delete(old)
        merged_players += 1

    session.flush()
    return {
        "teams_merged": merged_teams,
        "teams_renamed": renamed_teams,
        "clubs_merged": merged_clubs,
        "clubs_renamed": renamed_clubs,
        "players_merged": merged_players,
        "players_renamed": renamed_players,
    }


def repair_abandoned_replays(session: Session) -> dict[str, int]:
    """
    When an abandoned fixture is replayed, the club scorer sometimes logs the
    replayed match's events against the ORIGINAL fixture in the app - so the
    abandoned record (status 'A') carries goals and cards that belong to the
    replay (status 'R'), and the replay record is missing some of them. Seen
    once: Brooklands v Southgate, 20 Sep 2025 abandoned, replayed 5 Oct 2025 -
    the replay had the 4 goals, the abandoned record had the 4 goals AND the
    7 cards.

    Rule: for each abandoned match with events, find the replay (same season,
    same home/away, status 'R') and require that at least half of the
    abandoned record's goal events already appear on the replay (same type,
    minute, player) - that's the evidence they're the same game. Then move
    every event the replay doesn't already have onto it (source_id re-prefixed
    with the replay's fixture id so the upsert stays unique) and delete the
    duplicates. Idempotent; a re-ingest re-creates the stale copies and this
    removes them again.
    """
    moved = deleted = 0
    abandoned = session.scalars(select(Match).where(Match.status == "A")).all()
    for a in abandoned:
        a_events = session.scalars(select(MatchEvent).where(MatchEvent.match_id == a.id)).all()
        if not a_events:
            continue
        replay = session.scalar(
            select(Match).where(
                Match.status == "R", Match.season_id == a.season_id, Match.id != a.id,
                Match.home_team_id == a.home_team_id, Match.away_team_id == a.away_team_id,
            ).order_by(Match.starts_at)
        )
        if replay is None:
            continue
        r_keys = {
            (e.event_type, e.minute, e.player_id)
            for e in session.scalars(select(MatchEvent).where(MatchEvent.match_id == replay.id))
        }
        a_goals = [e for e in a_events if e.event_type in ("FG", "PC", "PS")]
        shared = sum((e.event_type, e.minute, e.player_id) in r_keys for e in a_goals)
        if not a_goals or shared * 2 < len(a_goals):
            continue  # not demonstrably the same game - leave it alone
        for e in a_events:
            if (e.event_type, e.minute, e.player_id) in r_keys:
                session.delete(e)
                deleted += 1
            else:
                e.match_id = replay.id
                e.source_id = e.source_id.replace(a.source_id, replay.source_id, 1)
                r_keys.add((e.event_type, e.minute, e.player_id))
                moved += 1
    session.flush()
    return {"abandoned_events_moved_to_replay": moved, "abandoned_duplicate_events_deleted": deleted}


def dedupe_players(session: Session) -> dict[str, int]:
    """
    Merge players that are the same real person split across several rows:
    identical (trimmed, case-folded) full name AND the same club. The lowest
    id survives; events are re-pointed to it.

    Same name at DIFFERENT clubs is deliberately left alone - that's usually
    two different people (or a genuine transfer), and wrongly combining two
    people's stats is worse than leaving one person split.
    """
    rows = session.execute(
        text(
            "SELECT id, club_id, LOWER(TRIM(CONCAT(first_name, ' ', last_name))) AS n "
            "FROM players ORDER BY id"
        )
    ).all()

    groups: dict[tuple[int, str], list[int]] = defaultdict(list)
    for pid, club_id, name in rows:
        groups[(club_id, name)].append(pid)

    merged_groups = removed = repointed = 0
    for ids in groups.values():
        if len(ids) < 2:
            continue
        keep, dupes = ids[0], ids[1:]  # ORDER BY id above => ids[0] is the lowest
        res = session.execute(
            update(MatchEvent).where(MatchEvent.player_id.in_(dupes)).values(player_id=keep)
        )
        repointed += res.rowcount or 0
        session.execute(
            update(MatchPlayer).where(MatchPlayer.player_id.in_(dupes)).values(player_id=keep)
        )
        session.execute(delete(Player).where(Player.id.in_(dupes)))
        removed += len(dupes)
        merged_groups += 1

    session.flush()
    return {"groups_merged": merged_groups, "players_removed": removed, "events_repointed": repointed}
