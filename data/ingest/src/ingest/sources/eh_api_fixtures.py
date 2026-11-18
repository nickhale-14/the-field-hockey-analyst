"""
Live-season fixture importer for the England Hockey API.

Deliberately kept separate from eh_api.py (the historic importer). That module
is proven and used for fully-completed seasons; this one exists for a season
that's still in progress, where most fixtures have no result yet. Mixing the
two concerns into one code path risked breaking the historic import while
chasing "handle an unplayed fixture" edge cases it was never designed for.

Only the genuinely generic bits are shared: EhApiClient (a plain HTTP wrapper)
and the DB schemas/loader (persistence plumbing, not fetch logic). The
competition name filter is intentionally duplicated rather than imported, so
this file has zero coupling to eh_api.py's internals.

Status code scheme written to matches.status (extends, doesn't change, the
existing 'R'/'A' convention used everywhere else):
  'R' - played, result recorded (same meaning as the historic importer's 'R')
  'A' - genuinely abandoned (isResult=true, statusCode 'A')
  'P' - postponed (isResult=true, statusCode 'P', scores sent as "P")
  'W' - awarded walkover (isResult=true, statusCode 'W')
  'S' - Scheduled: not yet played (isResult=false). New code - never appears
        in historic data, so `WHERE status = 'R'` keeps working unchanged.

The England Hockey API itself is ambiguous here: an unplayed fixture and an
abandoned one both report statusCode 'A'. They're only distinguishable via
isResult (false vs true) plus the longer statusDescription field. Confirmed
directly against the live API before writing this.

`isResult` does NOT mean "was played" - see `_match_status`. Only 'R', 'A' and
'W' carry a real scoreline; a postponement reports 0-0 and must store NULL.

Fixture detail (goals, cards, squads, officials) is fetched only for matches
that have been played ('R' or 'A' - an abandoned record can carry events, see
CLAUDE.md bug #5) and, by default, only for those the DB has no events for
yet, so a weekly re-run costs one call per newly played match rather than one
per match in the season. Clubs log events late sometimes, so a played match
with zero events is retried every run until something arrives. The parsing is
eh_api.parse_fixture_detail - shared on purpose, so event source_ids are
identical to the historic importer's and never double up.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ingest.schemas import (
    ClubIn,
    CompetitionIn,
    DatasetIn,
    MatchIn,
    PlayerIn,
    SeasonIn,
    TeamIn,
    VenueIn,
)
from ingest.settings import Settings
from ingest.sources.base import DataSource
from ingest.sources.eh_api import EhApiClient, parse_fixture_detail

logger = logging.getLogger(__name__)

PLAYED_STATUSES = ("R", "A")
# Statuses that carry a real scoreline. A postponement does not (see _match_status).
SCORED_STATUSES = ("R", "A", "W")

# Duplicated from eh_api.py on purpose - see module docstring.
TARGET_COMPETITIONS = (
    "Premier Division",
    "Division 1 North",
    "Division 1 South",
    "Conference North",
    "Conference West",
    "Conference East",
    "Conference Midlands",
    "League Finals Pool",
    "Phase 2 Top 6",
    "Phase 2 Lower 6",
    "Relegation Play Offs",
)


EXCLUDED_COMPETITIONS = ("Finals Weekend",)  # imported from Altius instead - see eh_api.py


def _is_target_competition(name: str) -> bool:
    if any(excluded in name for excluded in EXCLUDED_COMPETITIONS):
        return False
    return any(target in name for target in TARGET_COMPETITIONS)


def _parse_iso_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _match_status(fx: dict[str, Any]) -> str:
    """
    Map one API fixture onto our status codes.

    `isResult` is NOT "was played": the API sets it true for anything with a
    settled outcome, including a postponement (statusCode 'P', scores sent as
    the literal "P"). Trusting isResult alone recorded a postponed match as a
    0-0 result - caught 2026-09-23 by reconciling 2023/24 against the source,
    where the historic importer's statusCode-based mapping disagreed. So take
    statusCode whenever it names a real outcome and only fall back to 'R'.
    """
    if not fx.get("isResult"):
        return "S"
    code = (fx.get("statusCode") or "").strip().upper()
    if code in {"A", "P", "W"}:
        return code
    if (fx.get("statusDescription") or fx.get("status") or "") == "Abandoned":
        return "A"
    return "R"


class EhApiFixturesSource(DataSource):
    """
    Imports the full fixture list (played or not) for one live/current season,
    plus goals/cards/squads for played matches. Requires
    settings.eh_live_season_id to be set.

    `fetch_details`: pull `/fixtures/{id}` for played matches at all.
    `skip_detail_for`: match source_ids that already have events in the DB -
    not re-fetched unless `refetch_all`. The CLI computes this from the DB;
    the source itself never touches the DB.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        fetch_details: bool = True,
        skip_detail_for: set[str] | None = None,
        refetch_all: bool = False,
    ) -> None:
        self._settings = settings
        self._fetch_details = fetch_details
        self._skip_detail_for = set() if refetch_all else set(skip_detail_for or ())
        self.detail_fetches = 0

    def load(self) -> DatasetIn:
        settings = self._settings
        if not settings.eh_live_season_id:
            raise RuntimeError("EH_LIVE_SEASON_ID is required for the fixture importer")

        client = EhApiClient(settings)
        try:
            seasons = client.get_json("/seasons")
            season_obj = next(
                (s for s in seasons if s.get("id") == settings.eh_live_season_id), None
            )
            if season_obj is None:
                raise RuntimeError(
                    f"Season id {settings.eh_live_season_id} not found in /seasons"
                )
            season_id = season_obj["id"]
            season_name = season_obj.get("clubPortalSeasonDescription") or season_obj.get(
                "description", season_id
            )
            season = SeasonIn(source_id=season_id, name=season_name)

            groups = client.get_json(f"/seasons/{season_id}/competitiongroups")
            ehl_groups = [g for g in groups if "EHL" in (g.get("name") or "")]

            competitions_by_id: dict[str, CompetitionIn] = {}
            clubs_by_id: dict[str, ClubIn] = {}
            venues_by_id: dict[str, VenueIn] = {}
            teams_by_id: dict[str, TeamIn] = {}
            players_by_id: dict[str, PlayerIn] = {}
            matches_by_id: dict[str, MatchIn] = {}

            competitions: list[dict[str, Any]] = []
            for g in ehl_groups:
                group_id = g.get("id")
                if not group_id:
                    continue
                page_no = 1
                while True:
                    resp = client.get_json(
                        f"/competitiongroups/{group_id}/competitions",
                        params={"pageNo": page_no, "pageSize": settings.eh_page_size},
                    )
                    data = resp.get("data") if isinstance(resp, dict) else None
                    if not data:
                        break
                    competitions.extend(data)
                    if not resp.get("hasMoreData"):
                        break
                    page_no += 1

            for c in competitions:
                comp_id = c.get("id")
                comp_name = c.get("name") or ""
                if comp_id and _is_target_competition(comp_name):
                    competitions_by_id[comp_id] = CompetitionIn(
                        source_id=comp_id, name=comp_name or str(comp_id)
                    )

            for comp_id in list(competitions_by_id.keys()):
                matchdays_resp = client.get_json(f"/competitions/{comp_id}/matchdays")
                matchdays = (
                    matchdays_resp.get("matchDays")
                    if isinstance(matchdays_resp, dict)
                    else None
                )
                if not matchdays:
                    continue

                for md in matchdays:
                    date_id = md.get("id")
                    if not date_id:
                        continue

                    fixtures = client.get_json(f"/competitions/{comp_id}/matchdays/{date_id}")
                    if not isinstance(fixtures, list):
                        continue

                    for fx in fixtures:
                        fixture_id = fx.get("id")
                        fixture_date = fx.get("fixtureDate")
                        if not fixture_id or not fixture_date or fx.get("isBye"):
                            continue

                        venue_id = fx.get("venueId")
                        venue_name = fx.get("venue")
                        if venue_id:
                            venues_by_id[venue_id] = VenueIn(
                                source_id=venue_id, name=venue_name or str(venue_id)
                            )

                        home_team_obj = fx.get("homeTeam") or {}
                        away_team_obj = fx.get("awayTeam") or {}

                        home_team_id = fx.get("homeTeamId") or home_team_obj.get("id")
                        away_team_id = fx.get("awayTeamId") or away_team_obj.get("id")
                        home_club_id = home_team_obj.get("clubId") or fx.get("homeClubId")
                        away_club_id = away_team_obj.get("clubId") or fx.get("awayClubId")

                        if not (home_team_id and away_team_id and home_club_id and away_club_id):
                            continue

                        clubs_by_id[home_club_id] = ClubIn(
                            source_id=str(home_club_id),
                            name=home_team_obj.get("clubName") or str(home_club_id),
                            logo_url=home_team_obj.get("clubLogoUrl") or None,
                        )
                        clubs_by_id[away_club_id] = ClubIn(
                            source_id=str(away_club_id),
                            name=away_team_obj.get("clubName") or str(away_club_id),
                            logo_url=away_team_obj.get("clubLogoUrl") or None,
                        )
                        teams_by_id[str(home_team_id)] = TeamIn(
                            source_id=str(home_team_id),
                            club_source_id=str(home_club_id),
                            name=home_team_obj.get("teamName") or str(home_team_id),
                        )
                        teams_by_id[str(away_team_id)] = TeamIn(
                            source_id=str(away_team_id),
                            club_source_id=str(away_club_id),
                            name=away_team_obj.get("teamName") or str(away_team_id),
                        )

                        status = _match_status(fx)
                        match = MatchIn(
                            source_id=fixture_id,
                            season_source_id=season_id,
                            competition_source_id=comp_id,
                            home_team_source_id=str(home_team_id),
                            away_team_source_id=str(away_team_id),
                            venue_source_id=str(venue_id) if venue_id else None,
                            starts_at=_parse_iso_datetime(fixture_date),
                            status=status,
                            # A postponement carries isResult=true and scores of 0
                            # (the raw fields are the string "P") - that is not a 0-0.
                            home_score=fx.get("homeTeamScoreAsInt") if status in SCORED_STATUSES else None,
                            away_score=fx.get("awayTeamScoreAsInt") if status in SCORED_STATUSES else None,
                        )

                        if (
                            self._fetch_details
                            and match.status in PLAYED_STATUSES
                            and fixture_id not in self._skip_detail_for
                        ):
                            detail = client.get_json(f"/fixtures/{fixture_id}")
                            self.detail_fetches += 1
                            if isinstance(detail, dict):
                                parse_fixture_detail(
                                    detail,
                                    fixture_id,
                                    str(home_club_id),
                                    str(away_club_id),
                                    players_by_id,
                                    match,
                                )

                        matches_by_id[fixture_id] = match

            logger.info(
                "Live season: %d fixtures, %d fixture-detail fetches, %d matches with events",
                len(matches_by_id),
                self.detail_fetches,
                sum(1 for m in matches_by_id.values() if m.events),
            )
            return DatasetIn(
                # Same source label as the historic importer (eh_api.py) on purpose:
                # both describe the same real England Hockey API, and the DB's
                # uniqueness check is on (source, source_id) together - a different
                # label here would silently duplicate every team/club/venue/
                # competition that also exists in a historic season, since MySQL
                # wouldn't recognize them as the same row to upsert into. Confirmed
                # directly: England Hockey's own team IDs ARE stable across
                # seasons (e.g. Ashmoor: same source_id 2022-23 through 2026-27) -
                # this was purely a self-inflicted bug, not a source-system quirk.
                source="england_hockey",
                generated_at=datetime.utcnow(),
                season=season,
                competitions=list(competitions_by_id.values()),
                clubs=list(clubs_by_id.values()),
                venues=list(venues_by_id.values()),
                teams=list(teams_by_id.values()),
                players=list(players_by_id.values()),
                matches=list(matches_by_id.values()),
            )
        finally:
            client.close()
