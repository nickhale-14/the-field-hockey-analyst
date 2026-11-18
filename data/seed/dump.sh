#!/usr/bin/env bash
# Regenerate the committed seed dumps from the live db container.
# Run this after ingesting new data or fixing data quality issues, then commit
# the data/seed/<db>.sql.gz it wrote so a fresh clone gets the current state.
#
#   hockey_stats     England (the main database)
#   hockey_stats_de  German 1. Bundesliga - kept separate on purpose, see docs/germany.md
#
# Usage (from the repo root):
#   ./data/seed/dump.sh                    # both
#   ./data/seed/dump.sh hockey_stats_de    # just one
set -euo pipefail
cd "$(dirname "$0")/../.."

DBS=("$@")
[ ${#DBS[@]} -gt 0 ] || DBS=(hockey_stats hockey_stats_de)

for DB in "${DBS[@]}"; do
  OUT="data/seed/$DB.sql.gz"
  echo "dumping $DB to $OUT..."
  docker compose exec -T db mysqldump -u hockey_stats -phockey_stats \
    --single-transaction --no-tablespaces "$DB" 2>/dev/null | gzip -9 > "$OUT"
  ls -lh "$OUT"
  gunzip -c "$OUT" | tail -1
done
