from __future__ import annotations

from datetime import datetime

from ingest.schemas import MatchIn, PlayerIn
from ingest.sources.eh_api import parse_fixture_detail
from ingest.sources.eh_api_fixtures import EhApiFixturesSource, _match_status
from ingest.settings import Settings


def _match(fixture_id: str = "FX1") -> MatchIn:
    return MatchIn(
        source_id=fixture_id,
        season_source_id="S",
        competition_source_id="C",
        home_team_source_id="T1",
        away_team_source_id="T2",
        starts_at=datetime(2026, 9, 19, 12, 0),
        status="R",
        home_score=2,
        away_score=1,
    )


DETAIL = {
    "homeTeam": {"id": "T1", "clubId": "CL1"},
    "awayTeam": {"id": "T2", "clubId": "CL2"},
    "allPlayers": [
        {"memberId": "P1", "displayName": "Nick Bandurak", "teamId": "T1", "consent": True},
        {"memberId": "P2", "displayName": "No Consent", "teamId": "T2", "consent": False},
        {"memberId": "P3", "displayName": "Ash Le Huray", "teamId": "T2", "consent": True},
    ],
    "fixtureEvents": [
        {"eventType": "FG", "teamId": "T1", "memberId": "P1", "minute": 12, "consent": True},
        {"eventType": "PC", "teamId": "T1", "memberId": "P1", "minute": 12, "consent": True},
        {"eventType": "PC", "teamId": "T1", "memberId": "P1", "minute": 12, "consent": True},
        {"eventType": "GC", "teamId": "T2", "memberId": "P2", "minute": 30, "consent": False},
        {"eventType": "YC", "teamId": None, "minute": 40},
    ],
    "officials": [{"displayName": "A Umpire", "role": "Umpire"}],
    "teamOfficials": [{"displayName": "A Coach"}],
}


def test_parse_fixture_detail_players_events_officials() -> None:
    players: dict[str, PlayerIn] = {}
    m = _match()
    parse_fixture_detail(DETAIL, "FX1", "CL1", "CL2", players, m)

    # consented players only, split into first/last, club from their team
    assert set(players) == {"P1", "P3"}
    assert (players["P1"].first_name, players["P1"].last_name) == ("Nick", "Bandurak")
    assert (players["P3"].first_name, players["P3"].last_name) == ("Ash Le", "Huray")
    assert players["P3"].club_source_id == "CL2"

    # duplicate (team, player, type, minute) events get a running index so none collapse;
    # the id scheme is what the historic importer wrote, so re-imports upsert not duplicate
    ids = [e.source_id for e in m.events]
    assert ids == [
        "FX1|T1|P1|FG|12|0",
        "FX1|T1|P1|PC|12|0",
        "FX1|T1|P1|PC|12|1",
        "FX1|T2||GC|30|0",
    ]
    # a card without consent keeps the event but drops the player
    gc = m.events[3]
    assert gc.player_source_id is None and gc.card == "green" and gc.type == "GC"
    # events with no team are dropped
    assert all(e.type != "YC" for e in m.events)

    assert [(o.name, o.role) for o in m.officials] == [("A Umpire", "Umpire"), ("A Coach", "Team Official")]


def test_live_source_skips_detail_for_matches_with_events() -> None:
    src = EhApiFixturesSource(
        Settings(), skip_detail_for={"FX1", "FX2"}
    )
    assert src._skip_detail_for == {"FX1", "FX2"}
    assert EhApiFixturesSource(
        Settings(), skip_detail_for={"FX1"}, refetch_all=True
    )._skip_detail_for == set()
    assert not EhApiFixturesSource(Settings(), fetch_details=False)._fetch_details


def test_match_status_mapping() -> None:
    # An unplayed fixture reports statusCode 'A' too - isResult is the discriminator.
    assert _match_status({"isResult": False, "statusCode": "A"}) == "S"
    assert _match_status({"isResult": True, "statusDescription": "Abandoned"}) == "A"
    assert _match_status({"isResult": True, "statusDescription": "Result"}) == "R"


def test_postponed_is_not_a_result() -> None:
    # The API sets isResult=true on a POSTPONED fixture and sends "P" as both
    # scores; mapping that to 'R' invented a 0-0 result. statusCode wins.
    fx = {"isResult": True, "statusCode": "P", "statusDescription": "Postponed",
          "homeTeamScore": "P", "awayTeamScore": "P"}
    assert _match_status(fx) == "P"
    assert _match_status({"isResult": True, "statusCode": "A", "statusDescription": "Abandoned"}) == "A"
    assert _match_status({"isResult": True, "statusCode": "W", "statusDescription": "Walkover"}) == "W"
    # A normal result still reads as one whatever the code spelling.
    assert _match_status({"isResult": True, "statusCode": "R"}) == "R"
    assert _match_status({"isResult": True, "statusCode": ""}) == "R"


def test_parse_fixture_detail_squad() -> None:
    detail = {
        "homeTeam": {"id": "T1", "clubId": "CL1"},
        "awayTeam": {"id": "T2", "clubId": "CL2"},
        "allPlayers": [
            {"memberId": "P1", "displayName": "Keeper One", "teamId": "T1", "consent": True,
             "shirtNumber": "1", "otherRoleDescription": "GK"},
            {"memberId": "P4", "displayName": "Skip Four", "teamId": "T1", "consent": True,
             "shirtNumber": 7, "otherRoleDescription": "CAPT"},
            {"memberId": "P2", "displayName": "No Consent", "teamId": "T2", "consent": False,
             "shirtNumber": 12, "otherRoleDescription": ""},
            {"memberId": "P5", "displayName": "Also Hidden", "teamId": "T2", "consent": False},
            {"memberId": "P1", "displayName": "Keeper One", "teamId": "T1", "consent": True},
            {"memberId": "P9", "displayName": "Wrong Team", "teamId": "T9", "consent": True},
        ],
    }
    m = _match()
    parse_fixture_detail(detail, "FX1", "CL1", "CL2", {}, m)
    by_id = {s.source_id: s for s in m.squad}

    # one row per squad place; a repeated listing doesn't double-count, a stray team is dropped
    assert set(by_id) == {"FX1|T1|P1", "FX1|T1|P4", "FX1|T2|anon0", "FX1|T2|anon1"}
    assert (by_id["FX1|T1|P1"].shirt_number, by_id["FX1|T1|P1"].is_goalkeeper) == (1, True)
    assert by_id["FX1|T1|P4"].is_captain and not by_id["FX1|T1|P4"].is_goalkeeper
    # no consent: counted in the squad, but no identity stored - not even the member id
    assert by_id["FX1|T2|anon0"].player_source_id is None
    assert by_id["FX1|T2|anon0"].shirt_number == 12
    assert all("P2" not in s.source_id and "P5" not in s.source_id for s in m.squad)
