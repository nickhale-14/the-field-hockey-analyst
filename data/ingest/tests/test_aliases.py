from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from ingest.loaders import mysql as loader
from ingest.schemas import ClubIn, DatasetIn, MatchEventIn, MatchIn, PlayerIn, SeasonIn, TeamIn

OLD_TEAM, CANON_TEAM = "team-old", "team-canon"
OLD_CLUB, CANON_CLUB = "club-old", "club-canon"


def _dataset(*, include_canonical: bool) -> DatasetIn:
    clubs = [ClubIn(source_id=OLD_CLUB, name="Exe HC")]
    teams = [TeamIn(source_id=OLD_TEAM, club_source_id=OLD_CLUB, name="Exe 1")]
    if include_canonical:
        clubs.append(ClubIn(source_id=CANON_CLUB, name="Exeter HC"))
        teams.append(TeamIn(source_id=CANON_TEAM, club_source_id=CANON_CLUB, name="Exeter 1"))
    return DatasetIn(
        source="england_hockey",
        season=SeasonIn(source_id="s", name="2022/23"),
        clubs=clubs,
        teams=teams,
        players=[PlayerIn(source_id="p1", club_source_id=OLD_CLUB, first_name="A", last_name="B")],
        matches=[
            MatchIn(
                source_id="m1",
                season_source_id="s",
                competition_source_id="c",
                home_team_source_id=OLD_TEAM,
                away_team_source_id="other",
                starts_at=datetime(2022, 9, 1),
                status="R",
                events=[MatchEventIn(source_id="e1", type="FG", team_source_id=OLD_TEAM, player_source_id="p1")],
            )
        ],
    )


def _run(dataset: DatasetIn, *, canonical_in_db: bool, player_aliases: dict[str, str] | None = None) -> None:
    with (
        patch.object(loader, "TEAM_ALIASES", {OLD_TEAM: CANON_TEAM}),
        patch.object(loader, "CLUB_ALIASES", {OLD_CLUB: CANON_CLUB}),
        patch.object(loader, "PLAYER_ALIASES", player_aliases or {}),
        patch.object(
            loader,
            "_fetch_id_map",
            lambda session, table, source, ids: (
                {sid: 1 for sid in ids} if canonical_in_db else {}
            ),
        ),
    ):
        loader._apply_aliases(session=None, dataset=dataset)


def test_references_are_rewritten_to_canonical() -> None:
    ds = _dataset(include_canonical=True)
    _run(ds, canonical_in_db=False)

    m = ds.matches[0]
    assert m.home_team_source_id == CANON_TEAM
    assert m.away_team_source_id == "other"  # untouched
    assert m.events[0].team_source_id == CANON_TEAM
    assert ds.players[0].club_source_id == CANON_CLUB


def test_stale_entity_dropped_when_canonical_present_in_dataset() -> None:
    ds = _dataset(include_canonical=True)
    _run(ds, canonical_in_db=False)

    assert [t.source_id for t in ds.teams] == [CANON_TEAM]
    assert [t.name for t in ds.teams] == ["Exeter 1"]  # old name must not win
    assert [c.name for c in ds.clubs] == ["Exeter HC"]


def test_stale_entity_dropped_when_canonical_already_in_db() -> None:
    ds = _dataset(include_canonical=False)
    _run(ds, canonical_in_db=True)

    # Nothing to upsert for the team/club - canonical row already exists and
    # its name must not be overwritten by "Exe".
    assert ds.teams == []
    assert ds.clubs == []
    # ...but references still point at the canonical id.
    assert ds.matches[0].home_team_source_id == CANON_TEAM


def test_player_alias_rewrites_events_and_drops_stale_row() -> None:
    # A second source spelt the player differently and created "p1"; the real
    # row "p-canon" already exists in the DB.
    ds = _dataset(include_canonical=True)
    _run(ds, canonical_in_db=True, player_aliases={"p1": "p-canon"})

    assert ds.players == []  # stale spelling never upserted
    assert ds.matches[0].events[0].player_source_id == "p-canon"


def test_player_alias_adopts_canonical_id_when_canonical_unknown() -> None:
    ds = _dataset(include_canonical=True)
    _run(ds, canonical_in_db=False, player_aliases={"p1": "p-canon"})

    assert [p.source_id for p in ds.players] == ["p-canon"]
    assert ds.matches[0].events[0].player_source_id == "p-canon"


def test_stale_entity_adopts_canonical_id_when_canonical_unknown() -> None:
    ds = _dataset(include_canonical=False)
    _run(ds, canonical_in_db=False)

    # Old season ingested first on a fresh DB: keep the row so FKs resolve,
    # but under the canonical id.
    assert [t.source_id for t in ds.teams] == [CANON_TEAM]
    assert [c.source_id for c in ds.clubs] == [CANON_CLUB]
    assert ds.teams[0].club_source_id == CANON_CLUB
