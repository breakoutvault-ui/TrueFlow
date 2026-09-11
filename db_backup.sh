#!/bin/bash
# ════════════════════════════════════════════════════════════════════
# db_backup.sh — nightly backup of the TrueFlow Supabase database
#
#   * Full copy of every table in the `public` schema (all TrueFlow data)
#   * PostgreSQL custom format, compressed, restorable table by table
#   * Keeps the last 7 in /root/db_backups (the Sunday DigitalOcean backup
#     copies them off the server)
#   * Verified after writing; Telegram alert only if something fails
#   * Password is read from /root/.pgpass (root-only) — never stored here
#
# Cron: 30 0 * * *   (6:00 AM IST)
# Log:  /root/trueflow/db_backup.log
# ════════════════════════════════════════════════════════════════════
set -u

DIR=/root/db_backups
KEEP=7
HOST=aws-1-ap-south-1.pooler.supabase.com
PORT=5432
DBUSER=postgres.tsgltaqbxtisebqmbffg
DB=postgres
BIN=/usr/lib/postgresql/17/bin
MIN_TABLES=30                       # fewer tables than this = something is wrong

STAMP=$(TZ=Asia/Kolkata date +%Y-%m-%d_%H%M)
OUT="$DIR/supabase_$STAMP.dump"
T0=$(date +%s)

say() { echo "$(TZ=Asia/Kolkata date '+%Y-%m-%d %H:%M IST')  $*"; }

alert() {
  cd /root/trueflow 2>/dev/null && /root/trueflow/bin/python - "$1" <<'PY' >/dev/null 2>&1
import sys
try:
    import tf_config as c, requests
    requests.post("https://api.telegram.org/bot%s/sendMessage" % c.TELEGRAM_TOKEN,
                  data={"chat_id": c.TELEGRAM_CHAT_ID,
                        "text": "⚠️ DB backup FAILED\n" + sys.argv[1]}, timeout=15)
except Exception:
    pass
PY
}

fail() {
  say "FAILED: $1"
  rm -f "$OUT.part"
  alert "$1"
  exit 1
}

[ -x "$BIN/pg_dump" ] || fail "pg_dump 17 not installed at $BIN"
[ -f /root/.pgpass ]  || fail "/root/.pgpass missing (database password)"
mkdir -p "$DIR" && chmod 700 "$DIR"

say "start -> $OUT"
"$BIN/pg_dump" -h "$HOST" -p "$PORT" -U "$DBUSER" -d "$DB" -w \
  -n public -Fc --no-owner --no-privileges -f "$OUT.part" 2> /tmp/db_backup.err \
  || fail "pg_dump error: $(tail -c 300 /tmp/db_backup.err)"

TABLES=$("$BIN/pg_restore" -l "$OUT.part" 2>/dev/null | grep -c " TABLE DATA public ")
[ "$TABLES" -ge "$MIN_TABLES" ] || fail "only $TABLES tables in the copy (expected $MIN_TABLES+)"

mv "$OUT.part" "$OUT" && chmod 600 "$OUT"
SIZE=$(du -h "$OUT" | cut -f1)

ls -1t "$DIR"/supabase_*.dump 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f --
LEFT=$(ls -1 "$DIR"/supabase_*.dump 2>/dev/null | wc -l)

say "OK  $SIZE  $TABLES tables  $(( $(date +%s) - T0 ))s  (keeping $LEFT)"
