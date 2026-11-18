"""Shootout score on matches - knockout ties (Finals Weekend, EHL) can finish
level and be decided on a shootout; the drawn score stays in home/away_score."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("matches", sa.Column("home_shootout", sa.Integer(), nullable=True))
    op.add_column("matches", sa.Column("away_shootout", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("matches", "away_shootout")
    op.drop_column("matches", "home_shootout")
