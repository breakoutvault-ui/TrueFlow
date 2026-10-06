#!/bin/bash
# india_eod.sh - TrueFlow India evening chain (replaces the scattered India cron lines)
#
#   wave1  (cron 16:00 IST)  Kite-based data, ready ~16:45-17:00 IST
#          momentum scan -> breakout monitor -> OHLC top-up -> MTF -> MTF snapshot
#          -> shakeouts -> market history -> EP -> playbook tracker
#   wave2  (cron 17:30 IST)  NSE-file-based data
#          EOD EMA/delivery -> wait for NSE bhav (F&O OI) -> watchlist -> Next Day WL
#          -> deals -> playbook tracker   (health check runs separately at 20:05 IST)
#
# Every step: runs alone, max 45 min, a failure is reported to TrueFlow System and the
# chain carries on. Two copies of the same wave can never run at once (flock).

WAVE="$1"
cd /root/trueflow || exit 1
PY=/root/trueflow/bin/python
LOG=/root/trueflow/india_eod.log
exec 9>/tmp/india_eod_${WAVE}.lock
flock -n 9 || { echo "$(date -u) $WAVE already running - skipped" >> $LOG; exit 0; }

say() { echo "$(date -u '+%F %T UTC') [$WAVE] $*" >> $LOG; }

tg() {   # message to TrueFlow System
  $PY - "$1" <<'EOF' >> $LOG 2>&1
import sys, requests, tf_config as c
requests.post("https://api.telegram.org/bot%s/sendMessage" % c.TF_ALERTS_TOKEN,
              data={"chat_id": c.TF_SYSTEM, "text": sys.argv[1]}, timeout=20)
EOF
}

FAILED=""
step() {   # step "name" logfile command...
  local name="$1" lf="$2"; shift 2
  local t0=$(date +%s)
  say "START $name"
  timeout "${TMO:-45m}" "$@" >> "$lf" 2>&1
  local rc=$?
  local mins=$(( ($(date +%s) - t0) / 60 ))
  if [ $rc -eq 0 ]; then say "OK    $name (${mins} min)"
  else say "FAIL  $name rc=$rc (${mins} min)"; FAILED="$FAILED\n- $name (code $rc, see $(basename $lf))"; fi
}

trading_day() {   # fail-open: if the check itself breaks, run anyway (each script checks too)
  $PY -c "import nse_bhav_fetcher as b,sys; sys.exit(0 if b.is_trading_day() else 3)" 2>/dev/null
  [ $? -ne 3 ]
}

bhav_in() {   # has today's F&O OI landed in fo_bhav_oi?
  $PY - <<'EOF' 2>/dev/null
import sys, requests, tf_config as c
from datetime import datetime, timedelta, timezone
d = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()
H = {"apikey": c.SUPABASE_KEY, "Authorization": "Bearer " + c.SUPABASE_KEY}
r = requests.get(c.SUPABASE_URL + "/rest/v1/fo_bhav_oi?select=symbol&session_date=eq.%s&limit=1" % d,
                 headers=H, timeout=30)
sys.exit(0 if r.ok and r.json() else 1)
EOF
}

ist_hour() { TZ=Asia/Kolkata date +%H; }

trading_day || { say "not a trading day - nothing to do"; exit 0; }

if [ "$WAVE" = "wave1" ]; then
  T0=$(date +%s)
  step "Momentum scan"        momentum.log          $PY momentum_scan.py
  step "Breakout monitor"     breakout_monitor.log  $PY breakout_monitor.py
  step "OHLC top-up"          ohlc_append.log       $PY ohlc_backfill.py --append
  step "MTF scan"             mtf_scan.log          $PY mtf_scan.py
  step "MTF snapshot"         mtf_snapshot.log      /bin/bash mtf_snapshot.sh
  step "Shakeouts"            shakeout.log          $PY shakeout_scan.py --market IN
  step "Market history"       market_history.log    $PY market_history.py --market IN
  step "EP scan"              ep_scan.log           $PY ep_scan.py --market IN
  step "Playbook tracker"     playbook_track.log    $PY playbook_track.py
  M=$(( ($(date +%s) - T0) / 60 ))
  if [ -n "$FAILED" ]; then tg "$(echo -e "🇮🇳 India wave 1 finished in ${M} min WITH FAILURES:$FAILED")"
  else tg "🇮🇳 India wave 1 done in ${M} min - Screener, Charts, MTF, Shakeouts, EP, Market Sentiment updated."; fi

elif [ "$WAVE" = "wave2" ]; then
  TMO=100m step "EOD EMA + delivery"   eod_ema.log           $PY eod_ema.py
  # wait for NSE bhav copy: fetcher retries itself; we loop until 23:00 IST
  GOT=0; TRIES=0
  bhav_in && GOT=1
  while [ $GOT -eq 0 ]; do
    if [ "$(ist_hour)" -ge 23 ] || [ $TRIES -ge 8 ]; then say "bhav still missing (tries $TRIES)"; break; fi
    TRIES=$((TRIES+1))
    L0=$( { wc -l < bhav_fetcher.log; } 2>/dev/null || echo 0)
    step "NSE bhav fetch #$TRIES" bhav_fetcher.log  $PY nse_bhav_fetcher.py
    # done = the fetcher logged success, or the rows are in Supabase (never re-run after a success)
    tail -n +$((L0+1)) bhav_fetcher.log 2>/dev/null | grep -q "processing complete" && GOT=1
    bhav_in && GOT=1
    [ $GOT -eq 0 ] && sleep 300
  done
  if [ $GOT -eq 1 ]; then say "bhav present"
  else FAILED="$FAILED\n- NSE F&O bhav not published by 23:00 IST (06:30 catch-up will load it; watchlist/Next Day WL used yesterday's OI)"; fi
  step "F&O watchlist"        watchlist.log         $PY watchlist_generator.py
  step "Next Day WL"          nextday.log           $PY nextday_scorer.py
  step "Deals refresh"        /root/trueflow_deals/refresh.log /bin/bash /root/trueflow_deals/daily_refresh.sh
  step "Playbook tracker"     playbook_track.log    $PY playbook_track.py
  if [ -n "$FAILED" ]; then tg "$(echo -e "🇮🇳 India wave 2 finished WITH ISSUES:$FAILED")"
  else tg "🇮🇳 India wave 2 done at $(TZ=Asia/Kolkata date +%H:%M) IST - bhav/OI, delivery, watchlist, Next Day WL, deals updated."; fi
else
  echo "usage: india_eod.sh wave1|wave2"; exit 1
fi
