#!/usr/bin/env bash
# The weekly Prem graphics, start to finish, no Claude needed.
#
#   ./weekly.sh call            # Thu/Fri: fresh data -> models rebuilt -> The Call PNGs for the coming weekend
#   ./weekly.sh verdict         # Sun night/Mon: results in -> The Verdict PNGs (+ Full Time, + Power Rankings) for the weekend just gone
#   ./weekly.sh rankings        # just the Power Rankings stories (one Men's/Open, one Women's) for the weekend just gone
#
# Options (after the mode):
#   --from YYYY-MM-DD  first day of the round (default: next Saturday for call, the last one for verdict)
#   --to   YYYY-MM-DD  last day (default: the Sunday after --from)
#   --no-ingest        skip the England Hockey download (re-render from what's already in the DB)
#   --force            call: overwrite a Call already frozen for that weekend (only if it was never posted)
#   --partial          verdict: mark what's played even if some results aren't in yet
#
# What it writes:
#   analysis/reports/weekly/<from>_call.json     the frozen predictions as published (commit this)
#   analysis/reports/weekly/<from>_verdict.json  marked against the results; the season tally is built from these
#   content/weekly/<from>/{call,verdict,full-time}/*.png   1080x1350 slides, ready to post
#   content/weekly/<from>/power-rankings-story/story-{M,W}.png   1080x1920 stories, ready to post
#
# The models need no separate "update" step: round_card.py retrains Elo and Poisson from every
# result before the round each time it runs, so ingesting the latest results IS the update.
set -euo pipefail
cd "$(dirname "$0")"

SEASON="${SEASON:-2026/27}"
MODE="${1:-}"; shift || true
FROM=""; TO=""; INGEST=1; FORCE=0; PARTIAL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --from) FROM="$2"; shift 2 ;;
    --to) TO="$2"; shift 2 ;;
    --no-ingest) INGEST=0; shift ;;
    --force) FORCE=1; shift ;;
    --partial) PARTIAL="--partial"; shift ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
done
case "$MODE" in call|verdict|rankings) ;; *) sed -n '2,21p' "$0"; exit 2 ;; esac

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
fail() {
  printf '\n\033[31mSTOPPED: %s\033[0m\n' "$*" >&2
  osascript -e "display notification \"$MODE stopped: see the terminal\" with title \"FHA weekly\"" 2>/dev/null || true
  exit 1
}

# ---- dates: Saturday-based weekends
if [ -z "$FROM" ]; then
  FROM=$(python3 - "$MODE" <<'EOF'
import datetime as dt, sys
t = dt.date.today()
back = (t.weekday() - 5) % 7                    # days since the last Saturday (0 on a Saturday)
print(t - dt.timedelta(days=back) if sys.argv[1] != "call" else t + dt.timedelta(days=(5 - t.weekday()) % 7))
EOF
)
fi
[ -n "$TO" ] || TO=$(python3 -c "import datetime as d; print(d.date.fromisoformat('$FROM') + d.timedelta(days=1))")
ASOF=$(python3 -c "import datetime as d; print(d.date.fromisoformat('$FROM') - d.timedelta(days=1))")

REC="analysis/reports/weekly"; OUT="content/weekly/$FROM"
CALL="$REC/${FROM}_call.json"
mkdir -p "$REC" "$OUT" analysis/output/weekly
echo "$MODE for $FROM to $TO (models know everything before $ASOF)"

# ---- 1. Docker
say "Starting the database"
docker info >/dev/null 2>&1 || fail "Docker isn't running. Open Docker Desktop and run this again."
docker compose up -d --wait db jupyter >/dev/null || fail "db/jupyter containers didn't come up healthy"

# ---- 2. Fresh data (this is also the model update)
if [ "$INGEST" = 1 ]; then
  say "Downloading results from England Hockey"
  docker compose run --rm ingest ingest import-fixtures \
    || fail "import-fixtures failed. If the log shows 401/403, check EH_API_KEY in .env."
  say "Catching half-logged scorers"
  docker compose run --rm ingest ingest backfill-events --season "$SEASON" || fail "backfill-events failed"
  say "Catching late cards and changed scorers (last 4 weeks)"
  docker compose run --rm ingest ingest backfill-events --season "$SEASON" --all --days 28 || fail "backfill-events --all failed"
  say "Running repairs"
  docker compose run --rm ingest ingest repair || fail "repair failed"
fi

card() {  # card <json name> -> runs both models, text report to stdout
  docker compose exec -T jupyter python3 /home/jovyan/work/round_card.py \
    --from "$FROM" --to "$TO" --as-of "$ASOF" --json "/home/jovyan/work/output/weekly/$1"
}

# ---- 3. The Call
if [ "$MODE" = call ]; then
  if [ -f "$CALL" ] && [ "$FORCE" = 0 ]; then
    fail "a Call for $FROM is already frozen ($CALL). If it was never posted, rerun with --force."
  fi
  say "Rebuilding both models and predicting"
  card call.json | tee "$REC/${FROM}_call.txt" || fail "round_card.py failed"
  say "Checking and drawing The Call"
  python3 content/series/build-the-call.py --mode call --json analysis/output/weekly/call.json --png "$OUT/call" \
    || fail "checks failed (above); nothing frozen"
  cp analysis/output/weekly/call.json "$CALL"
  echo "frozen: $CALL  (commit it: it's the public record the Verdict marks against)"
fi

# ---- 4. The Verdict (+ Full Time)
if [ "$MODE" = verdict ]; then
  say "Re-running the models as of $ASOF, now with results"
  card results.json > "$REC/${FROM}_verdict.txt" || fail "round_card.py failed"
  RECORD="--record $REC"
  if [ ! -f "$CALL" ]; then
    echo "  no frozen Call for $FROM: marking the reproduced predictions, NOT counted in the season record"
    cp analysis/output/weekly/results.json analysis/output/weekly/call.json
    CALL=analysis/output/weekly/call.json; RECORD=""
  fi
  cat "$REC/${FROM}_verdict.txt"
  say "Checking and drawing The Verdict"
  python3 content/series/build-the-call.py --mode verdict --json "$CALL" \
    --results analysis/output/weekly/results.json --png "$OUT/verdict" $RECORD $PARTIAL \
    || fail "verdict not drawn (above)"
  say "Drawing Full Time (every league's results)"
  python3 content/series/build-full-time.py --from "$FROM" --to "$TO" --png "$OUT/full-time" >/dev/null \
    || echo "  Full Time failed; the Verdict is still done"
fi

# ---- 5. Power Rankings stories (verdict and rankings): Elo rebuilt from every result, one story per division
if [ "$MODE" = verdict ] || [ "$MODE" = rankings ]; then
  say "Drawing the Power Rankings stories"
  python3 content/series/build-power-rankings.py --refresh --story --png "$OUT/power-rankings-story" >/dev/null \
    && echo "  $OUT/power-rankings-story/story-M.png, story-W.png" \
    || echo "  Power Rankings failed; everything before it is still done"
fi

if [ "$MODE" = verdict ]; then
  say "Refreshing the seed snapshot"
  ./data/seed/dump.sh >/dev/null && echo "  data/seed/hockey_stats.sql.gz updated"
fi

say "Done"
echo "Slides: $OUT"
echo "To keep the record: git add $REC data/seed && git commit -m \"Weekly $MODE $FROM\""
osascript -e "display notification \"$MODE ready in $OUT\" with title \"FHA weekly\"" 2>/dev/null || true
open "$OUT" 2>/dev/null || true
