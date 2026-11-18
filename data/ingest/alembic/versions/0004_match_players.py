"""Matchday squads: who was in each team's 16 for each match (2023/24 onward
in the API). The importers already stored the people; this stores the link."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "match_players",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(128), nullable=False),
        sa.Column("match_id", sa.BigInteger(), sa.ForeignKey("matches.id"), nullable=False),
        sa.Column("team_id", sa.BigInteger(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("player_id", sa.BigInteger(), sa.ForeignKey("players.id"), nullable=True),
        sa.Column("shirt_number", sa.Integer(), nullable=True),
        sa.Column("is_goalkeeper", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_captain", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("source", "source_id", name="uq_match_players_source_source_id"),
    )
    op.create_index("ix_match_players_source", "match_players", ["source"])
    op.create_index("ix_match_players_match_id", "match_players", ["match_id"])
    op.create_index("ix_match_players_player_id", "match_players", ["player_id"])
    op.create_index("ix_match_players_team_id", "match_players", ["team_id"])


def downgrade() -> None:
    op.drop_table("match_players")
