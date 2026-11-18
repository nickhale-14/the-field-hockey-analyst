from __future__ import annotations

import logging
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler

from ingest.db import get_engine, get_sessionmaker
from ingest.loaders.mysql import load_dataset
from ingest.migrations import upgrade_head
from ingest.settings import Settings
from ingest.sources.eh_api import EhApiClient, EhApiSource
from ingest.sources.eh_api_fixtures import EhApiFixturesSource

app = typer.Typer(no_args_is_help=True)
console = Console()


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, console=console)],
    )


def _require_database_url(settings: Settings) -> str:
    if not settings.database_url:
        raise typer.BadParameter("DATABASE_URL is required (set it in .env)")
    return settings.database_url


@app.command()
def validate() -> None:
    """
    Validate that the configured ingestion source can be accessed/parsed.
    """
    settings = Settings()
    _setup_logging(settings.log_level)

    if settings.ingest_source == "eh_api":
        client = EhApiClient(settings)
        try:
            seasons = client.get_json("/seasons")
        finally:
            client.close()
        console.print("[green]OK[/green] EH API reachable")
        console.print({"seasons": len(seasons) if isinstance(seasons, list) else "?"})
        return

    raise typer.BadParameter(f"Unknown INGEST_SOURCE={settings.ingest_source}")


@app.command()
def migrate() -> None:
    """
    Apply database migrations (Alembic upgrade head).
    """
    settings = Settings()
    _setup_logging(settings.log_level)
    database_url = _require_database_url(settings)

    upgrade_head(database_url)
    console.print("[green]OK[/green] Migrations applied")


@app.command()
def run(
    source: Annotated[
        str | None, typer.Option(help="Override INGEST_SOURCE (eh_api)")
    ] = None
) -> None:
    """
    Run ingestion (idempotent upserts).
    """
    settings = Settings()
    _setup_logging(settings.log_level)
    database_url = _require_database_url(settings)

    if source is not None and source != settings.ingest_source:
        settings.ingest_source = source  # type: ignore[assignment]

    if settings.ingest_source == "eh_api":
        dataset = EhApiSource(settings).load()
    else:
        raise typer.BadParameter(f"Unknown INGEST_SOURCE={settings.ingest_source}")

    engine = get_engine(settings)
    SessionLocal = get_sessionmaker(engine)
    with SessionLocal() as session:
        counts = load_dataset(session, dataset)
        session.commit()

    console.print("[green]OK[/green] Ingestion complete")
    console.print(counts)


@app.command(name="import-fixtures")
def import_fixtures(
    details: Annotated[
        bool,
        typer.Option(help="Fetch goals/cards/squads for played matches (one API call each)."),
    ] = True,
    refetch_all: Annotated[
        bool,
        typer.Option(
            "--refetch-all",
            help="Re-fetch detail for every played match, not just those with no events yet.",
        ),
    ] = False,
) -> None:
    """
    The weekly live-season update (EH_LIVE_SEASON_ID): refresh the full fixture
    list (scores and statuses for anything played since last time) and pull
    goals/cards/squads for played matches that don't have events yet. Safe to
    re-run; everything is upserted. Follow with `ingest repair`.
    """
    from sqlalchemy import exists, select

    from ingest.models import Match, MatchEvent

    settings = Settings()
    _setup_logging(settings.log_level)
    _require_database_url(settings)

    engine = get_engine(settings)
    SessionLocal = get_sessionmaker(engine)

    # Played matches we already hold events for: skip their detail call so a
    # weekly run costs ~one request per newly played match, not per match.
    with SessionLocal() as session:
        have_events = set(
            session.scalars(
                select(Match.source_id).where(
                    Match.source == "england_hockey",
                    exists().where(MatchEvent.match_id == Match.id),
                )
            )
        )

    source = EhApiFixturesSource(
        settings, fetch_details=details, skip_detail_for=have_events, refetch_all=refetch_all
    )
    dataset = source.load()

    with SessionLocal() as session:
        counts = load_dataset(session, dataset)
        session.commit()

    console.print("[green]OK[/green] Fixture import complete")
    console.print({**counts, "fixture_detail_fetches": source.detail_fetches})


@app.command(name="verify-flashscore")
def verify_flashscore_cmd() -> None:
    """
    Check the hand-transcribed 2019/20 and 2020/21 seasons against England
    Hockey's own Altius site - an independent second source for the same
    matches. Writes nothing. (2021/22 isn't on Altius and stays unverified.)
    """
    from ingest.verify_flashscore import verify

    settings = Settings()
    _setup_logging(settings.log_level)
    _require_database_url(settings)

    SessionLocal = get_sessionmaker(get_engine(settings))
    with SessionLocal() as session:
        console.print(verify(session), markup=False)


@app.command(name="reconcile")
def reconcile(
    season: Annotated[str, typer.Option(help="Season name to reconcile, e.g. '2025/26'.")],
) -> None:
    """
    Re-walk a season's whole fixture list at the England Hockey API and compare
    it against what we hold: missing matches, extra matches, and any score or
    status that has changed since we ingested. Writes nothing.

    This is the check that our data still *matches the source* - `audit` only
    checks that our data is consistent with itself.
    """
    from sqlalchemy import text as sql_text

    settings = Settings()
    _setup_logging(settings.log_level)
    _require_database_url(settings)

    SessionLocal = get_sessionmaker(get_engine(settings))
    with SessionLocal() as session:
        row = session.execute(
            sql_text("SELECT source_id FROM seasons WHERE name = :n"), {"n": season}
        ).first()
        if not row:
            raise typer.BadParameter(f"No season called {season!r} in the database")
        ours = {
            r[0]: r
            for r in session.execute(
                sql_text(
                    """
                    SELECT m.source_id, m.status, m.home_score, m.away_score,
                           th.name, ta.name, m.starts_at
                    FROM matches m
                    JOIN seasons s ON s.id = m.season_id
                    JOIN teams th ON th.id = m.home_team_id
                    JOIN teams ta ON ta.id = m.away_team_id
                    WHERE s.name = :n AND m.source_id NOT LIKE 'altius:%'
                    """
                ),
                {"n": season},
            ).all()
        }

    # Reuse the proven live-season walk by pointing it at this season.
    settings.eh_live_season_id = row[0]
    console.print(f"Walking the API's fixture list for {season} (this takes a few minutes)...")
    dataset = EhApiFixturesSource(settings, fetch_details=False).load()

    theirs = {m.source_id: m for m in dataset.matches}
    missing = [sid for sid in theirs if sid not in ours]
    extra = [sid for sid in ours if sid not in theirs]
    score_diffs, status_diffs = [], []
    for sid, m in theirs.items():
        if sid not in ours:
            continue
        _, status, hs, as_, home, away, _ = ours[sid]
        if (m.home_score, m.away_score) != (hs, as_):
            score_diffs.append(f"{home} v {away}: ours {hs}-{as_}, API {m.home_score}-{m.away_score}")
        if m.status != status:
            status_diffs.append(f"{home} v {away}: ours '{status}', API '{m.status}'")

    console.print(f"\n[bold]{season} reconciliation[/bold]")
    console.print(f"  API fixtures: {len(theirs)}   ours: {len(ours)}")
    for label, items in (
        ("in the API but not in our DB", missing),
        ("in our DB but no longer in the API", extra),
        ("score differs", score_diffs),
        ("status differs", status_diffs),
    ):
        console.print(f"  {label}: {len(items)}", markup=False)
        for x in items[:10]:
            console.print(f"      {x}", markup=False)
        if len(items) > 10:
            console.print(f"      ... and {len(items) - 10} more")
    if not (missing or extra or score_diffs or status_diffs):
        console.print("  [green]clean - every fixture, score and status matches the source[/green]")


@app.command(name="audit")
def audit_cmd(
    out: Annotated[str | None, typer.Option(help="Also write the report to this path.")] = None,
) -> None:
    """
    Run every integrity check over the data already loaded and print a report.
    Writes nothing. See `ingest backfill-events` for the one class of problem
    that can be fixed automatically.
    """
    from ingest.audit import format_report, run_all

    settings = Settings()
    _setup_logging(settings.log_level)
    _require_database_url(settings)

    SessionLocal = get_sessionmaker(get_engine(settings))
    with SessionLocal() as session:
        report = format_report(run_all(session))

    console.print(report, markup=False)
    if out:
        with open(out, "w") as fh:
            fh.write(report + "\n")
        console.print(f"\nWritten to {out}")


@app.command(name="backfill-events")
def backfill_events(
    season: Annotated[str, typer.Option(help="Season name to scan, e.g. '2025/26'.")],
    all_matches: Annotated[bool, typer.Option(
        "--all", help="Check every played match, not just those whose goals don't add up "
        "(catches cards and re-attributed goals logged after we imported).")] = False,
    days: Annotated[int | None, typer.Option(
        help="Only matches played in the last N days (keeps the weekly --all run cheap).")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Report what the API has, write nothing.")] = False,
) -> None:
    """
    Re-fetch fixture detail for PLAYED matches and bring their events in line
    with the API.

    By default only matches whose stored goal events don't add up to the
    scoreline are checked: clubs log goals days after we ingested. With --all,
    every played match is checked, because a club can also add cards, or change
    who scored, after the goals already added up (Cambridge City W1 v
    Birmingham 2s, 17 Oct 2026: four cards logged late).

    A match whose event set differs from the API's has its events REPLACED
    wholesale, so a re-attributed goal can't leave the old row behind. Known-bad
    events (`corrections.EVENT_REMOVALS`) are removed again in the same
    transaction. A replay with an abandoned twin keeps its existing events
    (`ingest repair` may have moved some there from the abandoned record) and is
    only upserted. Only matches the API actually has events for are touched,
    so a match the club never logged (most of 2023/24) is a no-op.
    """
    import time
    from collections import defaultdict

    import httpx
    from sqlalchemy import bindparam, delete
    from sqlalchemy import text as sql_text

    from ingest.corrections import EVENT_REMOVALS
    from ingest.maintenance import remove_known_bad_events
    from ingest.models import MatchEvent
    from ingest.schemas import DatasetIn, MatchIn, SeasonIn
    from ingest.sources.eh_api import parse_fixture_detail

    settings = Settings()
    _setup_logging(settings.log_level)
    _require_database_url(settings)

    goals = ("FG", "PC", "PS")
    SessionLocal = get_sessionmaker(get_engine(settings))
    with SessionLocal() as session:
        rows = session.execute(
            sql_text(
                f"""
                SELECT m.id, m.source_id, s.source_id AS season_sid, s.name AS season_name,
                       c.source_id AS comp_sid, ht.source_id AS home_sid, at.source_id AS away_sid,
                       v.source_id AS venue_sid, m.starts_at, m.status, m.home_score, m.away_score,
                       ht.name AS home, at.name AS away,
                       (SELECT COUNT(*) FROM match_events e
                         WHERE e.match_id = m.id AND e.event_type IN ('FG','PC','PS')) AS goal_events,
                       EXISTS (SELECT 1 FROM matches a
                         WHERE a.status = 'A' AND a.season_id = m.season_id
                           AND a.home_team_id = m.home_team_id
                           AND a.away_team_id = m.away_team_id) AS has_abandoned_twin
                FROM matches m
                JOIN seasons s ON s.id = m.season_id
                JOIN competitions c ON c.id = m.competition_id
                JOIN teams ht ON ht.id = m.home_team_id
                JOIN teams at ON at.id = m.away_team_id
                LEFT JOIN venues v ON v.id = m.venue_id
                WHERE s.name = :season AND m.status = 'R' AND m.source_id NOT LIKE 'altius:%'
                  AND (:days IS NULL OR m.starts_at >= UTC_TIMESTAMP() - INTERVAL :days DAY)
                {"" if all_matches else "HAVING goal_events <> m.home_score + m.away_score"}
                ORDER BY m.starts_at
                """
            ),
            {"season": season, "days": days},
        ).mappings().all()

        stored: dict[int, set[str]] = defaultdict(set)  # match id -> its event source_ids
        stored_types: dict[str, str] = {}
        if rows:
            for mid, sid, etype in session.execute(
                sql_text(
                    "SELECT match_id, source_id, event_type FROM match_events "
                    "WHERE source = 'england_hockey' AND match_id IN :ids"
                ).bindparams(bindparam("ids", expanding=True)),
                {"ids": [r["id"] for r in rows]},
            ):
                stored[mid].add(sid)
                stored_types[sid] = etype

    scope = "played match" if all_matches else "match with missing goal events"
    window = f" from the last {days} days" if days else ""
    if not rows:
        console.print(f"[green]OK[/green] {season}: no {scope}{window} to check")
        return

    console.print(f"{len(rows)} {scope}(es) in {season}{window}; checking the API")
    client = EhApiClient(settings)
    matches: list[MatchIn] = []
    replace_ids: list[int] = []
    players: dict = {}
    unchanged = 0

    def tally(types) -> str:
        t = list(types)
        return f"{sum(x in goals for x in t)}g {sum(x not in goals for x in t)}c"

    try:
        for r in rows:
            label = f"{r['home']} {r['home_score']}-{r['away_score']} {r['away']}"
            for attempt in (1, 2, 3):  # the client retries bad statuses, not timeouts
                try:
                    detail = client.get_json(f"/fixtures/{r['source_id']}")
                    break
                except httpx.TransportError:
                    if attempt == 3:
                        raise
                    time.sleep(2 * attempt)
            n_events = len(detail.get("fixtureEvents") or []) if isinstance(detail, dict) else 0
            if not n_events:
                if not all_matches:
                    console.print(f"  [yellow]none upstream[/yellow]  {label}", markup=True)
                continue
            match = MatchIn(
                source_id=r["source_id"],
                season_source_id=r["season_sid"],
                competition_source_id=r["comp_sid"],
                home_team_source_id=r["home_sid"],
                away_team_source_id=r["away_sid"],
                venue_source_id=r["venue_sid"],
                starts_at=r["starts_at"],
                status=r["status"],
                home_score=r["home_score"],
                away_score=r["away_score"],
            )
            parse_fixture_detail(detail, r["source_id"], None, None, players, match)
            upstream = {e.source_id: e.type for e in match.events if e.source_id not in EVENT_REMOVALS}
            have = stored[r["id"]]
            if set(upstream) == have:
                unchanged += 1
                continue
            matches.append(match)
            note = ""
            if r["has_abandoned_twin"]:
                note = "  (abandoned twin: upsert only)"
            else:
                replace_ids.append(r["id"])
            console.print(
                f"  [green]{tally(stored_types[s] for s in have)} -> {tally(upstream.values())}[/green]"
                f"  {r['starts_at']:%d %b}  {label}{note}",
                markup=True,
            )
    finally:
        client.close()

    if all_matches:
        console.print(f"{unchanged} already match the API")
    if not matches:
        console.print("Nothing to write.")
        return
    if dry_run:
        console.print(f"[yellow]dry run[/yellow] - would write {len(matches)} match(es)")
        return

    dataset = DatasetIn(
        source="england_hockey",
        season=SeasonIn(source_id=rows[0]["season_sid"], name=rows[0]["season_name"]),
        players=list(players.values()),
        matches=matches,
    )
    with SessionLocal() as session:
        replaced = 0
        if replace_ids:
            replaced = session.execute(
                delete(MatchEvent).where(
                    MatchEvent.source == "england_hockey", MatchEvent.match_id.in_(replace_ids)
                )
            ).rowcount
        counts = load_dataset(session, dataset)
        counts.update(remove_known_bad_events(session))
        counts["stale_events_deleted_first"] = replaced
        session.commit()
    console.print(f"[green]OK[/green] Backfill complete - {len(matches)} match(es) rewritten; run `ingest repair` next")
    console.print(counts)


@app.command(name="backfill-squads")
def backfill_squads(
    season: Annotated[str, typer.Option(help="Season name to scan, e.g. '2025/26'.")],
    competition: Annotated[
        str | None, typer.Option(help="Only competitions whose name contains this, e.g. 'Premier Division'.")
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Report what the API has, write nothing.")] = False,
    pause: Annotated[float, typer.Option(help="Seconds to wait between API calls - it isn't our API.")] = 1.0,
) -> None:
    """
    Fetch matchday squads for PLAYED matches that have none stored yet.

    The importers always read the squad lists (to create players) but until
    2026-10-17 didn't keep who was in which match. Squads exist upstream from
    2023/24; 2022/23 has none. Writes squads only - events and officials are
    left exactly as they are, and players already in the DB are not touched
    (so a re-fetch can't move anyone's club or undo a repair). Commits every
    100 matches and only picks matches with no squad rows, so it resumes
    where it stopped. One API call per match, `--pause` seconds apart. Do the
    Premier Division first (`--competition "Premier Division"`); the lower
    tiers are ~1,100 calls a season.
    """
    import time

    from sqlalchemy import text as sql_text

    from ingest.schemas import DatasetIn, MatchIn, SeasonIn
    from ingest.sources.eh_api import parse_fixture_detail

    settings = Settings()
    _setup_logging(settings.log_level)
    _require_database_url(settings)

    SessionLocal = get_sessionmaker(get_engine(settings))
    with SessionLocal() as session:
        rows = session.execute(
            sql_text(
                """
                SELECT m.source_id, s.source_id AS season_sid, s.name AS season_name,
                       c.source_id AS comp_sid, ht.source_id AS home_sid, at.source_id AS away_sid,
                       v.source_id AS venue_sid, m.starts_at, m.status, m.home_score, m.away_score,
                       m.home_shootout, m.away_shootout
                FROM matches m
                JOIN seasons s ON s.id = m.season_id
                JOIN competitions c ON c.id = m.competition_id
                JOIN teams ht ON ht.id = m.home_team_id
                JOIN teams at ON at.id = m.away_team_id
                LEFT JOIN venues v ON v.id = m.venue_id
                WHERE s.name = :season AND m.status = 'R' AND m.source = 'england_hockey'
                  AND m.source_id NOT LIKE 'altius:%' AND m.source_id NOT LIKE 'flashscore:%'
                  AND c.name LIKE :comp
                  AND NOT EXISTS (SELECT 1 FROM match_players mp WHERE mp.match_id = m.id)
                ORDER BY m.starts_at
                """
            ),
            {"season": season, "comp": f"%{competition or ''}%"},
        ).mappings().all()
        known_players = {
            sid for (sid,) in session.execute(
                sql_text("SELECT source_id FROM players WHERE source = 'england_hockey'")
            )
        }

    if not rows:
        console.print(f"[green]OK[/green] {season}: every played match already has a squad (or none are API matches)")
        return

    console.print(f"{len(rows)} played match(es) in {season} without a squad; fetching from the API")
    client = EhApiClient(settings)
    totals = {"fetched": 0, "with_squad": 0, "empty": 0, "squad_rows": 0, "new_players": 0}
    pending: list[MatchIn] = []
    pending_players: dict = {}

    def flush() -> None:
        if not pending or dry_run:
            pending.clear()
            pending_players.clear()
            return
        dataset = DatasetIn(
            source="england_hockey",
            season=SeasonIn(source_id=rows[0]["season_sid"], name=rows[0]["season_name"]),
            players=[p for sid, p in pending_players.items() if sid not in known_players],
            matches=list(pending),
        )
        totals["new_players"] += len(dataset.players)
        known_players.update(p.source_id for p in dataset.players)
        with SessionLocal() as session:
            load_dataset(session, dataset)
            session.commit()
        pending.clear()
        pending_players.clear()

    try:
        for i, r in enumerate(rows, 1):
            if i > 1:
                time.sleep(pause)
            detail = client.get_json(f"/fixtures/{r['source_id']}")
            totals["fetched"] += 1
            match = MatchIn(
                source_id=r["source_id"],
                season_source_id=r["season_sid"],
                competition_source_id=r["comp_sid"],
                home_team_source_id=r["home_sid"],
                away_team_source_id=r["away_sid"],
                venue_source_id=r["venue_sid"],
                starts_at=r["starts_at"],
                status=r["status"],
                home_score=r["home_score"],
                away_score=r["away_score"],
                home_shootout=r["home_shootout"],
                away_shootout=r["away_shootout"],
            )
            if isinstance(detail, dict):
                parse_fixture_detail(detail, r["source_id"], None, None, pending_players, match)
            match.events, match.officials = [], []  # squads only
            if not match.squad:
                totals["empty"] += 1
                continue
            totals["with_squad"] += 1
            totals["squad_rows"] += len(match.squad)
            pending.append(match)
            if len(pending) >= 100:
                flush()
                console.print(f"  {i}/{len(rows)} fetched, {totals['with_squad']} with squads")
    finally:
        flush()
        client.close()

    verb = "would write" if dry_run else "wrote"
    console.print(
        f"[green]OK[/green] {season}: {totals['fetched']} fetched, {totals['with_squad']} with squads "
        f"({verb} {totals['squad_rows']} squad rows, {totals['new_players']} new players), "
        f"{totals['empty']} with none upstream"
    )


@app.command(name="import-finals")
def import_finals(
    competition: Annotated[
        list[int] | None,
        typer.Option(help="Altius competition id(s) to import; default = every known Finals Weekend"),
    ] = None,
    dry_run: Annotated[bool, typer.Option(help="Scrape and resolve, report, but write nothing")] = False,
) -> None:
    """
    Import the Premier Division Finals Weekend (semis, 3rd/4th, final) from
    englandhockey.altiusrt.com - the league API never carried these
    completely. Goals, cards, shootouts and officials included; players are
    matched onto existing league players by club + name, and any that can't
    be are created and listed so they can be aliased by hand. Safe to re-run.
    Also removes the league API's partial 2023/2024 copies so nothing is
    double-counted.
    """
    from ingest.maintenance import dedupe_players, remove_league_api_finals
    from ingest.sources.eh_altius import (
        KNOWN_FINALS_COMPETITIONS,
        AltiusClient,
        EhAltiusFinalsSource,
        build_resolver,
    )

    settings = Settings()
    _setup_logging(settings.log_level)
    _require_database_url(settings)
    ids = competition or list(KNOWN_FINALS_COMPETITIONS)

    SessionLocal = get_sessionmaker(get_engine(settings))
    client = AltiusClient()
    try:
        with SessionLocal() as session:
            source = EhAltiusFinalsSource(client, build_resolver(session), ids)
            console.print(f"Scraping {len(ids)} competition(s) from {client._client.base_url} ...")
            datasets = source.load_all()
            report = source.report

            for line in report.competitions:
                console.print(f"  {line}")
            console.print(
                f"matches: {report.matches}  players matched: {report.players_matched}  "
                f"players created: {len(report.players_created)}"
            )
            for line in report.players_created:
                console.print(f"  [yellow]new player[/yellow] {line}")
            if report.weak_matches:
                console.print(
                    f"{len(report.weak_matches)} match(es) made without 'has scored for this team' "
                    "evidence - worth a glance:"
                )
                for key, val in sorted(report.weak_matches.items()):
                    console.print(f"  check {key} {val}", markup=False, highlight=False)
            for line in report.problems:
                console.print(f"  [red]problem[/red] {line}")

            if dry_run:
                console.print("[yellow]Dry run - nothing written[/yellow]")
                return
            if report.problems:
                raise typer.Exit(code=1)

            removed = remove_league_api_finals(session)
            totals: dict[str, int] = {}
            for ds in datasets:
                for k, v in load_dataset(session, ds).items():
                    totals[k] = totals.get(k, 0) + v
            deduped = dedupe_players(session)
            session.commit()
    finally:
        client.close()

    console.print("[green]OK[/green] Finals Weekend import complete")
    console.print({**totals, **removed, **deduped})


@app.command(name="import-bundesliga")
def import_bundesliga(
    season: Annotated[
        list[str] | None,
        typer.Option(help="Season(s) like 2024/25; default = every known season"),
    ] = None,
    gender: Annotated[
        list[str] | None, typer.Option(help="M and/or W; default = both")
    ] = None,
    incremental: Annotated[
        bool, typer.Option(help="Skip matches already loaded as played (the weekly update)")
    ] = False,
    cache_dir: Annotated[
        str, typer.Option(help="Where finished-match pages are cached; '' to disable")
    ] = "/app/cache/hockeybundesliga",
    dry_run: Annotated[bool, typer.Option(help="Scrape and parse, report, but write nothing")] = False,
) -> None:
    """
    Import the German 1. Bundesliga (field, men + women) from hockeybundesliga.de:
    fixtures, scores, shootouts, goals (typed where the ticker says), cards,
    squads and umpires. Writes to its OWN database - point DATABASE_URL at
    hockey_stats_de, never the England one (the English analysis code reads
    every played match with no source filter). Refuses to run against a
    database that holds England Hockey data. Safe to re-run.
    """
    from pathlib import Path

    from sqlalchemy import text as sql_text

    from ingest.sources.hockeybundesliga import SEASONS, HblClient, HockeyBundesligaSource

    settings = Settings()
    _setup_logging(settings.log_level)
    _require_database_url(settings)
    seasons = season or list(SEASONS)
    genders = gender or ["M", "W"]
    unknown = [s for s in seasons if s not in SEASONS] + [g for g in genders if g not in ("M", "W")]
    if unknown:
        raise typer.BadParameter(f"unknown season/gender: {unknown}; seasons are {list(SEASONS)}")

    SessionLocal = get_sessionmaker(get_engine(settings))
    with SessionLocal() as session:
        english = session.execute(
            sql_text("SELECT COUNT(*) FROM matches WHERE source = 'england_hockey'")
        ).scalar_one()
        if english:
            console.print(
                f"[red]Refusing:[/red] this database has {english} England Hockey matches. "
                "German data goes in hockey_stats_de (set DATABASE_URL)."
            )
            raise typer.Exit(code=1)
        skip = (
            set(session.execute(
                sql_text("SELECT source_id FROM matches WHERE source = 'hockeybundesliga' AND status = 'R'")
            ).scalars())
            if incremental
            else set()
        )

    client = HblClient(cache_dir=Path(cache_dir) if cache_dir else None)
    try:
        source = HockeyBundesligaSource(client, seasons, genders, log=console.print, skip_played=skip)
        console.print(f"Scraping {', '.join(seasons)} ({'/'.join(genders)}) from {client._client.base_url} ...")
        datasets = source.load_all()
    finally:
        client.close()
    report = source.report

    console.print(
        f"matches: {report.matches}  played (fetched): {report.played}  already loaded: {report.skipped}  "
        f"goals with no type: {report.goals_untyped}  requests: {client.requests}  cache hits: {client.cache_hits}"
    )
    for line in report.warnings:
        console.print(f"  [yellow]warning[/yellow] {line}", markup=True, highlight=False)
    for line in report.problems:
        console.print(f"  [red]problem[/red] {line}", markup=True, highlight=False)
    if dry_run:
        console.print("[yellow]Dry run - nothing written[/yellow]")
        return
    if report.problems:
        raise typer.Exit(code=1)

    totals: dict[str, int] = {}
    with SessionLocal() as session:
        for ds in datasets:
            for k, v in load_dataset(session, ds).items():
                totals[k] = totals.get(k, 0) + v
        session.commit()
    console.print("[green]OK[/green] Bundesliga import complete")
    console.print(totals)


@app.command(name="import-flashscore")
def import_flashscore(
    raw_dir: Annotated[str, typer.Option(help="Folder of saved Flashscore page texts")] = "/app/data/flashscore/raw",
    dry_run: Annotated[bool, typer.Option(help="Parse and resolve, report, but write nothing")] = False,
) -> None:
    """
    Load historic Premier Division results (scores only) transcribed from
    flashscore.com page text - see sources/flashscore_text.py for the file
    format and why it's a paste, not a scrape. Files are named
    <yyyy>-<yy>_<men|women>.txt. Team labels are mapped onto existing league
    teams; teams unknown to the league data are created. Safe to re-run.
    """
    from pathlib import Path

    from ingest.sources.flashscore_text import build_datasets, build_flashscore_resolver

    settings = Settings()
    _setup_logging(settings.log_level)
    _require_database_url(settings)
    files = sorted(Path(raw_dir).glob("*.txt"))
    if not files:
        raise typer.BadParameter(f"no *.txt files in {raw_dir}")

    SessionLocal = get_sessionmaker(get_engine(settings))
    with SessionLocal() as session:
        datasets, report = build_datasets(files, build_flashscore_resolver(session))
        for line in report.files:
            console.print(f"  {line}")
        console.print("team mapping:")
        for key, val in sorted(report.team_map.items()):
            console.print(f"  {key:<32} -> {val}", markup=False, highlight=False)
        for line in report.problems:
            console.print(f"  [red]problem[/red] {line}", markup=False, highlight=False)
        console.print(f"matches: {report.matches}  new teams: {len(report.teams_created)}")

        if dry_run:
            console.print("[yellow]Dry run - nothing written[/yellow]")
            return
        if report.problems:
            raise typer.Exit(code=1)
        totals: dict[str, int] = {}
        for ds in datasets:
            for k, v in load_dataset(session, ds).items():
                totals[k] = totals.get(k, 0) + v
        session.commit()

    console.print("[green]OK[/green] Flashscore import complete")
    console.print(totals)


@app.command(name="backfill-club-logos")
def backfill_club_logos(
    refresh: Annotated[
        bool, typer.Option(help="Re-fetch for every club, not just those missing a logo")
    ] = False,
) -> None:
    """
    Fill clubs.logo_url from the API's /clubs/{id} endpoint. Cheap (one call per
    club) - avoids re-running full ingestion just to pick up badge URLs for
    clubs ingested before the column existed.
    """
    import time

    from sqlalchemy import select

    from ingest.models import Club

    settings = Settings()
    _setup_logging(settings.log_level)
    _require_database_url(settings)

    engine = get_engine(settings)
    SessionLocal = get_sessionmaker(engine)
    client = EhApiClient(settings)
    updated = missing = 0
    try:
        with SessionLocal() as session:
            stmt = select(Club)
            if not refresh:
                stmt = stmt.where(Club.logo_url.is_(None))
            clubs = session.execute(stmt).scalars().all()
            console.print(f"Checking {len(clubs)} club(s)...")
            for club in clubs:
                detail = client.get_json(f"/clubs/{club.source_id}")
                url = detail.get("logoUrl") if isinstance(detail, dict) else None
                if url:
                    club.logo_url = url
                    updated += 1
                else:
                    missing += 1
                time.sleep(0.3)
            session.commit()
    finally:
        client.close()

    console.print(f"[green]OK[/green] logo_url set on {updated} club(s); {missing} had none in the API")


@app.command(name="apply-aliases")
def apply_aliases_cmd() -> None:
    """
    Merge already-loaded teams/clubs per aliases.py (same real side under two
    source ids, e.g. a club rename). New ingests apply these automatically;
    this is for rows loaded before the alias existed. Re-runs the player
    dedupe afterwards, since merging clubs can reveal split players.
    """
    from ingest.maintenance import apply_aliases, dedupe_players

    settings = Settings()
    _setup_logging(settings.log_level)
    _require_database_url(settings)

    SessionLocal = get_sessionmaker(get_engine(settings))
    with SessionLocal() as session:
        alias_counts = apply_aliases(session)
        player_counts = dedupe_players(session)
        session.commit()

    console.print("[green]OK[/green] Aliases applied")
    console.print({**alias_counts, **player_counts})


@app.command(name="repair")
def repair_cmd() -> None:
    """
    Run every idempotent data repair in one go: trim padded names, aliases
    (teams/clubs/players), verified score corrections, removal of known-bad
    events, player dedupe, drop the league API's partial finals, move events
    logged against an abandoned fixture onto its replay, and name the unnamed
    squad places that clubs' own team sheets identify. Safe to re-run; run it
    after any re-ingest.
    """
    from ingest.maintenance import (
        apply_aliases,
        apply_score_corrections,
        dedupe_players,
        identify_unnamed_squad_players,
        remove_known_bad_events,
        remove_league_api_finals,
        repair_abandoned_replays,
        trim_names,
    )

    settings = Settings()
    _setup_logging(settings.log_level)
    _require_database_url(settings)

    SessionLocal = get_sessionmaker(get_engine(settings))
    with SessionLocal() as session:
        counts: dict[str, int] = {}
        for step in (trim_names, apply_aliases, apply_score_corrections, remove_known_bad_events,
                     remove_league_api_finals, repair_abandoned_replays,
                     identify_unnamed_squad_players, dedupe_players):
            counts.update(step(session))
        session.commit()

    console.print("[green]OK[/green] Repairs applied")
    console.print(counts)


@app.command(name="dedupe-players")
def dedupe_players_cmd() -> None:
    """
    Merge players split across several rows (same name + same club), keeping
    the lowest id and re-pointing their events. Safe to re-run.
    """
    from ingest.maintenance import dedupe_players

    settings = Settings()
    _setup_logging(settings.log_level)
    _require_database_url(settings)

    SessionLocal = get_sessionmaker(get_engine(settings))
    with SessionLocal() as session:
        counts = dedupe_players(session)
        session.commit()

    console.print("[green]OK[/green] Player dedupe complete")
    console.print(counts)
