#!/bin/bash
# ════════════════════════════════════════════════════════════════════
# mtf_snapshot.sh — copy momentum_mtf into momentum_mtf_hist, dated
#
#   momentum_mtf is overwritten by mtf_scan.py every night. Without this
#   there is no weekly/monthly history at all, which is why the Alignment
#   and Smart Money playbooks cannot be audited and never will be for any
#   date before this script first runs.
#
#   Runs AFTER mtf_scan.py (15:00 UTC) so it captures the fresh numbers.
#
#   Idempotent: ON CONFLICT DO NOTHING, so running it twice in a day is
#   harmless and a missed day simply stays missed.
#
# Cron: 30 15 * * 1-5   (9:00 PM IST, after the 8:30 PM MTF scan)
# Log:  /root/trueflow/mtf_snapshot.log
# ════════════════════════════════════════════════════════════════════
set -u

HOST=aws-1-ap-south-1.pooler.supabase.com
PORT=5432
DBUSER=postgres.tsgltaqbxtisebqmbffg
DB=postgres
BIN=/usr/lib/postgresql/17/bin
PSQL="$BIN/psql -h $HOST -p $PORT -U $DBUSER -d $DB -w -v ON_ERROR_STOP=1"

say() { echo "$(TZ=Asia/Kolkata date '+%Y-%m-%d %H:%M IST')  $*"; }

alert() {
  cd /root/trueflow 2>/dev/null && /root/trueflow/bin/python - "$1" <<'PY' >/dev/null 2>&1
import sys
try:
    import tf_config as c, requests
    requests.post("https://api.telegram.org/bot%s/sendMessage" % c.TELEGRAM_TOKEN,
                  data={"chat_id": c.TELEGRAM_CHAT_ID,
                        "text": "⚠️ MTF snapshot problem\n" + sys.argv[1]}, timeout=15)
except Exception:
    pass
PY
}

fail() { say "FAILED: $1"; alert "$1"; exit 1; }

[ -x "$BIN/psql" ] || fail "psql 17 not installed"
[ -f /root/.pgpass ] || fail "/root/.pgpass missing"

# Refuse to snapshot a table the scan has emptied or half-written.
LIVE=$($PSQL -Atc "select count(*) from momentum_mtf" 2>/dev/null) \
  || fail "could not read momentum_mtf"
[ "$LIVE" -ge 500 ] 2>/dev/null \
  || fail "momentum_mtf holds only $LIVE rows — the scan looks incomplete, nothing captured"

# momentum_mtf carries its own session_date - keep it rather than stamping
# the calendar date, so a scan finishing after midnight UTC is still filed
# under the session it actually belongs to.
$PSQL -c "insert into momentum_mtf_hist select * from momentum_mtf
          where session_date is not null
          on conflict (symbol, session_date) do nothing" >/dev/null \
  || fail "insert failed"

LATEST=$($PSQL -Atc "select max(session_date) from momentum_mtf")
GOT=$($PSQL -Atc "select count(*) from momentum_mtf_hist where session_date = '$LATEST'")
DAYS=$($PSQL -Atc "select count(distinct session_date) from momentum_mtf_hist")
SIZE=$($PSQL -Atc "select pg_size_pretty(pg_total_relation_size('momentum_mtf_hist'))")

say "captured $GOT of $LIVE rows for $LATEST · $DAYS days of history · $SIZE"
