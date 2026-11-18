from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("clubs", sa.Column("logo_url", sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column("clubs", "logo_url")
