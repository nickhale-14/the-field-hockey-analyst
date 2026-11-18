from __future__ import annotations

import json

from ingest.schemas import ClubIn, DatasetIn, MatchOfficialIn, PlayerIn, TeamIn


def test_dataset_parses_minimal_shape() -> None:
    raw = {
        "source": "mock",
        "season": {"source_id": "S1", "name": "2025/26"},
        "competitions": [{"source_id": "C1", "name": "League"}],
        "clubs": [{"source_id": "CL1", "name": "Club"}],
        "venues": [{"source_id": "V1", "name": "Pitch"}],
        "teams": [{"source_id": "T1", "club_source_id": "CL1", "name": "Team"}],
        "players": [
            {
                "source_id": "P1",
                "club_source_id": "CL1",
                "first_name": "A",
                "last_name": "B",
            }
        ],
        "matches": [
            {
                "source_id": "M1",
                "season_source_id": "S1",
                "competition_source_id": "C1",
                "home_team_source_id": "T1",
                "away_team_source_id": "T1",
                "starts_at": "2026-01-01T12:00:00Z",
                "status": "scheduled",
                "events": [],
                "officials": [],
            }
        ],
    }

    ds = DatasetIn.model_validate(json.loads(json.dumps(raw)))
    assert ds.source == "mock"
    assert ds.season.source_id == "S1"


def test_names_are_trimmed_on_the_way_in() -> None:
    # The EH API pads a few names ("Fareham ", "Loughborough Students "); every
    # importer must land the trimmed form or name joins silently miss.
    assert TeamIn(source_id="T1", club_source_id="C1", name="Fareham ").name == "Fareham"
    assert ClubIn(source_id="C1", name="  Loughborough Students ").name == "Loughborough Students"
    p = PlayerIn(source_id="P1", club_source_id="C1", first_name=" Nick", last_name="Bandurak ")
    assert (p.first_name, p.last_name) == ("Nick", "Bandurak")
    assert MatchOfficialIn(name="A Ref ").name == "A Ref"
    # Interior whitespace is content, not padding.
    assert TeamIn(source_id="T2", club_source_id="C1", name="Old  Georgians").name == "Old  Georgians"

