# ingest service

This service is a CLI-first ingestion job. It is intended to run as an ephemeral container:

```bash
docker compose run --rm ingest ingest migrate
docker compose run --rm ingest ingest run
```

