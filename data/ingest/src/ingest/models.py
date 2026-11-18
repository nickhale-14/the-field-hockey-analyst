from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class SourceMixin:
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Season(Base, SourceMixin):
    __tablename__ = "seasons"
    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_seasons_source_source_id"),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)


class Competition(Base, SourceMixin):
    __tablename__ = "competitions"
    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_competitions_source_source_id"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)


class Club(Base, SourceMixin):
    __tablename__ = "clubs"
    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_clubs_source_source_id"),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    logo_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    teams: Mapped[list["Team"]] = relationship(back_populates="club")
    players: Mapped[list["Player"]] = relationship(back_populates="club")


class Venue(Base, SourceMixin):
    __tablename__ = "venues"
    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_venues_source_source_id"),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    city: Mapped[str | None] = mapped_column(String(255), nullable=True)


class Team(Base, SourceMixin):
    __tablename__ = "teams"
    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_teams_source_source_id"),)

    club_id: Mapped[int] = mapped_column(ForeignKey("clubs.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    club: Mapped[Club] = relationship(back_populates="teams")


class Player(Base, SourceMixin):
    __tablename__ = "players"
    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_players_source_source_id"),)

    club_id: Mapped[int] = mapped_column(ForeignKey("clubs.id"), nullable=False)
    first_name: Mapped[str] = mapped_column(String(255), nullable=False)
    last_name: Mapped[str] = mapped_column(String(255), nullable=False)

    club: Mapped[Club] = relationship(back_populates="players")


class Match(Base, SourceMixin):
    __tablename__ = "matches"
    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_matches_source_source_id"),)

    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), nullable=False)
    competition_id: Mapped[int] = mapped_column(ForeignKey("competitions.id"), nullable=False)
    home_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False)
    away_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False)
    venue_id: Mapped[int | None] = mapped_column(ForeignKey("venues.id"), nullable=True)

    starts_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    home_score: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    away_score: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    # NULL for league matches; set only when a knockout tie went to a shootout.
    home_shootout: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    away_shootout: Mapped[int | None] = mapped_column(Integer(), nullable=True)


class MatchEvent(Base, SourceMixin):
    __tablename__ = "match_events"
    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_match_events_source_source_id"),
    )

    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), nullable=False)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), nullable=True)

    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    period: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    minute: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    card: Mapped[str | None] = mapped_column(String(16), nullable=True)


class MatchPlayer(Base, SourceMixin):
    """One row per player in a team's matchday squad (the API's `allPlayers`,
    ~16 a side). Rolling subs, so the squad is effectively who played; the
    source never fills its started/played flags. player_id is NULL for a
    player who hasn't consented to be named - they still count in the squad."""

    __tablename__ = "match_players"
    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_match_players_source_source_id"),
    )

    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), nullable=False)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), nullable=True)

    shirt_number: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    is_goalkeeper: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    is_captain: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)


class MatchOfficial(Base, SourceMixin):
    __tablename__ = "match_officials"
    __table_args__ = (
        UniqueConstraint(
            "source", "source_id", name="uq_match_officials_source_source_id"
        ),
    )

    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False)

