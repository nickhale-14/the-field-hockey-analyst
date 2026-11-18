from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from datetime import datetime
from typing import Any

import httpx

from ingest.schemas import (
    ClubIn,
    CompetitionIn,
    DatasetIn,
    MatchEventIn,
    MatchIn,
    MatchOfficialIn,
    MatchPlayerIn,
    PlayerIn,
    SeasonIn,
    TeamIn,
    VenueIn,
)
from ingest.settings import Settings
from ingest.sources.base import DataSource

logger = logging.getLogger(__name__)


def _parse_iso_datetime(value: str) -> datetime:
    # The EH API uses ISO-8601 strings, sometimes without timezone info.
    # Python's fromisoformat handles both `YYYY-MM-DDTHH:MM:SS` and offsets.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _split_name(display_name: str) -> tuple[str, str]:
    parts = display_name.split()
    if not parts:
        return ("", "")
    if len(parts) == 1:
        return (parts[0], "")
    return (" ".join(parts[:-1]), parts[-1])


def _card_from_event_type(event_type: str) -> str | None:
    mapping = {"GC": "green", "YC": "yellow", "RC": "red"}
    return mapping.get(event_type)


# National leagues only (Men's/Open + Women's): main divisions plus their
# playoffs/finals. Excludes junior, masters, county cup, and regional groups.
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


# The league API's copy of the end-of-season Finals Weekend is incomplete
# (men's only, two seasons, one placeholder "TBC v TBC" row, no shootouts).
# The complete record comes from Altius (sources/eh_altius.py), so these are
# skipped here to avoid double-counting the same matches.
EXCLUDED_COMPETITIONS = ("Finals Weekend",)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_target_competition(name: str) -> bool:
    if any(excluded in name for excluded in EXCLUDED_COMPETITIONS):
        return False
    return any(target in name for target in TARGET_COMPETITIONS)


def parse_fixture_detail(
    detail: dict[str, Any],
    fixture_id: str,
    home_club_id: str | None,
    away_club_id: str | None,
    players_by_id: dict[str, PlayerIn],
    match: MatchIn,
) -> None:
    """
    Fold one `/fixtures/{id}` payload into `players_by_id` and `match.events` /
    `match.officials`. Shared by the historic and live-season importers so both
    produce identical event source_ids - re-ingesting a season from either path
    upserts the same rows instead of duplicating them.
    """
    # Players
    team_to_club: dict[str, str] = {}
    for team_key, club_key in (
        ("homeTeam", "homeClubId"),
        ("awayTeam", "awayClubId"),
    ):
        team_obj = detail.get(team_key) or {}
        team_id = team_obj.get("id") or detail.get(f"{team_key[:-4]}TeamId")
        club_id = team_obj.get("clubId") or detail.get(club_key)
        if team_id and club_id:
            team_to_club[str(team_id)] = str(club_id)

    for p in detail.get("allPlayers") or []:
        if not isinstance(p, dict):
            continue
        if not p.get("consent"):
            continue
        member_id = p.get("memberId")
        display_name = p.get("displayName")
        team_id = p.get("teamId")
        if not member_id or not display_name:
            continue
        club_id = team_to_club.get(str(team_id or "")) or home_club_id or away_club_id
        if not club_id:
            continue
        first_name, last_name = _split_name(str(display_name))
        players_by_id[str(member_id)] = PlayerIn(
            source_id=str(member_id),
            club_source_id=str(club_id),
            first_name=first_name,
            last_name=last_name,
        )

    # Squads: every listed player, so squad sizes are right. Someone who hasn't
    # consented gets a place in the squad but no identity (not even memberId).
    anon: defaultdict[str, int] = defaultdict(int)
    in_squad: set[str] = set()
    match_teams = {str(t) for t in team_to_club} | {
        str(detail.get(k)) for k in ("homeTeamId", "awayTeamId") if detail.get(k)
    }
    for p in detail.get("allPlayers") or []:
        if not isinstance(p, dict) or not p.get("teamId"):
            continue
        team_id = str(p["teamId"])
        if match_teams and team_id not in match_teams:
            continue  # a team id that isn't one of the two sides would fail the load
        named = bool(p.get("consent") and p.get("memberId") and p.get("displayName"))
        if named:
            key = str(p["memberId"])
        else:
            key = f"anon{anon[team_id]}"
            anon[team_id] += 1
        source_id = f"{fixture_id}|{team_id}|{key}"
        if source_id in in_squad:
            continue
        in_squad.add(source_id)
        roles = set(re.split(r"[^A-Z]+", str(p.get("otherRoleDescription") or "").upper()))
        match.squad.append(
            MatchPlayerIn(
                source_id=source_id,
                team_source_id=team_id,
                player_source_id=key if named else None,
                shirt_number=_int_or_none(p.get("shirtNumber")),
                is_goalkeeper="GK" in roles,
                is_captain="CAPT" in roles,
            )
        )

    # Events
    seen: defaultdict[str, int] = defaultdict(int)
    for ev in detail.get("fixtureEvents") or []:
        if not isinstance(ev, dict):
            continue
        minute = ev.get("minute")
        event_type = ev.get("eventType")
        team_id = ev.get("teamId")
        consent = bool(ev.get("consent"))
        member_id = ev.get("memberId") if consent else None

        if event_type is None or team_id is None:
            continue

        base = f"{fixture_id}|{team_id}|{member_id or ''}|{event_type}|{minute or ''}"
        idx = seen[base]
        seen[base] += 1
        source_id = f"{base}|{idx}"

        match.events.append(
            MatchEventIn(
                source_id=source_id,
                type=str(event_type),
                team_source_id=str(team_id),
                player_source_id=str(member_id) if member_id else None,
                minute=int(minute) if minute is not None else None,
                card=_card_from_event_type(str(event_type)),
            )
        )

    # Officials (match officials + team officials)
    for o in detail.get("officials") or []:
        if not isinstance(o, dict):
            continue
        name = o.get("displayName")
        role = o.get("role") or "Official"
        if name:
            match.officials.append(MatchOfficialIn(name=str(name), role=str(role)))

    for o in detail.get("teamOfficials") or []:
        if not isinstance(o, dict):
            continue
        name = o.get("displayName")
        role = o.get("role") or "Team Official"
        if name:
            match.officials.append(MatchOfficialIn(name=str(name), role=str(role)))


class EhApiClient:
    def __init__(self, settings: Settings) -> None:
        headers: dict[str, str] = {"accept": "application/json"}
        if settings.eh_api_key:
            headers["x-api-key"] = settings.eh_api_key

        self._client = httpx.Client(
            base_url=settings.eh_api_base_url.rstrip("/"),
            headers=headers,
            timeout=settings.eh_request_timeout_seconds,
        )

    def close(self) -> None:
        self._client.close()

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        retry_statuses = {429, 500, 502, 503, 504}
        max_attempts = 5

        for attempt in range(1, max_attempts + 1):
            resp = self._client.get(path, params=params)
            if resp.status_code in retry_statuses and attempt < max_attempts:
                sleep_s = 0.5 * (2 ** (attempt - 1))
                logger.warning(
                    "EH API %s returned %s; retrying in %.1fs (%s/%s)",
                    path,
                    resp.status_code,
                    sleep_s,
                    attempt,
                    max_attempts,
                )
                time.sleep(sleep_s)
                continue

            resp.raise_for_status()
            return resp.json()


class EhApiSource(DataSource):
    """
    Loads England Hockey data via the EH Data Warehouse API.

    Assumptions:
    - `EH_API_KEY` is sent as `x-api-key` (if provided).
    - `EH_SEASON_ID` is preferred to limit scope while iterating.
    - `EH_COMPETITION_ID` can be used to ingest a single competition (bypasses
      the national-league name filter).
    - Only competition groups with "EHL" in the name are scanned, and within
      those, only competitions matching `TARGET_COMPETITIONS` are ingested.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def load(self) -> DatasetIn:
        settings = self._settings
        client = EhApiClient(settings)
        try:
            seasons = client.get_json("/seasons")
            if not isinstance(seasons, list) or not seasons:
                raise RuntimeError("EH API /seasons did not return a non-empty list")

            season_obj = None
            if settings.eh_season_id:
                for s in seasons:
                    if s.get("id") == settings.eh_season_id:
                        season_obj = s
                        break
                if season_obj is None:
                    raise RuntimeError(
                        f"Season id {settings.eh_season_id} not found in /seasons"
                    )
            else:
                # Default to the latest season by numeric year.
                season_obj = max(seasons, key=lambda s: s.get("year") or 0)

            season_id = season_obj["id"]
            season_name = season_obj.get("clubPortalSeasonDescription") or season_obj.get(
                "description", season_id
            )

            season = SeasonIn(source_id=season_id, name=season_name)

            competitions_by_id: dict[str, CompetitionIn] = {}
            clubs_by_id: dict[str, ClubIn] = {}
            venues_by_id: dict[str, VenueIn] = {}
            teams_by_id: dict[str, TeamIn] = {}
            players_by_id: dict[str, PlayerIn] = {}
            matches_by_id: dict[str, MatchIn] = {}

            if settings.eh_competition_id:
                comp = client.get_json(f"/competitions/{settings.eh_competition_id}")
                competitions = [comp]
            else:
                groups = client.get_json(f"/seasons/{season_id}/competitiongroups")
                # National leagues live under the "EHL" competition groups
                # (Men's/Open + Women's). Skip junior, masters, county cup,
                # and regional groups before spending API calls on them.
                ehl_groups = [g for g in groups if "EHL" in (g.get("name") or "")]
                competitions = []
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
                if comp_id and (settings.eh_competition_id or _is_target_competition(comp_name)):
                    competitions_by_id[comp_id] = CompetitionIn(
                        source_id=comp_id, name=comp_name or str(comp_id)
                    )

            # Fetch fixtures for each competition.
            for comp_id in list(competitions_by_id.keys()):
                matchdays_resp = client.get_json(f"/competitions/{comp_id}/matchdays")
                matchdays = matchdays_resp.get("matchDays") if isinstance(matchdays_resp, dict) else None
                if not matchdays:
                    continue

                for md in matchdays:
                    date_id = md.get("id")
                    if not date_id:
                        continue

                    fixtures = client.get_json(
                        f"/competitions/{comp_id}/matchdays/{date_id}"
                    )
                    if not isinstance(fixtures, list):
                        continue

                    for fx in fixtures:
                        fixture_id = fx.get("id")
                        if not fixture_id:
                            continue

                        # Venue
                        venue_id = fx.get("venueId")
                        venue_name = fx.get("venue")
                        if venue_id:
                            venues_by_id[venue_id] = VenueIn(
                                source_id=venue_id, name=venue_name or str(venue_id)
                            )

                        # Teams + Clubs
                        def _ingest_team(team_obj: dict[str, Any] | None) -> str | None:
                            if not team_obj:
                                return None
                            team_id = team_obj.get("id")
                            team_name = team_obj.get("teamName")
                            club_id = team_obj.get("clubId")
                            club_name = team_obj.get("clubName")
                            if club_id:
                                clubs_by_id[club_id] = ClubIn(
                                    source_id=club_id,
                                    name=club_name or str(club_id),
                                    logo_url=team_obj.get("clubLogoUrl") or None,
                                )
                            if team_id and club_id:
                                teams_by_id[team_id] = TeamIn(
                                    source_id=team_id,
                                    club_source_id=club_id,
                                    name=team_name or str(team_id),
                                )
                            return team_id

                        home_team_obj = fx.get("homeTeam")
                        away_team_obj = fx.get("awayTeam")
                        _ingest_team(home_team_obj)
                        _ingest_team(away_team_obj)
                        home_team_id = fx.get("homeTeamId") or (home_team_obj or {}).get(
                            "id"
                        )
                        away_team_id = fx.get("awayTeamId") or (away_team_obj or {}).get(
                            "id"
                        )
                        if not home_team_id or not away_team_id:
                            continue

                        # If the nested objects were missing, fall back to IDs and club IDs (names may be missing).
                        home_club_id = (
                            (home_team_obj or {}).get("clubId") or fx.get("homeClubId")
                        )
                        away_club_id = (
                            (away_team_obj or {}).get("clubId") or fx.get("awayClubId")
                        )

                        # Ensure placeholder club/team rows exist even if nested objects are incomplete.
                        if home_club_id and home_club_id not in clubs_by_id:
                            clubs_by_id[home_club_id] = ClubIn(
                                source_id=str(home_club_id), name=str(home_club_id)
                            )
                        if away_club_id and away_club_id not in clubs_by_id:
                            clubs_by_id[away_club_id] = ClubIn(
                                source_id=str(away_club_id), name=str(away_club_id)
                            )

                        if home_team_id and home_club_id and home_team_id not in teams_by_id:
                            teams_by_id[str(home_team_id)] = TeamIn(
                                source_id=str(home_team_id),
                                club_source_id=str(home_club_id),
                                name=str(home_team_id),
                            )
                        if away_team_id and away_club_id and away_team_id not in teams_by_id:
                            teams_by_id[str(away_team_id)] = TeamIn(
                                source_id=str(away_team_id),
                                club_source_id=str(away_club_id),
                                name=str(away_team_id),
                            )

                        # Match basics
                        fixture_date = fx.get("fixtureDate")
                        if not fixture_date:
                            continue

                        match = MatchIn(
                            source_id=fixture_id,
                            season_source_id=season_id,
                            competition_source_id=comp_id,
                            home_team_source_id=str(home_team_id),
                            away_team_source_id=str(away_team_id),
                            venue_source_id=str(venue_id) if venue_id else None,
                            starts_at=_parse_iso_datetime(fixture_date),
                            status=str(fx.get("statusCode") or fx.get("status") or ""),
                            home_score=fx.get("homeTeamScoreAsInt"),
                            away_score=fx.get("awayTeamScoreAsInt"),
                        )

                        # Optionally enrich via fixture detail (events/officials/players).
                        if settings.eh_fetch_fixture_details:
                            detail = client.get_json(f"/fixtures/{fixture_id}")
                            if isinstance(detail, dict):
                                parse_fixture_detail(
                                    detail, fixture_id, home_club_id, away_club_id, players_by_id, match
                                )

                        matches_by_id[fixture_id] = match

            return DatasetIn(
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
