FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# uv is a fast pip replacement. We use it inside the image so the host does not need it.
RUN pip install --no-cache-dir uv

COPY pyproject.toml /app/pyproject.toml
COPY src /app/src
COPY tests /app/tests
COPY alembic.ini /app/alembic.ini
COPY alembic /app/alembic

# Install the project (and its deps) into the system site-packages so the `ingest` CLI is available.
RUN uv pip install --system -e ".[dev]"

CMD ["ingest", "--help"]
