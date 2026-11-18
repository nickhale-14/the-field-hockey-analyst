# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2", "pymysql>=1.1", "python-dotenv>=1.0", "starlette"]
# ///
"""Field Hockey Stats MCP server.

Exposes the hockey_stats database to an MCP host (Claude Code, Claude Desktop, ...)
as a small set of tools. Connects as the read-only `hockey_reader` MySQL user
(SELECT only) — see the repo README / .env.example. Run with:

    uv run --directory mcp server.py
"""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pymysql
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# The repo's .env holds MCP_DATABASE_URL (gitignored). Load it from the repo root
# regardless of the working directory the host launched us from.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Sent to the host during the initialize handshake — the model reads this before
# it ever sees a tool. Anything a stranger's Claude needs to know goes here.
INSTRUCTIONS = """\
Statistics for England Hockey national-league club matches (Men's and Women's Premier
Division, Division 1 North/South, Conference North/West/East/Midlands, plus play-offs),
seasons 2022/23 to 2026/27.

Data notes that matter for every query:
- Only matches with status = 'R' (Result) have been played. 'S' = scheduled, 'A' = abandoned,
  'P' = postponed. Always filter status = 'R' for goals, cards, results, or tables.
- match_events.event_type: FG, PC and PS are ALL goals (field goal / penalty-corner goal /
  penalty-stroke goal). GC, YC, RC are green/yellow/red cards.
- Season 2022/23 has scores but NO events (no goal-scorer or card data). 2023/24 has partial
  event coverage. 2024/25 onward is complete. 2026/27 has fixtures only so far.
- Team names are not unique across genders: ~18 names are shared by a club's men's and
  women's sides. Join on teams.id, never group by team name alone.
- Season names look like '2024/25'. Competition names DRIFT between seasons — sponsor
  prefixes come and go ("Vitality Women's Premier Division", "Open - Men's Premier Division")
  and spelling varies ("Womens" vs "Women's"). Never assume a name: call list_competitions
  and use the exact string it returns for that season.
"""


# --- Credential A: who is calling, and what may they see? -------------------------
#
# Static bearer tokens. The caller sends `Authorization: Bearer <token>`; the token
# maps to a role; the role decides which tools exist for this caller. No header at
# all = anonymous viewer (public, curated tools only). A wrong token is rejected
# outright rather than downgraded. On stdio there is no HTTP request: the host
# launched us locally, which is the permission, so stdio callers get admin.

CURATED = {"list_competitions", "find_team", "league_table", "matches", "player_leaders", "match_detail"}
ROLE_TOOLS: dict[str, set[str]] = {
    "viewer": CURATED,
    "analyst": CURATED | {"run_query", "list_tables", "describe_table"},
}
ROLE_TOOLS["admin"] = ROLE_TOOLS["analyst"] | {"audit_log"}

AUDIT_PATH = Path(__file__).resolve().parent / "audit.log"


def _users() -> dict[str, tuple[str, str]]:
    """token -> (user, role). MCP_USERS in the repo .env, one entry per person:
    MCP_USERS="you:admin:<token>;someone:analyst:<token>". The role belongs to the
    user, not the token — swapping to OAuth later only changes where `user` comes from."""
    found = {}
    for entry in os.environ.get("MCP_USERS", "").split(";"):
        entry = entry.strip()
        if not entry:
            continue
        user, role, token = (part.strip() for part in entry.split(":", 2))
        if role not in ROLE_TOOLS:
            raise RuntimeError(f"MCP_USERS: unknown role {role!r} for {user!r}")
        found[token] = (user, role)
    return found


def _identify(ctx) -> tuple[str, str]:
    """The verifier: (user, role) for this request, or reject."""
    if ctx.request is None:  # stdio: launched by the host on this machine
        return "local", "admin"
    header = ctx.request.headers.get("authorization", "")
    if not header:
        return "anonymous", "viewer"
    scheme, _, token = header.partition(" ")
    identity = _users().get(token.strip()) if scheme.lower() == "bearer" else None
    if identity is None:
        raise MCPError(code=-32001, message="Unauthorized: unknown or malformed bearer token.")
    return identity


def _audit(who: str, role: str, method: str, tool: str | None, args, outcome: str, ms: float) -> None:
    line = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "who": who,
        "role": role,
        "method": method,
        "tool": tool,
        "args": args if args is None else json.dumps(args)[:300],
        "outcome": outcome,
        "ms": round(ms, 1),
    }
    with AUDIT_PATH.open("a") as f:
        f.write(json.dumps(line) + "\n")
    print("audit " + json.dumps(line), file=sys.stderr, flush=True)  # stdout is the stdio transport


async def auth_middleware(ctx, call_next):
    who, role = _identify(ctx)
    if ctx.method == "tools/call":
        params = ctx.params or {}
        tool, args = params.get("name"), params.get("arguments")
        if tool not in ROLE_TOOLS[role]:
            _audit(who, role, ctx.method, tool, args, "denied", 0)
            raise MCPError(code=-32001, message=f"Tool {tool!r} is not available to role {role!r}.")
        started = time.perf_counter()
        try:
            result = await call_next(ctx)
        except Exception:
            _audit(who, role, ctx.method, tool, args, "error", (time.perf_counter() - started) * 1000)
            raise
        _audit(who, role, ctx.method, tool, args, "ok", (time.perf_counter() - started) * 1000)
        return result
    result = await call_next(ctx)
    if ctx.method == "tools/list" and result is not None:
        allowed = ROLE_TOOLS[role]
        tools = result["tools"] if isinstance(result, dict) else result.tools
        kept = [t for t in tools if (t["name"] if isinstance(t, dict) else t.name) in allowed]
        if isinstance(result, dict):
            result["tools"] = kept
        else:
            result.tools = kept
    return result


mcp = MCPServer("hockey", instructions=INSTRUCTIONS, middleware=[auth_middleware])
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


def _tls_context() -> ssl.SSLContext:
    """Encrypt the connection without verifying the server certificate.

    Matches the MySQL client's ssl-mode=REQUIRED. For VERIFY_CA you would load the
    provider's CA bundle here instead (RDS publishes one per region) — worth doing if
    the database ever leaves the VPC, but inside a private subnet the traffic never
    crosses a network a stranger can reach.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _settings_from_parts() -> dict | None:
    """Connection settings from separate env vars, or None if they aren't set.

    This is the shape AWS hands you: a Secrets Manager secret for an RDS user holds
    username / password / host / port as separate JSON keys, so ECS can inject each
    one on its own and Secrets Manager can rotate the password without rewriting
    anything else. It carries no database name, so DB_NAME is an ordinary env var.
    """
    host = os.environ.get("DB_HOST")
    user = os.environ.get("DB_USERNAME")
    if not (host and user):
        return None
    return {
        "host": host,
        "port": int(os.environ.get("DB_PORT", "3306")),
        "user": user,
        "password": os.environ.get("DB_PASSWORD", ""),
        "database": os.environ.get("DB_NAME", "hockey_stats"),
        # No ssl-mode to read, so TLS is on unless explicitly disabled. RDS supports it
        # everywhere and the cost is negligible; opting out should take a deliberate act.
        "ssl": None if os.environ.get("DB_SSL", "REQUIRED").upper() == "DISABLED" else _tls_context(),
    }


def _settings_from_url(url: str) -> dict:
    """Connection settings from one URL — how local development and .env do it."""
    u = urlparse(url)
    opts = parse_qs(u.query)
    return {
        "host": u.hostname,
        "port": u.port or 3306,
        "user": u.username,
        "password": u.password or "",
        "database": u.path.lstrip("/"),
        "ssl": None if opts.get("ssl-mode", ["DISABLED"])[0].upper() == "DISABLED" else _tls_context(),
    }


def _connect() -> pymysql.Connection:
    """Open a connection, from either a URL or separate parts.

    MCP_DATABASE_URL wins when set, so a developer's .env always beats anything the
    environment happens to carry. In ECS it is simply absent and the DB_* variables
    are used instead.
    """
    url = os.environ.get("MCP_DATABASE_URL")
    settings = _settings_from_url(url) if url else _settings_from_parts()
    if settings is None:
        raise RuntimeError(
            "No database configuration: set MCP_DATABASE_URL, or DB_HOST/DB_USERNAME "
            "(+ DB_PASSWORD, DB_NAME) from a Secrets Manager secret. See .env.example."
        )
    return pymysql.connect(
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        read_timeout=30,
        connect_timeout=10,
        **settings,
    )


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


# --- Health checks ---------------------------------------------------------------
# Unauthenticated by design (custom_route bypasses auth_middleware) — a load
# balancer sends no bearer token. Two of them, deliberately:


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> Response:
    """Liveness. Point the ALB target group here.

    Deliberately does NOT touch the database. If this checked MySQL, a brief RDS
    blip would fail the health check, ECS would kill and replace a container that
    was working fine, and a small database wobble would become an outage. The load
    balancer's question is only "is this process able to answer?".
    """
    return JSONResponse({"status": "ok", "service": "hockey-mcp"})


@mcp.custom_route("/health/db", methods=["GET"])
async def health_db(request: Request) -> Response:
    """Readiness. For monitoring and for debugging a deploy by hand — NOT for the ALB.

    Answers "can I actually serve a query?", so it catches the things /health can't:
    a wrong MCP_DATABASE_URL, a missing security-group rule, revoked grants.
    Returns 503 so a monitor can alert on it without ECS treating it as fatal.

    Admin bearer token required. custom_route bypasses auth_middleware, so the check
    is done here by hand — the row count would otherwise be public, and it is the
    one thing that distinguishes "pointed at the real database" from "pointed at an
    empty one", which is worth not advertising.
    """
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    identity = _users().get(token.strip()) if scheme.lower() == "bearer" else None
    if identity is None or identity[1] != "admin":
        # 404, not 403: an unauthenticated caller learns nothing, not even that the
        # route exists. /health is the endpoint the world is meant to find.
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    try:
        rows = _rows("SELECT COUNT(*) AS n FROM matches WHERE status = 'R'")
        return JSONResponse({"status": "ok", "database": "up", "played_matches": rows[0]["n"]})
    except Exception as exc:
        # The class name only — never the message, which can carry host and user.
        return JSONResponse({"status": "error", "database": type(exc).__name__}, status_code=503)


@mcp.tool(annotations=READ_ONLY)
def list_tables() -> list[dict]:
    """List every table in the hockey database with its approximate row count.

    Call this first if you are unsure what data exists.
    """
    return _rows(
        "SELECT table_name AS `table`, table_rows AS approx_rows "
        "FROM information_schema.tables "
        "WHERE table_schema = DATABASE() ORDER BY table_name"
    )


@mcp.tool(annotations=READ_ONLY)
def describe_table(table: str) -> list[dict]:
    """Show the columns of one table: name, type, nullability, and key.

    Args:
        table: exact table name, e.g. 'matches' or 'match_events'
    """
    return _rows(
        "SELECT column_name AS `column`, column_type AS `type`, is_nullable AS nullable, "
        "column_key AS `key` "
        "FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = %s "
        "ORDER BY ordinal_position",
        (table,),
    )


MAX_ROWS = 500
_STATEMENT_ALLOWED = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


def _json_safe(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


@mcp.tool(annotations=READ_ONLY)
def run_query(sql: str, limit: int = 200) -> dict:
    """Run a read-only SQL query (MySQL dialect) against the hockey database.

    Use list_tables / describe_table first if you don't know the schema. Rules:
    - SELECT (or WITH ... SELECT) only, one statement. Anything else is rejected —
      and the database user is SELECT-only regardless, so writes cannot succeed.
    - Results are capped at `limit` rows (max 500); the response says if it was cut.
    - Remember: filter matches on status = 'R'; FG/PC/PS event types are all goals;
      2022/23 has no events; join teams on id, not name.

    Args:
        sql: the SELECT statement to run
        limit: max rows to return (default 200, max 500)
    """
    body = sql.strip().rstrip(";").strip()
    if not _STATEMENT_ALLOWED.match(body):
        raise ToolError("Only SELECT / WITH queries are allowed.")
    if ";" in body:
        raise ToolError("One statement per call — remove the extra ';'.")
    limit = max(1, min(int(limit), MAX_ROWS))

    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SET SESSION MAX_EXECUTION_TIME = 10000")  # 10s ceiling per query
            cur.execute(body)
            rows = cur.fetchmany(limit + 1)
    except pymysql.MySQLError as exc:
        # ToolError reaches the model (so it can fix the query); other exceptions are masked.
        code, msg = (exc.args + ("", ""))[:2]
        raise ToolError(f"MySQL error {code}: {msg}") from exc
    truncated = len(rows) > limit
    rows = [{k: _json_safe(v) for k, v in r.items()} for r in rows[:limit]]
    return {"row_count": len(rows), "truncated": truncated, "rows": rows}


# --- Curated tools: one question each, gotchas baked in ---------------------------


@mcp.tool(annotations=READ_ONLY)
def list_competitions(season: str | None = None) -> list[dict]:
    """List the valid season and competition names, with match counts.

    Call this before league_table / matches / player_leaders if you are not certain of
    the exact names. Competition names repeat every season (e.g. "Men's Premier Division"
    exists in each), so a competition is identified by (competition, season) together.

    Args:
        season: optional exact season name, e.g. '2024/25'. Omit for all seasons.
    """
    sql = (
        "SELECT s.name AS season, TRIM(c.name) AS competition, "
        "COUNT(*) AS matches, SUM(m.status = 'R') AS played, "
        "COUNT(DISTINCT m.home_team_id) AS teams, "
        "MIN(DATE(m.starts_at)) AS first_match, MAX(DATE(m.starts_at)) AS last_match "
        "FROM matches m JOIN seasons s ON s.id = m.season_id "
        "JOIN competitions c ON c.id = m.competition_id "
    )
    params: tuple = ()
    if season:
        sql += "WHERE s.name = %s "
        params = (season,)
    sql += "GROUP BY s.name, TRIM(c.name) ORDER BY s.name DESC, competition"
    rows = _rows(sql, params)
    if season and not rows:
        raise ToolError(f"No season named {season!r}. Valid: " + ", ".join(
            r["name"] for r in _rows("SELECT name FROM seasons ORDER BY name")))
    return [{k: _json_safe(v) for k, v in r.items()} for r in rows]


@mcp.tool(annotations=READ_ONLY)
def find_team(query: str) -> list[dict]:
    """Find teams by (partial) team or club name, with the competitions each has played in.

    Call this first whenever a question names a team. Team names are ambiguous: a club
    has several sides (Surbiton M1, M2, W1...) and ~18 names are shared by a club's men's
    and women's teams. Use the returned team_id and gender to pick the right one, then
    pass the exact team name (or id) to other tools.

    Args:
        query: part of a team or club name, case-insensitive, e.g. 'surbiton' or 'old georgians'
    """
    like = f"%{query.strip()}%"
    teams = _rows(
        "SELECT t.id AS team_id, t.name AS team, c.name AS club "
        "FROM teams t JOIN clubs c ON c.id = t.club_id "
        "WHERE t.name LIKE %s OR c.name LIKE %s "
        "ORDER BY c.name, t.name LIMIT 30",
        (like, like),
    )
    if not teams:
        return []
    ids = tuple(t["team_id"] for t in teams)
    placeholders = ",".join(["%s"] * len(ids))
    history = _rows(
        "SELECT t.id AS team_id, s.name AS season, TRIM(c.name) AS competition, COUNT(*) AS matches "
        "FROM teams t JOIN matches m ON t.id IN (m.home_team_id, m.away_team_id) "
        "JOIN seasons s ON s.id = m.season_id JOIN competitions c ON c.id = m.competition_id "
        f"WHERE t.id IN ({placeholders}) "
        "GROUP BY t.id, s.name, TRIM(c.name) ORDER BY t.id, s.name, competition",
        ids,
    )
    by_team: dict[int, list[dict]] = {}
    for h in history:
        by_team.setdefault(h["team_id"], []).append(
            {"season": h["season"], "competition": h["competition"], "matches": h["matches"]}
        )
    for t in teams:
        seasons = by_team.get(t["team_id"], [])
        comps = " ".join(x["competition"] for x in seasons)
        # "Women" first: "Women's" contains "Men's"; some seasons spell it "Womens".
        t["gender"] = "W" if "Women" in comps else "M" if "Men" in comps else None
        t["seasons"] = seasons
    return teams


# --- Name resolution (the gotchas live here, not in the model's memory) ------------

_SPONSOR_PREFIXES = ("open - ", "vitality ")


def _norm_comp(name: str) -> str:
    n = " ".join(name.lower().replace("\u2019", "'").split())
    for prefix in _SPONSOR_PREFIXES:
        if n.startswith(prefix):
            n = n[len(prefix):]
    n = n.replace("womens", "women's").replace("mens", "men's")
    n = re.sub(r"\bprem\b", "premier", n)
    n = re.sub(r"\bdiv\b", "division", n)
    return n.strip()


def _season_id(season: str) -> int:
    rows = _rows("SELECT id FROM seasons WHERE name = %s", (season,))
    if not rows:
        valid = ", ".join(r["name"] for r in _rows("SELECT name FROM seasons ORDER BY name"))
        raise ToolError(f"No season named {season!r}. Valid: {valid}")
    return rows[0]["id"]


def _resolve_competition(competition: str, season: str | None) -> tuple[list[int], str]:
    """Map a loosely-typed competition name to competition ids, tolerant of sponsor
    prefixes and spelling drift. Returns (ids, canonical name)."""
    sql = (
        "SELECT DISTINCT c.id, TRIM(c.name) AS name FROM competitions c "
        "JOIN matches m ON m.competition_id = c.id JOIN seasons s ON s.id = m.season_id "
    )
    params: tuple = ()
    if season:
        sql += "WHERE s.name = %s "
        params = (season,)
    candidates = _rows(sql, params)
    if season and not candidates:
        _season_id(season)  # raises with the valid list
    wanted = _norm_comp(competition)
    # Group by normalised name so "Men's Premier Division" and "Open - Men's Premier
    # Division" (the same league, different seasons) count as one competition.
    groups: dict[str, list[dict]] = {}
    for c in candidates:
        groups.setdefault(_norm_comp(c["name"]), []).append(c)

    def _pick(norm: str) -> tuple[list[int], str]:
        rows = groups[norm]
        return [c["id"] for c in rows], " / ".join(sorted({c["name"] for c in rows}))

    if wanted in groups:
        return _pick(wanted)
    # Word-boundary match so "men's ..." never matches inside "women's ...".
    at_word = re.compile(r"(?<![a-z])" + re.escape(wanted))
    hits = sorted(n for n in groups if at_word.search(n))
    if len(hits) == 1:
        return _pick(hits[0])
    # Several hits: if the shortest is a prefix of all the others they are its
    # sub-competitions (Pool A, Phase 2, Play Offs...) — take the base competition.
    prefixed = [n for n in hits if n.startswith(wanted)]
    if prefixed and len(prefixed) == len(hits):
        base = min(prefixed, key=len)
        if all(n.startswith(base) for n in prefixed):
            return _pick(base)
    scope = f" in {season}" if season else ""
    if hits:
        shown = sorted({c["name"] for n in hits for c in groups[n]})
        raise ToolError(f"{competition!r} is ambiguous{scope}; did you mean: " + " | ".join(shown))
    raise ToolError(f"No competition matching {competition!r}{scope}. Call list_competitions.")


def _resolve_team(ref: str | int) -> dict:
    """Accept a team id or a team name; return {team_id, team}. Refuse ambiguity."""
    if isinstance(ref, int) or str(ref).isdigit():
        rows = _rows("SELECT id AS team_id, name AS team FROM teams WHERE id = %s", (int(ref),))
        if not rows:
            raise ToolError(f"No team with id {ref}.")
        return rows[0]
    name = str(ref).strip()
    rows = _rows("SELECT id AS team_id, name AS team FROM teams WHERE name = %s", (name,))
    if len(rows) == 1:
        return rows[0]
    rows = _rows(
        "SELECT id AS team_id, name AS team FROM teams WHERE name LIKE %s ORDER BY name LIMIT 12",
        (f"%{name}%",),
    )
    if len(rows) == 1:
        return rows[0]
    if not rows:
        raise ToolError(f"No team matching {name!r}. Call find_team.")
    raise ToolError(
        f"{name!r} matches several teams: " + ", ".join(f"{r['team']} (id {r['team_id']})" for r in rows)
        + ". Pass the exact name or team_id (see find_team)."
    )


@mcp.tool(annotations=READ_ONLY)
def league_table(competition: str, season: str) -> dict:
    """League standings for one competition in one season (played matches only).

    Columns: position, team, played, won, drawn, lost, goals_for, goals_against,
    goal_diff, points (3 for a win, 1 for a draw). Sorted by points, goal difference,
    goals for. Competition names are matched loosely (sponsor prefixes and 'Womens' vs
    "Women's" are ignored), so "Men's Premier Division" works for every season.

    Args:
        competition: e.g. "Men's Premier Division", "Women's Division 1 South"
        season: e.g. '2024/25'
    """
    ids, canonical = _resolve_competition(competition, season)
    sid = _season_id(season)
    placeholders = ",".join(["%s"] * len(ids))
    where = f"competition_id IN ({placeholders}) AND season_id = %s AND status = 'R'"
    rows = _rows(
        "SELECT t.id AS team_id, t.name AS team, COUNT(*) AS played, "
        "SUM(x.gf > x.ga) AS won, SUM(x.gf = x.ga) AS drawn, SUM(x.gf < x.ga) AS lost, "
        "SUM(x.gf) AS goals_for, SUM(x.ga) AS goals_against, SUM(x.gf) - SUM(x.ga) AS goal_diff, "
        "SUM(x.gf > x.ga) * 3 + SUM(x.gf = x.ga) AS points "
        "FROM ("
        f"  SELECT home_team_id AS team_id, home_score AS gf, away_score AS ga FROM matches WHERE {where} "
        "  UNION ALL "
        f"  SELECT away_team_id, away_score, home_score FROM matches WHERE {where}"
        ") x JOIN teams t ON t.id = x.team_id "
        "GROUP BY t.id, t.name ORDER BY points DESC, goal_diff DESC, goals_for DESC, team",
        (*ids, sid, *ids, sid),
    )
    table = [{"position": i, **{k: _json_safe(v) for k, v in r.items()}} for i, r in enumerate(rows, 1)]
    return {"competition": canonical, "season": season, "played_matches_only": True, "table": table}


@mcp.tool(annotations=READ_ONLY)
def matches(
    team: str | None = None,
    opponent: str | None = None,
    competition: str | None = None,
    season: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> dict:
    """Fixtures and results, filtered any way you like. Covers: a team's results or
    upcoming fixtures, head-to-head (team + opponent), a competition's matches in a date
    range, this week's games. All filters optional and combinable; newest-first when a
    team is given, otherwise chronological.

    status: 'R' = played (has scores), 'S' = scheduled fixture (no scores), 'A' abandoned,
    'P' postponed. Omit for all. Dates are 'YYYY-MM-DD', inclusive.

    Args:
        team: team name or team_id (use find_team if unsure; ambiguous names are rejected)
        opponent: second team, for head-to-head
        competition: e.g. "Men's Premier Division" (loosely matched)
        season: e.g. '2025/26'
        date_from: earliest match date, 'YYYY-MM-DD'
        date_to: latest match date, 'YYYY-MM-DD'
        status: 'R', 'S', 'A' or 'P'
        limit: max matches to return (default 50, max 200)
    """
    where, params, resolved = [], [], {}
    if team:
        t = _resolve_team(team)
        resolved["team"] = t["team"]
        where.append("%s IN (m.home_team_id, m.away_team_id)")
        params.append(t["team_id"])
    if opponent:
        o = _resolve_team(opponent)
        resolved["opponent"] = o["team"]
        where.append("%s IN (m.home_team_id, m.away_team_id)")
        params.append(o["team_id"])
    if competition:
        ids, canonical = _resolve_competition(competition, season)
        resolved["competition"] = canonical
        where.append(f"m.competition_id IN ({','.join(['%s'] * len(ids))})")
        params.extend(ids)
    if season:
        where.append("m.season_id = %s")
        params.append(_season_id(season))
    if date_from:
        where.append("DATE(m.starts_at) >= %s")
        params.append(date_from)
    if date_to:
        where.append("DATE(m.starts_at) <= %s")
        params.append(date_to)
    if status:
        status = status.upper()
        if status not in ("R", "S", "A", "P"):
            raise ToolError("status must be one of R, S, A, P")
        where.append("m.status = %s")
        params.append(status)
    limit = max(1, min(int(limit), 200))
    order = "DESC" if team else "ASC"
    rows = _rows(
        "SELECT m.id AS match_id, m.starts_at, s.name AS season, TRIM(c.name) AS competition, "
        "ht.name AS home, m.home_score, m.away_score, at.name AS away, m.status "
        "FROM matches m JOIN seasons s ON s.id = m.season_id "
        "JOIN competitions c ON c.id = m.competition_id "
        "JOIN teams ht ON ht.id = m.home_team_id JOIN teams at ON at.id = m.away_team_id "
        + ("WHERE " + " AND ".join(where) + " " if where else "")
        + f"ORDER BY m.starts_at {order} LIMIT %s",
        (*params, limit + 1),
    )
    truncated = len(rows) > limit
    out = [{k: _json_safe(v) for k, v in r.items()} for r in rows[:limit]]
    return {"filters": resolved, "count": len(out), "truncated": truncated, "matches": out}


_STATS = {
    "goals": ("FG", "PC", "PS"),
    "field_goals": ("FG",),
    "pc_goals": ("PC",),
    "strokes": ("PS",),
    "cards": ("GC", "YC", "RC"),
    "green_cards": ("GC",),
    "yellow_cards": ("YC",),
    "red_cards": ("RC",),
}


@mcp.tool(annotations=READ_ONLY)
def player_leaders(
    stat: str = "goals",
    competition: str | None = None,
    season: str | None = None,
    team: str | None = None,
    limit: int = 10,
) -> dict:
    """Top players for a statistic: goals, field_goals, pc_goals, strokes, cards,
    green_cards, yellow_cards, red_cards. Played matches only. Each row includes the
    breakdown (goals: field/pc/stroke; cards: green/yellow/red).

    The response also reports how many played matches in scope have NO event data, so
    you can caveat the answer (2022/23 has none; 2023/24 has gaps).

    Args:
        stat: one of goals, field_goals, pc_goals, strokes, cards, green_cards, yellow_cards, red_cards
        competition: e.g. "Men's Division 1 North" (loosely matched; word-boundary safe)
        season: e.g. '2023/24'
        team: team name or team_id (see find_team)
        limit: rows to return (default 10, max 50)
    """
    if stat not in _STATS:
        raise ToolError(f"stat must be one of: {', '.join(_STATS)}")
    # Two filters: which matches are in scope (for the coverage check) and which
    # events count (the team's own events, not its opponents').
    match_where, match_params, scope = ["m.status = 'R'"], [], {"stat": stat}
    event_where, event_params = [], []
    if competition:
        ids, canonical = _resolve_competition(competition, season)
        scope["competition"] = canonical
        match_where.append(f"m.competition_id IN ({','.join(['%s'] * len(ids))})")
        match_params.extend(ids)
    if season:
        match_where.append("m.season_id = %s")
        match_params.append(_season_id(season))
        scope["season"] = season
    if team:
        t = _resolve_team(team)
        scope["team"] = t["team"]
        match_where.append("%s IN (m.home_team_id, m.away_team_id)")
        match_params.append(t["team_id"])
        event_where.append("e.team_id = %s")
        event_params.append(t["team_id"])
    coverage = _rows(
        "SELECT COUNT(*) AS played, "
        "SUM(NOT EXISTS (SELECT 1 FROM match_events x WHERE x.match_id = m.id)) AS without_events "
        f"FROM matches m WHERE {' AND '.join(match_where)}",
        tuple(match_params),
    )[0]
    where, params = match_where + event_where, match_params + event_params
    types = _STATS[stat]
    type_list = ",".join(["%s"] * len(types))
    limit = max(1, min(int(limit), 50))
    rows = _rows(
        "SELECT p.id AS player_id, CONCAT(p.first_name, ' ', p.last_name) AS player, "
        "GROUP_CONCAT(DISTINCT t.name ORDER BY t.name SEPARATOR ' / ') AS team, "
        "COUNT(*) AS total, "
        "SUM(e.event_type = 'FG') AS field, SUM(e.event_type = 'PC') AS pc, SUM(e.event_type = 'PS') AS stroke, "
        "SUM(e.event_type = 'GC') AS green, SUM(e.event_type = 'YC') AS yellow, SUM(e.event_type = 'RC') AS red "
        "FROM match_events e JOIN matches m ON m.id = e.match_id "
        "JOIN players p ON p.id = e.player_id JOIN teams t ON t.id = e.team_id "
        f"WHERE {' AND '.join(where)} AND e.event_type IN ({type_list}) "
        "GROUP BY p.id, player ORDER BY total DESC, player LIMIT %s",
        (*params, *types, limit),
    )
    keep = ("field", "pc", "stroke") if types[0] in ("FG", "PC", "PS") else ("green", "yellow", "red")
    leaders = [
        {"rank": i, "player": r["player"], "player_id": r["player_id"], "team": r["team"],
         stat: _json_safe(r["total"]), **{k: _json_safe(r[k]) for k in keep}}
        for i, r in enumerate(rows, 1)
    ]
    return {"scope": scope, "coverage": {k: _json_safe(v) for k, v in coverage.items()}, "leaders": leaders}


_GOAL_TYPES = {"FG": "field goal", "PC": "penalty corner", "PS": "penalty stroke"}
_CARD_TYPES = {"GC": "green", "YC": "yellow", "RC": "red"}


@mcp.tool(annotations=READ_ONLY)
def match_detail(match_id: int) -> dict:
    """Everything recorded for one match: score, every goal (minute, type, scorer, team),
    every card (minute, colour, player, team), and the umpires. Get match_id from
    `matches`. A played match with an empty events list has no event data at source
    (all of 2022/23, some of 2023/24) — say so rather than reporting 0 goals.

    Args:
        match_id: the id returned by `matches`
    """
    rows = _rows(
        "SELECT m.id AS match_id, m.starts_at, m.status, s.name AS season, TRIM(c.name) AS competition, "
        "ht.id AS home_team_id, ht.name AS home, m.home_score, at.id AS away_team_id, at.name AS away, m.away_score "
        "FROM matches m JOIN seasons s ON s.id = m.season_id JOIN competitions c ON c.id = m.competition_id "
        "JOIN teams ht ON ht.id = m.home_team_id JOIN teams at ON at.id = m.away_team_id WHERE m.id = %s",
        (match_id,),
    )
    if not rows:
        raise ToolError(f"No match with id {match_id}. Find ids with `matches`.")
    m = {k: _json_safe(v) for k, v in rows[0].items()}
    events = _rows(
        "SELECT e.minute, e.event_type, t.name AS team, "
        "CONCAT(p.first_name, ' ', p.last_name) AS player "
        "FROM match_events e JOIN teams t ON t.id = e.team_id "
        "LEFT JOIN players p ON p.id = e.player_id "
        "WHERE e.match_id = %s ORDER BY e.minute, e.id",
        (match_id,),
    )
    goals = [
        {"minute": e["minute"], "type": _GOAL_TYPES[e["event_type"]], "team": e["team"], "player": e["player"]}
        for e in events if e["event_type"] in _GOAL_TYPES
    ]
    cards = [
        {"minute": e["minute"], "card": _CARD_TYPES[e["event_type"]], "team": e["team"], "player": e["player"]}
        for e in events if e["event_type"] in _CARD_TYPES
    ]
    officials = _rows(
        "SELECT role, name FROM match_officials WHERE match_id = %s "
        "AND (role LIKE 'Umpire%%' OR role LIKE 'Match Official%%') ORDER BY role, name",
        (match_id,),
    )
    return {
        **m,
        "has_event_data": bool(events),
        "goals": goals,
        "cards": cards,
        "officials": officials,
    }


@mcp.tool(annotations=READ_ONLY)
def audit_log(limit: int = 50) -> list[dict]:
    """Admin only: the most recent tool calls made through this server — when, by whom
    (user and role), which tool, with what arguments, and whether it was served, denied, or errored.

    Args:
        limit: number of most-recent entries to return (default 50, max 500)
    """
    limit = max(1, min(int(limit), 500))
    if not AUDIT_PATH.exists():
        return []
    lines = AUDIT_PATH.read_text().splitlines()[-limit:]
    return [json.loads(l) for l in lines][::-1]


if __name__ == "__main__":
    # Two doors, same server. stdio (default): the host launches us and talks over
    # stdin/stdout — nothing to host. http: we listen on a port at /mcp so any host
    # that can reach the URL can connect (localhost, a tunnel, or a deploy).
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    if transport == "http":
        mcp.run(
            "streamable-http",
            host=os.environ.get("MCP_HOST", "127.0.0.1"),
            port=int(os.environ.get("MCP_PORT", "8000")),
            stateless_http=True,  # each request stands alone — simplest to host and scale
        )
    else:
        mcp.run()
