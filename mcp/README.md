# Hockey Stats MCP server

Exposes the `hockey_stats` database to MCP hosts (Claude Code, Claude Desktop, ...) as a
small set of tools. `server.py` is the server; `smoke.py` is a throwaway client that does
what a host does (launch, handshake, `tools/list`, `tools/call`) so you can test without
an AI in the loop.

## Credential

The server connects as `hockey_reader`, a MySQL user with `SELECT` only. That grant — not
the server code — is what makes the server read-only. Create it once (db container running):

```sql
CREATE USER 'hockey_reader'@'%' IDENTIFIED BY '<password>';
GRANT SELECT ON hockey_stats.* TO 'hockey_reader'@'%';
```

then put the connection string in the repo `.env` as `MCP_DATABASE_URL` (see `.env.example`).

## Auth (HTTP only)

Static bearer tokens, one per person, in the repo `.env`:

```
MCP_USERS="you:admin:<token>;someone:analyst:<token>"     # openssl rand -hex 24
```

| Caller | Role | Tools |
|---|---|---|
| no `Authorization` header | `viewer` | the six curated tools |
| `Authorization: Bearer <token>` of an `analyst` | `analyst` | + `run_query`, `list_tables`, `describe_table` |
| … of an `admin` | `admin` | + `audit_log` |
| stdio (launched locally by the host) | `admin` | everything — being launched locally is the permission |

A wrong token is rejected, not downgraded. `tools/list` is filtered per role and `tools/call`
refuses tools outside it. Every call appends one line to `mcp/audit.log` (gitignored):
time, who, role, tool, args, ok/denied/error, ms. It's all one middleware (`auth_middleware`);
swapping static tokens for OAuth later means replacing `_identify` and nothing else.

## Run

Claude Code picks the server up from `/.mcp.json` when a session starts in this repo (stdio).
For the HTTP door — needed by anything that connects by URL:

```
MCP_TRANSPORT=http uv run --directory mcp server.py          # http://127.0.0.1:8000/mcp
claude mcp add --transport http hockey-http http://127.0.0.1:8000/mcp --header "Authorization: Bearer <token>"
```

To test by hand:

```
uv run --directory mcp smoke.py                                  # list tools, call list_tables
uv run --directory mcp smoke.py describe_table '{"table":"matches"}'
uv run --directory mcp smoke.py --url http://127.0.0.1:8000/mcp league_table '{"competition":"mens prem","season":"2024/25"}'
```

Or raw HTTP (Postman/curl): `POST /mcp` with `Content-Type: application/json`,
`Accept: application/json, text/event-stream`, body `{"jsonrpc":"2.0","id":1,"method":"tools/list"}`.

Dependencies are declared in the script headers; `uv run` installs them in an isolated env.

## Tools

| Tool | Purpose |
|---|---|
| `list_tables`, `describe_table` | schema discovery |
| `run_query(sql, limit)` | escape hatch — any SELECT, one statement, ≤500 rows, 10s cap; errors are passed back so the model can fix the query |
| `list_competitions(season?)` | valid season/competition names with match counts |
| `find_team(query)` | resolves a partial name to sides (M1/W1…) with gender and season history |
| `league_table(competition, season)` | standings; competition names matched loosely (`_norm_comp`) |
| `matches(team?, opponent?, competition?, season?, date_from?, date_to?, status?)` | fixtures, results, head-to-head |
| `player_leaders(stat, competition?, season?, team?)` | goals / pc_goals / cards…; reports event-coverage gaps |
| `match_detail(match_id)` | one match: score, goals (minute/type/scorer), cards, umpires; flags missing event data |
| `audit_log(limit)` | admin only — recent calls: who, role, tool, args, outcome |

All tools are annotated `read_only_hint` so hosts can skip confirmation prompts. Raise
`ToolError` for anything the model should read; other exceptions are masked by the SDK.
