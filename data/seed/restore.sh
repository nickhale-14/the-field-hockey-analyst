#!/usr/bin/env bash
# Restore the committed seed dumps into the running db container.
# Replaces the need to re-run ~3 hours of API ingestion (England) and a few
# hours of polite scraping (Germany) on a fresh clone.
#
#   hockey_stats.sql.gz     -> hockey_stats     (England)
#   hockey_stats_de.sql.gz  -> hockey_stats_de  (German 1. Bundesliga, see docs/germany.md)
#
# The container only creates hockey_stats on first start, so hockey_stats_de
# is created (and granted to hockey_stats) here, as root.
#
# Usage (from the repo root, with `docker compose up -d db` already healthy):
#   ./data/seed/restore.sh                    # both
#   ./data/seed/restore.sh hockey_stats_de    # just one
set -euo pipefail
cd "$(dirname "$0")/../.."

DBS=("$@")
[ ${#DBS[@]} -gt 0 ] || DBS=(hockey_stats hockey_stats_de)

for DB in "${DBS[@]}"; do
  SEED="data/seed/$DB.sql.gz"
  [ -f "$SEED" ] || { echo "missing $SEED" >&2; exit 1; }

  if [ "$DB" != "hockey_stats" ]; then
    docker compose exec -T db sh -c "mysql -uroot -p\"\$MYSQL_ROOT_PASSWORD\" -e \"
      CREATE DATABASE IF NOT EXISTS $DB CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
      GRANT ALL PRIVILEGES ON $DB.* TO 'hockey_stats'@'%';\"" 2>/dev/null
  fi

  echo "restoring $SEED into $DB..."
  gunzip -c "$SEED" | docker compose exec -T db mysql -u hockey_stats -phockey_stats "$DB" 2>/dev/null
  echo "done. $DB row counts:"
  docker compose exec -T db mysql -u hockey_stats -phockey_stats "$DB" -N -e "
    SELECT 'matches', COUNT(*) FROM matches UNION ALL
    SELECT 'match_events', COUNT(*) FROM match_events UNION ALL
    SELECT 'players', COUNT(*) FROM players UNION ALL
    SELECT 'teams', COUNT(*) FROM teams;" 2>/dev/null
done
