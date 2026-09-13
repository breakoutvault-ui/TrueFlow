#!/bin/bash
# ════════════════════════════════════════════════════════════════════
# db_archive.sh — keep Supabase small without ever losing history
#
#   For each table below: everything older than KEEP sessions is exported
#   to a compressed CSV on this server, verified row-for-row, and only
#   then deleted from the database.
#
#   Nothing is lost. Old sessions stop being queryable from the dashboard
#   and live in /root/db_archive instead — which the Sunday DigitalOcean
#   backup picks up, so they are off-server too.
#
#   Roughly 0.1 MB per session compressed: ten years of both markets is
#   about 700 MB on a disk with 16 GB free.
#
# SAFETY
#   * Exports first, counts the rows in the gzip, compares with the
#     database, and aborts the delete if they differ by even one row.
#   * Deletes month by month, so a failure can never half-empty a table.
#   * Refuses to run if the disk is over 85% full.
#   * Telegram alert on any failure.
#
# Cron: 0 0 * * *   (5:30 AM IST, before the 6:00 AM database backup)
# Log:  /root/trueflow/db_archive.log
# ════════════════════════════════════════════════════════════════════
set -u

DIR=/root/db_archive
HOST=aws-1-ap-south-1.pooler.supabase.com
PORT=5432
DBUSER=postgres.tsgltaqbxtisebqmbffg
DB=postgres
BIN=/usr/lib/postgresql/17/bin
PSQL="$BIN/psql -h $HOST -p $PORT -U $DBUSER -d $DB -w -v ON_ERROR_STOP=1"

# table : date column : sessions kept live in Supabase
TABLES=(
  "momentum_stocks:session_date:400"
  "us_momentum_stocks:session_date:100"
)

say() { echo "$(TZ=Asia/Kolkata date '+%Y-%m-%d %H:%M IST')  $*"; }

alert() {
  cd /root/trueflow 2>/dev/null && /root/trueflow/bin/python - "$1" <<'PY' >/dev/null 2>&1
import sys
try:
    import tf_config as c, requests
    requests.post("https://api.telegram.org/bot%s/sendMessage" % c.TELEGRAM_TOKEN,
                  data={"chat_id": c.TELEGRAM_CHAT_ID,
                        "text": "⚠️ DB archive problem\n" + sys.argv[1]}, timeout=15)
except Exception:
    pass
PY
}

fail() { say "FAILED: $1"; alert "$1"; exit 1; }

[ -x "$BIN/psql" ] || fail "psql 17 not installed"
[ -f /root/.pgpass ] || fail "/root/.pgpass missing"
mkdir -p "$DIR" && chmod 700 "$DIR"

USED=$(df --output=pcent / | tail -1 | tr -dc '0-9')
[ "$USED" -lt 85 ] || fail "disk ${USED}% full — archive skipped"

TOTAL_ROWS=0
for SPEC in "${TABLES[@]}"; do
  TBL="${SPEC%%:*}"; REST="${SPEC#*:}"; COL="${REST%%:*}"; KEEP="${REST##*:}"

  CUT=$($PSQL -Atc "select $COL from (select distinct $COL from $TBL order by 1 desc offset $KEEP limit 1) x" 2>/dev/null)
  if [ -z "$CUT" ]; then
    say "$TBL: under $KEEP sessions, nothing to archive"
    continue
  fi

  # oldest month still holding data, so each pass moves one month at a time
  while : ; do
    MIN=$($PSQL -Atc "select min($COL) from $TBL where $COL < '$CUT'" 2>/dev/null)
    [ -z "$MIN" ] || [ "$MIN" = "" ] && break
    MEND=$(date -d "$MIN +1 month" +%Y-%m-01 2>/dev/null) || fail "date arithmetic failed on $MIN"
    [ "$MEND" \> "$CUT" ] && MEND="$CUT"
    MSTART=$(date -d "$MIN" +%Y-%m-01)

    N=$($PSQL -Atc "select count(*) from $TBL where $COL >= '$MSTART' and $COL < '$MEND'")
    [ "$N" -gt 0 ] 2>/dev/null || break

    OUT="$DIR/${TBL}_${MSTART}.csv.gz"
    say "$TBL: archiving $MSTART to $MEND ($N rows) -> $(basename "$OUT")"

    $PSQL -c "\copy (select * from $TBL where $COL >= '$MSTART' and $COL < '$MEND' order by $COL) to stdout with csv header" \
      2> /tmp/db_archive.err | gzip -9 > "$OUT.part"
    [ "${PIPESTATUS[0]}" -eq 0 ] || fail "$TBL export error: $(tail -c 200 /tmp/db_archive.err)"

    LINES=$(gunzip -c "$OUT.part" 2>/dev/null | wc -l)
    GOT=$((LINES - 1))                     # drop the header row
    if [ "$GOT" -ne "$N" ]; then
      rm -f "$OUT.part"
      fail "$TBL $MSTART: exported $GOT rows but the database holds $N — nothing deleted"
    fi

    mv "$OUT.part" "$OUT" && chmod 600 "$OUT"
    $PSQL -c "delete from $TBL where $COL >= '$MSTART' and $COL < '$MEND'" >/dev/null \
      || fail "$TBL $MSTART: exported fine but the delete failed (archive file kept)"

    TOTAL_ROWS=$((TOTAL_ROWS + N))
    say "$TBL: $MSTART done — $N rows archived and removed ($(du -h "$OUT" | cut -f1))"
  done

  LEFT=$($PSQL -Atc "select count(distinct $COL) from $TBL")
  say "$TBL: $LEFT sessions live in Supabase (keeping $KEEP)"
done

SIZE=$($PSQL -Atc "select pg_size_pretty(pg_database_size(current_database()))")
FILES=$(ls -1 "$DIR"/*.csv.gz 2>/dev/null | wc -l)
ARCH=$(du -sh "$DIR" 2>/dev/null | cut -f1)
say "done — $TOTAL_ROWS rows archived this run · database now $SIZE · archive $FILES files, $ARCH"
