from __future__ import annotations

from collections.abc import Iterable, Iterator

from datetime import timezone

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from ingest.models import (
    Club,
    Competition,
    Match,
    MatchEvent,
    MatchOfficial,
    MatchPlayer,
    Player,
    Season,
    Team,
    Venue,
)
from ingest.aliases import CLUB_ALIASES, PLAYER_ALIASES, TEAM_ALIASES
from ingest.schemas import DatasetIn


def _to_utc_naive(dt):
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _apply_aliases(session: Session, dataset: DatasetIn) -> None:
    """
    Rewrite every reference to an aliased team/club/player onto its canonical id, in place.

    If the dataset contains only the OLD identity (e.g. re-ingesting 2022-23 on
    its own) and the canonical row already exists in the DB, the old entity is
    dropped from the upsert so its stale name can't overwrite the canonical one.
    If the canonical row doesn't exist anywhere yet, the old entity is kept under
    the canonical id - the name gets corrected the next time the canonical
    season is ingested.
    """
    if not TEAM_ALIASES and not CLUB_ALIASES and not PLAYER_ALIASES:
        return

    def _dedupe(items, alias_map, table):
        canonical_in_dataset = {i.source_id for i in items if i.source_id not in alias_map}
        canonical_in_db = set(
            _fetch_id_map(session, table, dataset.source, alias_map.values()).keys()
        )
        kept, seen = [], set()
        for item in items:
            if item.source_id in alias_map:
                canon = alias_map[item.source_id]
                if canon in canonical_in_dataset or canon in canonical_in_db:
                    continue  # canonical exists elsewhere; don't upsert the stale name
                item.source_id = canon
            if item.source_id in seen:
                continue
            seen.add(item.source_id)
            kept.append(item)
        return kept

    dataset.clubs = _dedupe(dataset.clubs, CLUB_ALIASES, Club.__table__)
    for t in dataset.teams:
        t.club_source_id = CLUB_ALIASES.get(t.club_source_id, t.club_source_id)
    dataset.teams = _dedupe(dataset.teams, TEAM_ALIASES, Team.__table__)
    for p in dataset.players:
        p.club_source_id = CLUB_ALIASES.get(p.club_source_id, p.club_source_id)
    dataset.players = _dedupe(dataset.players, PLAYER_ALIASES, Player.__table__)
    for m in dataset.matches:
        m.home_team_source_id = TEAM_ALIASES.get(m.home_team_source_id, m.home_team_source_id)
        m.away_team_source_id = TEAM_ALIASES.get(m.away_team_source_id, m.away_team_source_id)
        for e in m.events:
            e.team_source_id = TEAM_ALIASES.get(e.team_source_id, e.team_source_id)
            if e.player_source_id:
                e.player_source_id = PLAYER_ALIASES.get(e.player_source_id, e.player_source_id)
        for sq in m.squad:
            sq.team_source_id = TEAM_ALIASES.get(sq.team_source_id, sq.team_source_id)
            if sq.player_source_id:
                sq.player_source_id = PLAYER_ALIASES.get(sq.player_source_id, sq.player_source_id)


def _chunked(items: list, size: int) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _upsert_rows(
    session: Session,
    table,
    rows: list[dict],
    update_columns: list[str],
    keep_existing_if_null: tuple[str, ...] = (),
) -> None:
    if not rows:
        return

    # MySQL has practical limits on statement size/max packet; keep batches modest.
    for batch in _chunked(rows, size=500):
        stmt = mysql_insert(table).values(batch)
        update_dict = {col: stmt.inserted[col] for col in update_columns}

        # Sources sometimes send a row with a field missing that an earlier run
        # had populated; don't let an incoming NULL wipe a known-good value.
        for col in keep_existing_if_null:
            update_dict[col] = func.coalesce(stmt.inserted[col], table.c[col])

        if "updated_at" in table.c:
            update_dict["updated_at"] = func.now()

        session.execute(stmt.on_duplicate_key_update(**update_dict))


def _fetch_id_map(
    session: Session, table, source: str, source_ids: Iterable[str]
) -> dict[str, int]:
    source_ids = list(source_ids)
    if not source_ids:
        return {}

    out: dict[str, int] = {}
    for batch in _chunked(source_ids, size=1000):
        stmt: Select = select(table.c.source_id, table.c.id).where(
            table.c.source == source, table.c.source_id.in_(batch)
        )
        rows = session.execute(stmt).all()
        out.update({source_id: _id for (source_id, _id) in rows})
    return out


def _referenced_ids(dataset: DatasetIn) -> dict[str, set[str]]:
    """
    Every source_id the dataset points at, per table - not just the rows it
    carries. A dataset may reference clubs/teams/players that already exist
    in the DB without re-sending them (the Finals Weekend importer does this:
    it never creates teams, only refers to the league ones).
    """
    clubs = {c.source_id for c in dataset.clubs}
    clubs |= {t.club_source_id for t in dataset.teams}
    clubs |= {p.club_source_id for p in dataset.players}
    teams = {t.source_id for t in dataset.teams}
    players = {p.source_id for p in dataset.players}
    venues = {v.source_id for v in dataset.venues}
    competitions = {c.source_id for c in dataset.competitions}
    for m in dataset.matches:
        teams |= {m.home_team_source_id, m.away_team_source_id}
        competitions.add(m.competition_source_id)
        if m.venue_source_id:
            venues.add(m.venue_source_id)
        for e in m.events:
            teams.add(e.team_source_id)
            if e.player_source_id:
                players.add(e.player_source_id)
        for sq in m.squad:
            teams.add(sq.team_source_id)
            if sq.player_source_id:
                players.add(sq.player_source_id)
    return {"clubs": clubs, "teams": teams, "players": players, "venues": venues, "competitions": competitions}


def _require_all(kind: str, id_map: dict[str, int], wanted: set[str]) -> None:
    missing = sorted(wanted - id_map.keys())
    if missing:
        raise RuntimeError(
            f"{len(missing)} {kind} referenced by the dataset do not exist in the DB and were not "
            f"included in it: {missing[:5]}{' ...' if len(missing) > 5 else ''}"
        )


def load_dataset(session: Session, dataset: DatasetIn) -> dict[str, int]:
    """
    Loads a dataset into MySQL using idempotent upserts.

    Returns a simple count summary.
    """
    _apply_aliases(session, dataset)
    source = dataset.source
    counts: dict[str, int] = {}
    referenced = _referenced_ids(dataset)

    _upsert_rows(
        session,
        Season.__table__,
        [
            {
                "source": source,
                "source_id": dataset.season.source_id,
                "name": dataset.season.name,
            }
        ],
        update_columns=["name"],
    )
    season_map = _fetch_id_map(
        session, Season.__table__, source, [dataset.season.source_id]
    )

    _upsert_rows(
        session,
        Competition.__table__,
        [
            {"source": source, "source_id": c.source_id, "name": c.name}
            for c in dataset.competitions
        ],
        update_columns=["name"],
    )
    competition_map = _fetch_id_map(session, Competition.__table__, source, referenced["competitions"])
    _require_all("competitions", competition_map, referenced["competitions"])

    _upsert_rows(
        session,
        Club.__table__,
        [
            {
                "source": source,
                "source_id": c.source_id,
                "name": c.name,
                "logo_url": c.logo_url,
            }
            for c in dataset.clubs
        ],
        update_columns=["name", "logo_url"],
        keep_existing_if_null=("logo_url",),
    )
    club_map = _fetch_id_map(session, Club.__table__, source, referenced["clubs"])
    _require_all("clubs", club_map, referenced["clubs"])

    _upsert_rows(
        session,
        Venue.__table__,
        [
            {
                "source": source,
                "source_id": v.source_id,
                "name": v.name,
                "city": v.city,
            }
            for v in dataset.venues
        ],
        update_columns=["name", "city"],
    )
    venue_map = _fetch_id_map(session, Venue.__table__, source, referenced["venues"])

    _upsert_rows(
        session,
        Team.__table__,
        [
            {
                "source": source,
                "source_id": t.source_id,
                "club_id": club_map[t.club_source_id],
                "name": t.name,
            }
            for t in dataset.teams
        ],
        update_columns=["club_id", "name"],
    )
    team_map = _fetch_id_map(session, Team.__table__, source, referenced["teams"])
    _require_all("teams", team_map, referenced["teams"])

    _upsert_rows(
        session,
        Player.__table__,
        [
            {
                "source": source,
                "source_id": p.source_id,
                "club_id": club_map[p.club_source_id],
                "first_name": p.first_name,
                "last_name": p.last_name,
            }
            for p in dataset.players
        ],
        update_columns=["club_id", "first_name", "last_name"],
    )
    # Players are the one soft reference: an event whose player is unknown
    # still loads (player_id NULL), matching how the API importer behaves.
    player_map = _fetch_id_map(session, Player.__table__, source, referenced["players"])

    match_rows: list[dict] = []
    for m in dataset.matches:
        match_rows.append(
            {
                "source": source,
                "source_id": m.source_id,
                "season_id": season_map[m.season_source_id],
                "competition_id": competition_map[m.competition_source_id],
                "home_team_id": team_map[m.home_team_source_id],
                "away_team_id": team_map[m.away_team_source_id],
                "venue_id": venue_map.get(m.venue_source_id) if m.venue_source_id else None,
                "starts_at": _to_utc_naive(m.starts_at),
                "status": m.status,
                "home_score": m.home_score,
                "away_score": m.away_score,
                "home_shootout": m.home_shootout,
                "away_shootout": m.away_shootout,
            }
        )

    _upsert_rows(
        session,
        Match.__table__,
        match_rows,
        update_columns=[
            "season_id",
            "competition_id",
            "home_team_id",
            "away_team_id",
            "venue_id",
            "starts_at",
            "status",
            "home_score",
            "away_score",
            "home_shootout",
            "away_shootout",
        ],
    )
    match_map = _fetch_id_map(
        session, Match.__table__, source, [m.source_id for m in dataset.matches]
    )

    event_rows: list[dict] = []
    squad_rows: list[dict] = []
    official_rows: list[dict] = []
    for m in dataset.matches:
        match_id = match_map[m.source_id]

        for e in m.events:
            event_rows.append(
                {
                    "source": source,
                    "source_id": e.source_id,
                    "match_id": match_id,
                    "team_id": team_map[e.team_source_id],
                    "player_id": player_map.get(e.player_source_id)
                    if e.player_source_id
                    else None,
                    "event_type": e.type,
                    "period": e.period,
                    "minute": e.minute,
                    "card": e.card,
                }
            )

        for sq in m.squad:
            squad_rows.append(
                {
                    "source": source,
                    "source_id": sq.source_id,
                    "match_id": match_id,
                    "team_id": team_map[sq.team_source_id],
                    "player_id": player_map.get(sq.player_source_id)
                    if sq.player_source_id
                    else None,
                    "shirt_number": sq.shirt_number,
                    "is_goalkeeper": sq.is_goalkeeper,
                    "is_captain": sq.is_captain,
                }
            )

        for o in m.officials:
            role = o.role or "Official"
            source_id = f"{m.source_id}:{role}:{o.name}"
            official_rows.append(
                {
                    "source": source,
                    "source_id": source_id,
                    "match_id": match_id,
                    "name": o.name,
                    "role": role,
                }
            )

    _upsert_rows(
        session,
        MatchEvent.__table__,
        event_rows,
        update_columns=["match_id", "team_id", "player_id", "event_type", "period", "minute", "card"],
    )

    _upsert_rows(
        session,
        MatchPlayer.__table__,
        squad_rows,
        update_columns=["match_id", "team_id", "player_id", "shirt_number", "is_goalkeeper", "is_captain"],
    )

    _upsert_rows(
        session,
        MatchOfficial.__table__,
        official_rows,
        update_columns=["match_id", "name", "role"],
    )

    counts["seasons"] = 1
    counts["competitions"] = len(dataset.competitions)
    counts["clubs"] = len(dataset.clubs)
    counts["venues"] = len(dataset.venues)
    counts["teams"] = len(dataset.teams)
    counts["players"] = len(dataset.players)
    counts["matches"] = len(dataset.matches)
    counts["match_events"] = sum(len(m.events) for m in dataset.matches)
    counts["match_players"] = sum(len(m.squad) for m in dataset.matches)
    counts["match_officials"] = sum(len(m.officials) for m in dataset.matches)

    return counts
