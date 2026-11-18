from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field

# The England Hockey API sends some names with trailing whitespace ("Fareham ",
# "Loughborough Students ") and every re-import would put them back after we
# trimmed the rows by hand - so trim at the boundary every source passes through.
Name = Annotated[str, AfterValidator(str.strip)]


class SeasonIn(BaseModel):
    source_id: str
    name: Name


class CompetitionIn(BaseModel):
    source_id: str
    name: Name


class ClubIn(BaseModel):
    source_id: str
    name: Name
    logo_url: str | None = None


class VenueIn(BaseModel):
    source_id: str
    name: Name
    city: Name | None = None


class TeamIn(BaseModel):
    source_id: str
    club_source_id: str
    name: Name


class PlayerIn(BaseModel):
    source_id: str
    club_source_id: str
    first_name: Name
    last_name: Name


class MatchEventIn(BaseModel):
    source_id: str
    # Upstream systems often use short codes (e.g. FG/PC/PS for goals, GC/YC/RC for cards).
    # We store the raw value so we don't lose detail.
    type: str
    team_source_id: str
    player_source_id: str | None = None
    period: int | None = None
    minute: int | None = None
    card: str | None = None


class MatchPlayerIn(BaseModel):
    """A place in a team's matchday squad. player_source_id is None for a
    player who hasn't consented to be named."""

    source_id: str
    team_source_id: str
    player_source_id: str | None = None
    shirt_number: int | None = None
    is_goalkeeper: bool = False
    is_captain: bool = False


class MatchOfficialIn(BaseModel):
    name: Name
    role: str = "Official"


class MatchIn(BaseModel):
    source_id: str
    season_source_id: str
    competition_source_id: str
    home_team_source_id: str
    away_team_source_id: str
    venue_source_id: str | None = None

    starts_at: datetime
    status: str
    home_score: int | None = None
    away_score: int | None = None
    # Shootout score when a knockout tie was level after normal time. The
    # match score stays the drawn score (e.g. 2-2) so goal stats are right;
    # these say who actually went through.
    home_shootout: int | None = None
    away_shootout: int | None = None

    events: list[MatchEventIn] = Field(default_factory=list)
    squad: list[MatchPlayerIn] = Field(default_factory=list)
    officials: list[MatchOfficialIn] = Field(default_factory=list)


class DatasetIn(BaseModel):
    source: str = "mock"
    generated_at: datetime | None = None

    season: SeasonIn
    competitions: list[CompetitionIn] = Field(default_factory=list)
    clubs: list[ClubIn] = Field(default_factory=list)
    venues: list[VenueIn] = Field(default_factory=list)
    teams: list[TeamIn] = Field(default_factory=list)
    players: list[PlayerIn] = Field(default_factory=list)
    matches: list[MatchIn] = Field(default_factory=list)
