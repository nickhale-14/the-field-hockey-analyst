from __future__ import annotations

from sqlalchemy.dialects import mysql
from sqlalchemy.dialects.mysql import insert as mysql_insert

from ingest.models import Club


def test_mysql_upsert_compiles() -> None:
    table = Club.__table__
    stmt = mysql_insert(table).values(
        [{"source": "mock", "source_id": "CL1", "name": "Club"}]
    )
    stmt = stmt.on_duplicate_key_update(name=stmt.inserted.name)
    compiled = str(stmt.compile(dialect=mysql.dialect()))
    assert "ON DUPLICATE KEY UPDATE" in compiled

