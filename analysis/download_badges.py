"""
Download every club badge (clubs.logo_url) into assets/badges/<club-slug>.<ext>.

Run inside the jupyter container (it has the DB driver and sits on the same
Docker network as `db`):

    docker compose exec jupyter python3 /home/jovyan/work/download_badges.py

Files land in assets/badges/ on the host via the bind mount. Re-running only
fetches badges that are missing locally, so it's safe to run after each
`ingest backfill-club-logos` / new-season import.

assets/badges/ is gitignored - the URLs in the DB are the source of truth
(assets/badges-hq/, the hand-prepared set, IS tracked)
and the images are regenerable in seconds.
"""
import os
import re
import urllib.request
from pathlib import Path

from sqlalchemy import create_engine, text

DATABASE_URL = os.getenv(
    "DATABASE_URL", "mysql+pymysql://hockey_stats:hockey_stats@db:3306/hockey_stats?charset=utf8mb4"
)
OUT_DIR = Path(__file__).parent.parent / "assets" / "badges"


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT name, logo_url FROM clubs WHERE logo_url IS NOT NULL ORDER BY name")
        ).fetchall()

    fetched = skipped = failed = 0
    for name, url in rows:
        ext = Path(url.split("?")[0]).suffix.lower() or ".png"
        dest = OUT_DIR / f"{slugify(name)}{ext}"
        if dest.exists():
            skipped += 1
            continue
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                dest.write_bytes(resp.read())
            fetched += 1
        except Exception as exc:  # noqa: BLE001 - report and keep going
            failed += 1
            print(f"FAILED {name}: {exc}")

    print(f"fetched {fetched}, already had {skipped}, failed {failed} -> {OUT_DIR}")


if __name__ == "__main__":
    main()
