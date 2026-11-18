# Deployment — the cloud / AWS workstream

Read when working on the AWS deployment, the MCP server's hosting, or the
scheduled ingester.

**What goes where (so nothing is duplicated):**
- **This file** — the roadmap (what's done / next / later), the *decisions and
  understanding* picked up along the way, and the live to-do. The narrative a fresh
  session needs. (A Todoist project was tried and abandoned; this is the to-do.)
- [`docs/mcp.md`](mcp.md) — the MCP server's own technical detail (auth, roles,
  health endpoints, the two DB-config shapes). Don't restate it here; link to it.
- [`mcp/release.sh`](../mcp/release.sh) / [`mcp/README.md`](../mcp/README.md) — build+push, and local development.

**Working rule:** AWS changes are the user's to run. Claude hands over the exact
commands; the user executes them in his own account. (Memory: `feedback_aws_commands_user_runs`.)

## Roadmap

0. **Status 2026-11-17: all MCP AWS infrastructure torn down** by the user to stop the
   running cost (ECS service, load balancer and the rest). Its address,
   `mcp.thefieldhockeyanalyst.com`, also went when the domain's DNS moved from Route 53
   to Cloudflare and the Route 53 hosted zone was deleted (the domain itself is now the
   predictor game's; see [`docs/predictor-game.md`](predictor-game.md)). Nothing here
   is live. The facts and decisions below are kept as the recipe for a rebuild; a
   rebuild needs a new `mcp` record in Cloudflare (CNAME to the load balancer, DNS
   only) and a new ACM validation CNAME there too.
1. **MCP server on AWS — built, then torn down (see 0).** The server itself is production-shaped:
   bearer-token auth with viewer/analyst/admin roles, an audit log, HTTP transport,
   `/health` (liveness, unauthenticated) and `/health/db` (readiness, admin-only),
   and a DB config that accepts the separate-parts shape AWS hands you (Secrets
   Manager keys for an RDS user + `DB_NAME` as a plain env var). Images build
   `linux/amd64` (Fargate is x86) and push to ECR under a readable
   `date-sha-slug` tag, never bare `:latest`. See [`docs/mcp.md`](mcp.md).
2. **Scheduled ingester — next, not started.** The weekly update (`ingest
   import-fixtures`, see [`docs/data-scope.md`](data-scope.md)) is proven but
   still manual-trigger by design. The task is to run it on a schedule so results
   flow without a laptop. **Decided 2026-11-18: GitHub Actions, not AWS** (AWS costs;
   the site must stay free). Repo is private: 2,000 free minutes/month, a weekly run
   is ~10–15. Plan, not built: a scheduled workflow (`.github/workflows/`) starts a
   MySQL service container, restores `data/seed/hockey_stats.sql.gz`, runs the weekly
   ingest + models + checks, pushes the round to Supabase, then dumps the seed and
   `web/public/data/` JSON and commits them back (the commit redeploys the site; the
   Mac catches up with `git pull` + `restore.sh`). Secrets in GitHub repo settings:
   `EH_API_KEY`, Supabase keys. Before relying on it: one manual test run to confirm
   the EH API accepts calls from GitHub's servers. Known limits: scheduled runs can
   start 10–30 min late; graphics checked by eye and audit/alias fixes stay local.
   Do it once `publish_round.py` exists (before the March 2027 launch).
3. **Later backlog.** A long list the user keeps for after the two above; pull
   items into the roadmap as they become active rather than listing them here.

## AWS facts (the ones worth not re-discovering)

- **Account** `<aws-account-id>`, **region** `ap-southeast-2`.
- **ECR repo** `hockey-master-mcp`. Registry
  `<aws-account-id>.dkr.ecr.ap-southeast-2.amazonaws.com`. Release with
  `./mcp/release.sh "short description"`; point the ECS task definition at the
  immutable `date-sha-slug` tag it prints, not `:latest` (so you can always say
  which code is live, and roll back by changing a string).
- **Fargate is x86** — an arm64 image will not start; the release script forces
  `--platform linux/amd64`.
- **RDS + Secrets Manager** is the intended DB path: a secret holds username /
  password / host / port as separate JSON keys (ECS injects each, rotation
  rewrites nothing else); the secret carries no dbname, so `DB_NAME=hockey_stats`
  is a plain env var. TLS on by default. The seed must be loaded into RDS — the
  `/health/db` row count is what tells "real database" from "empty one".
- **Load balancer** should target `/health`, never `/health/db` (a DB blip there
  would cycle healthy containers) and never `/` (returns 404). Target group port
  **8000**, not the default 80.
- **VPC endpoints needed from the private subnets** — `ecr.api`, `ecr.dkr`,
  `secretsmanager` and `logs` as *interface* endpoints, `s3` as a *gateway* endpoint
  (ECR keeps layers in S3). Interface endpoints need their own security group
  allowing 443 from `sg-mcp`, plus private DNS; the gateway endpoint needs only a
  route-table association. `sg-db` must allow 3306 from `sg-mcp`.
- **MCP task definition** — Fargate, Linux/**X86_64**, 0.25 vCPU / 0.5 GB, container
  port 8000, logs to `awslogs` group `/ecs/hockey-mcp` (set retention; the default is
  forever). Environment: `DB_NAME=hockey_stats`. Secrets: `DB_USERNAME`,
  `DB_PASSWORD`, `DB_HOST`, `DB_PORT` from the `hockey_reader` secret, plus
  `MCP_USERS`. Leave `MCP_DATABASE_URL` unset — it takes precedence and would
  silently ignore the parts.
- **Secret ARNs need the `:KEY::` suffix** (`…:secret:name-AbC123:DB_HOST::`).
  Without it the container receives the whole JSON blob as the value and fails in a
  way that points nowhere.
- **Use `hockey_reader`'s secret, not the AWS-created master one.** The master
  secret (the one carrying `dbInstanceIdentifier`) holds the admin user; pointing the
  MCP at it works immediately and silently gives `run_query` write access, discarding
  the read-only guarantee. Keep the master secret for migrations and seed loads.

## Decisions & understanding

Distil the *conclusion* here as you work — what was decided and why — not the Q&A
that got there.

**Private subnets reach AWS through endpoints, not NAT.** Four interface endpoints
cost about $29/month against NAT's $33, so cost was not the deciding factor — the
posture was. The task has no route to the internet at all. The tradeoff is real:
nothing in a private subnet can call a third-party API, so anything needing the
England Hockey API goes elsewhere (see the worker note below). The `logs` endpoint is
the one people forget, and its failure mode is nasty: `awslogs` can't reach
CloudWatch, the task won't start, and there is nothing in the logs because logging is
what's broken.

**Security groups reference each other, not addresses.** `sg-db` allows 3306 from
`sg-mcp` — not from an IP, because the task's address changes on every deploy. The
badge survives; the address doesn't.

**The scheduled ingester should go in a public subnet, not behind NAT.** It needs the
England Hockey API, which endpoints can't reach. A public subnet with *zero inbound
rules* and `assignPublicIp` enabled is unreachable from outside — the security group
decides that, not the subnet — costs nothing, and still reaches RDS over the private
network. NAT would be $400/year for the same outcome. (Without a public IP, a task in
a public subnet can't reach the internet at all: a classic half-hour of confusion.)

**TLS terminates at the load balancer.** The container speaks plain HTTP over the
private network, which is why no Caddy or nginx was ever needed and why ACM handles
renewal. Encrypting the internal leg is a regulated-environment concern, not one for
a single service over public sports data.

**An ECS service runs continuously** — about $9/month for 0.25 vCPU / 0.5 GB whether
anyone uses it or not. It does not wake on request; Lambda and App Runner are the
scale-to-zero alternatives.

**App Runner was the road not taken.** It would have been roughly a third of the
configuration — no ALB, no target group, no endpoints, TLS included — in exchange for
less control. Worth remembering the next time this has to be done under time
pressure. ECS was chosen because the VPC and RDS already existed, keeping the
database private, and because it is the more transferable thing to have built.

**Execution role and task role are different things.** The *execution* role belongs
to ECS and is used **before** the container starts: pulling the image, fetching
secrets, writing logs. The *task* role is for the application's own AWS API calls,
and this one makes none, so it stays empty. Every "secrets didn't load" failure is
the execution role — and because it happens before the container runs, the reason is
in the stopped-task details, not CloudWatch.

**`:latest` is a trap.** Pushing a new `:latest` does not update a running service
(it keeps the image it already pulled), and it orphans the previous image's only
name, leaving untagged rows you can no longer identify. Hence `mcp/release.sh` and
immutable `date-sha-slug` tags. Two related fixes: `--provenance=false` on the build,
or BuildKit adds a 0 MB attestation row per push and the ECR list becomes unreadable;
and an ECR lifecycle rule expiring untagged images (**not yet created**).

**Credentials: `aws login`, not `aws configure`.** It borrows the browser console
session and issues temporary credentials, so no long-lived access key sits on disk.
The cost is that they expire mid-session and need re-running.
