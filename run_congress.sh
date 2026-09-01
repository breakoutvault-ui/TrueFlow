#!/bin/bash
# TrueFlow - Congress trades nightly pipeline
# Runs: House -> Senate -> Enrichment -> Aggregation
# Cron: 0 20 * * 1-5   (01:30 IST next day)

cd /root/trueflow || exit 1
PY=/root/trueflow/bin/python
mkdir -p /root/trueflow/logs
LOG=/root/trueflow/logs/congress_$(date +%Y%m%d).log

{
  echo "========================================="
  echo "CONGRESS PIPELINE START $(date)"
  echo "========================================="
} >> "$LOG"

STATUS=""

run_stage () {
  NAME="$1"
  shift
  echo "" >> "$LOG"
  echo "--- $NAME $(date +%H:%M:%S) ---" >> "$LOG"
  if timeout 3600 "$PY" "$@" >> "$LOG" 2>&1; then
    echo "$NAME OK" >> "$LOG"
    STATUS="$STATUS%0A OK - $NAME"
  else
    echo "$NAME FAILED" >> "$LOG"
    STATUS="$STATUS%0A FAILED - $NAME"
  fi
}

run_stage "House"       us_congress_house.py full
run_stage "Senate"      us_congress_senate.py full
run_stage "Enrichment"  us_congress_enrich.py
run_stage "Aggregation" us_congress_agg.py

SUMMARY=$(grep -E "^DONE:|^congress clusters|^committee conflicts|^convergence ANY|^still actionable|^in universe" "$LOG" | tail -8 | tr '\n' '|')

{
  echo ""
  echo "CONGRESS PIPELINE END $(date)"
} >> "$LOG"

TOKEN=$(cat /root/trueflow/.tgtoken 2>/dev/null)
CHAT=$(cat /root/trueflow/.tgchat 2>/dev/null)
if [ -n "$TOKEN" ] && [ -n "$CHAT" ]; then
  MSG="Congress pipeline $(date +%d-%b)$STATUS%0A%0A$SUMMARY"
  curl -s -X POST "https://api.telegram.org/bot$TOKEN/sendMessage" \
       -d "chat_id=$CHAT" -d "text=$MSG" > /dev/null
fi

# keep 30 days of logs
find /root/trueflow/logs -name "congress_*.log" -mtime +30 -delete 2>/dev/null

exit 0
