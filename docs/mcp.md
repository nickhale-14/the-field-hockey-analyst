# MCP server

Read when working on `mcp/`, its hosting, or auth. Deployment state and AWS
decisions live in [`docs/deployment.md`](deployment.md); this file is the server
itself. `mcp/README.md` covers local development, `mcp/release.sh` builds and pushes.

## What it is

Lets Claude query the hockey database through tools instead of raw shell access.
Connects as `hockey_reader`, a MySQL user with `SELECT` only — **that grant, not the
code, is the read-only guarantee.**

Tools, in three tiers:

- **Schema** — `list_tables`, `describe_table`
- **Escape hatch** — `run_query` (SELECT/WITH only, one statement, row-capped, 10s ceiling)
- **Curated** — `list_competitions`, `find_team`, `league_table`, `matches`,
  `player_leaders`, `match_detail`

The curated tools absorb the gotchas in [`docs/data-scope.md`](data-scope.md)
(`status='R'`, competition-name drift, gender-ambiguous team names) so the model
doesn't have to know them.

Two transports: **stdio** (default, via `.mcp.json`) and **HTTP**
(`MCP_TRANSPORT=http`, port 8000, path `/mcp`).

Python `mcp` SDK is 2.x — `MCPServer`, snake_case attributes, raise `ToolError`
for anything the model should be able to read and act on.

## Auth and roles

Static bearer tokens, one per person, in `MCP_USERS="user:role:token;…"`.

| Arriving as | Identity | Can call |
|---|---|---|
| no `Authorization` header | `anonymous` / viewer | the 6 curated tools |
| a known token | that person / analyst | + `run_query`, schema tools |
| a known token | that person / admin | + `audit_log` |
| an unknown token | rejected, `-32001` | — |
| stdio (no HTTP at all) | `local` / admin | everything |

No token is allowed in; a *wrong* token is not. Deliberate: a stranger can browse
the curated tools with zero setup, while a typo in someone's config fails loudly
instead of silently downgrading them.

`auth_middleware` runs before anything else. It filters `tools/list` so a viewer
never sees `run_query` exists, refuses out-of-role `tools/call`, and appends a line
per call to `mcp/audit.log` (gitignored) and to stderr (so a host platform's log
driver catches it — in a container the file is wiped on every deploy).

**Roles do not change the database user.** Every role connects as `hockey_reader`.
The role gates *which tools you may call*, nothing more.

**Tokens are 24 random bytes as hex** (48 characters, ~192 bits — `secrets.token_hex(24)`).
Rotating them needs a new deployment: `_users()` reads the environment, and a running
process's environment cannot change.

`_identify` is the only place the server decides who you are, which is the point —
swapping static tokens for OAuth rewrites those eleven lines and nothing downstream,
because everything else only ever receives `(user, role)`.

## Database configuration — two shapes

`MCP_DATABASE_URL` (one URL, what `.env` and local development use) wins when set.
Otherwise `_connect` builds the settings from separate parts:

| Variable | Default |
|---|---|
| `DB_HOST`, `DB_USERNAME` | none — either missing means the parts path is skipped |
| `DB_PASSWORD` | `""` |
| `DB_PORT` | `3306` |
| `DB_NAME` | `hockey_stats` |
| `DB_SSL` | `REQUIRED` |

The separate form is the shape AWS hands you: a Secrets Manager secret for an RDS
user holds username / password / host / port as separate JSON keys, so ECS injects
each one and rotation can replace the password without rewriting anything else.
That secret carries **no dbname**, so `DB_NAME` is a plain environment variable.

TLS is on by default on the parts path; on the URL path it follows `ssl-mode`.
Neither set raises a `RuntimeError` naming both options.

## Health endpoints

| Path | Auth | Returns |
|---|---|---|
| `/health` | none | 200, never touches the database |
| `/health/db` | admin bearer | 200 + played-match count, or 503 |
| `/` | — | **404 — never use as a health check** |

**Point a load balancer at `/health`.** It deliberately avoids the database: a deep
check there would let a brief RDS blip fail the health check, the orchestrator would
replace a container that was working fine, and each replacement would hammer the
struggling database on startup. A blip becomes an outage.

`/health/db` is for monitoring and hand-debugging a deploy — it catches what
`/health` can't (wrong connection string, missing security-group rule, revoked
grants). It returns only the exception *class* name, never the message, which
carries host and username. It requires an admin token and returns **404 rather than
403** so the route isn't advertised; the row count is what distinguishes "pointed at
the real database" from "pointed at an empty one".

Both bypass `auth_middleware` — `custom_route` registers them directly with
Starlette, which is correct for `/health` since a load balancer sends no token, and
is why `/health/db` checks the header by hand.

## Security posture (reviewed 2026-10-06)

`hockey_reader`'s grants were verified as exactly:

```
GRANT USAGE ON *.* TO `hockey_reader`@`%`
GRANT SELECT ON `hockey_stats`.* TO `hockey_reader`@`%`
```

That closes the three escalation routes: no `SELECT` on `mysql.*` (can't read other
users' password hashes), no `FILE` privilege (no `LOAD_FILE`, no `INTO OUTFILE`), and
no SQL exists for switching user mid-connection. A client never sees or supplies a
database credential — the connection string lives only in the server's environment,
and no tool reads environment variables or files.

Four independent layers, and the innermost is enforced by MySQL rather than by code:
**grant → statement guard → role gate → network**.

Known and accepted weaknesses:

- **No rate limiting.** Not a practical break at 192-bit tokens; the real exposure is
  abuse and cost on the open viewer endpoint. An AWS WAF rate rule on the load
  balancer fixes both, attaches with no downtime, and is deferred.
- **The viewer endpoint is open to the internet by design** (no token = viewer).
- **An analyst can read every table in `hockey_stats`** via `run_query`. Fine for
  public match data; **keep auth/user tables in a separate database** that
  `hockey_reader` is never granted on.
- **Secrets sit in the container environment**, so anyone who can run
  `ecs execute-command` or assume the task role can read them. Keep that IAM tight —
  it's the most likely real-world path to the credentials.

## Planned next

OAuth via an issuer (Cognito), replacing `_identify` only. A `team_summary` tool.
