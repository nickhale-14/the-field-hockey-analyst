from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ingest.settings import Settings


def get_engine(settings: Settings) -> Engine:
    if not settings.database_url:
        raise ValueError("DATABASE_URL is required")
    return create_engine(settings.database_url, pool_pre_ping=True)


def get_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)

