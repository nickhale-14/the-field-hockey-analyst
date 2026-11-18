from __future__ import annotations

from alembic import command
from alembic.config import Config


def upgrade_head(database_url: str) -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(cfg, "head")

