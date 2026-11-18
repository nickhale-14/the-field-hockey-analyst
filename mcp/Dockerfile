# Hockey Stats MCP server — one process, HTTP door, data lives in RDS MySQL.
# Secrets (MCP_DATABASE_URL, MCP_USERS) are NOT baked in: pass them as env vars
# (Secrets Manager via the ECS task definition in production, `docker run -e …`
# locally). Build for amd64 — Fargate is x86 and an arm64 image will not start.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
COPY server.py .

# Install the script's dependencies (declared in its header) at build time so the
# container starts instantly instead of resolving packages on first boot.
RUN uv sync --script server.py

ENV MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000
EXPOSE 8000

CMD ["uv", "run", "--script", "server.py"]
