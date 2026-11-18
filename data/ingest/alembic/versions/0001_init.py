from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    mysql_table_args = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}

    op.create_table(
        "seasons",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("source", "source_id", name="uq_seasons_source_source_id"),
        **mysql_table_args,
    )

    op.create_table(
        "competitions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "source", "source_id", name="uq_competitions_source_source_id"
        ),
        **mysql_table_args,
    )

    op.create_table(
        "clubs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("source", "source_id", name="uq_clubs_source_source_id"),
        **mysql_table_args,
    )

    op.create_table(
        "venues",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("city", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("source", "source_id", name="uq_venues_source_source_id"),
        **mysql_table_args,
    )

    op.create_table(
        "teams",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("club_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], name="fk_teams_club_id"),
        sa.UniqueConstraint("source", "source_id", name="uq_teams_source_source_id"),
        sa.Index("ix_teams_club_id", "club_id"),
        **mysql_table_args,
    )

    op.create_table(
        "players",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("club_id", sa.BigInteger(), nullable=False),
        sa.Column("first_name", sa.String(length=255), nullable=False),
        sa.Column("last_name", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["club_id"], ["clubs.id"], name="fk_players_club_id"),
        sa.UniqueConstraint("source", "source_id", name="uq_players_source_source_id"),
        sa.Index("ix_players_club_id", "club_id"),
        **mysql_table_args,
    )

    op.create_table(
        "matches",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("season_id", sa.BigInteger(), nullable=False),
        sa.Column("competition_id", sa.BigInteger(), nullable=False),
        sa.Column("home_team_id", sa.BigInteger(), nullable=False),
        sa.Column("away_team_id", sa.BigInteger(), nullable=False),
        sa.Column("venue_id", sa.BigInteger(), nullable=True),
        sa.Column("starts_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("home_score", sa.Integer(), nullable=True),
        sa.Column("away_score", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["season_id"], ["seasons.id"], name="fk_matches_season_id"),
        sa.ForeignKeyConstraint(
            ["competition_id"], ["competitions.id"], name="fk_matches_competition_id"
        ),
        sa.ForeignKeyConstraint(
            ["home_team_id"], ["teams.id"], name="fk_matches_home_team_id"
        ),
        sa.ForeignKeyConstraint(
            ["away_team_id"], ["teams.id"], name="fk_matches_away_team_id"
        ),
        sa.ForeignKeyConstraint(["venue_id"], ["venues.id"], name="fk_matches_venue_id"),
        sa.UniqueConstraint("source", "source_id", name="uq_matches_source_source_id"),
        sa.Index("ix_matches_season_id", "season_id"),
        sa.Index("ix_matches_competition_id", "competition_id"),
        sa.Index("ix_matches_home_team_id", "home_team_id"),
        sa.Index("ix_matches_away_team_id", "away_team_id"),
        sa.Index("ix_matches_venue_id", "venue_id"),
        **mysql_table_args,
    )

    op.create_table(
        "match_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("match_id", sa.BigInteger(), nullable=False),
        sa.Column("team_id", sa.BigInteger(), nullable=False),
        sa.Column("player_id", sa.BigInteger(), nullable=True),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("period", sa.Integer(), nullable=True),
        sa.Column("minute", sa.Integer(), nullable=True),
        sa.Column("card", sa.String(length=16), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["match_id"], ["matches.id"], name="fk_match_events_match_id"
        ),
        sa.ForeignKeyConstraint(
            ["team_id"], ["teams.id"], name="fk_match_events_team_id"
        ),
        sa.ForeignKeyConstraint(
            ["player_id"], ["players.id"], name="fk_match_events_player_id"
        ),
        sa.UniqueConstraint(
            "source", "source_id", name="uq_match_events_source_source_id"
        ),
        sa.Index("ix_match_events_match_id", "match_id"),
        sa.Index("ix_match_events_team_id", "team_id"),
        sa.Index("ix_match_events_player_id", "player_id"),
        **mysql_table_args,
    )

    op.create_table(
        "match_officials",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("match_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["match_id"], ["matches.id"], name="fk_match_officials_match_id"
        ),
        sa.UniqueConstraint(
            "source", "source_id", name="uq_match_officials_source_source_id"
        ),
        sa.Index("ix_match_officials_match_id", "match_id"),
        **mysql_table_args,
    )


def downgrade() -> None:
    op.drop_table("match_officials")
    op.drop_table("match_events")
    op.drop_table("matches")
    op.drop_table("players")
    op.drop_table("teams")
    op.drop_table("venues")
    op.drop_table("clubs")
    op.drop_table("competitions")
    op.drop_table("seasons")

