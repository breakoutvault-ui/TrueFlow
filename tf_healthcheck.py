#!/root/trueflow/bin/python
# -*- coding: utf-8 -*-
"""
tf_healthcheck.py - one nightly check that today's data actually arrived

Runs at 10:20 PM IST (after the last bhav retry) and posts ONE summary to
TrueFlow System, so a silent failure is seen the same evening, not the next
morning. It only READS - nothing is written anywhere except that message.

  India (today, if NSE was open): momentum scan, daily OHLC top-up, MTF scan,
      playbook tracker, F&O bhav OI, tomorrow's Next Day WL, alert engine log,
      BankNifty live-commentary log
  US (the last US session): US scan, US OHLC top-up, US MTF scan, US tracker
  Server: disk space

USAGE
  /root/trueflow/bin/python tf_healthcheck.py --dry-run   # print only
  /root/trueflow/bin/python tf_healthcheck.py             # print + Telegram
"""
import os, sys, argparse, shutil
from datetime import datetime, date, timedelta, timezone
import requests

sys.path.insert(0, "/root/trueflow")
import tf_config as CFG

IST = timezone(timedelta(hours=5, minutes=30))
BASE = "/root/trueflow"
H = {"apikey": CFG.SUPABASE_KEY, "Authorization": "Bearer " + CFG.SUPABASE_KEY}

# same list nse_bhav_fetcher.py uses
NSE_HOLIDAYS = {"2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03",
                "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02",
                "2026-10-20", "2026-11-10", "2026-11-24", "2026-12-25"}
# NYSE full-day closures, 2026
US_HOLIDAYS = {"2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
               "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25"}


def open_day(d, hol):
    return d.weekday() < 5 and str(d) not in hol


def prev_open(d, hol):
    d -= timedelta(days=1)
    while not open_day(d, hol):
        d -= timedelta(days=1)
    return d


def latest(table, col, where=""):
    try:
        r = requests.get("%s/rest/v1/%s?select=%s&order=%s.desc&limit=1%s" % (CFG.SUPABASE_URL, table, col, col, where),
                         headers=H, timeout=45)
        if r.status_code != 200:
            return None, "read failed (%s)" % r.status_code
        j = r.json()
        return (str(j[0][col])[:10] if j else None), None
    except Exception as e:
        return None, "read failed (%s)" % str(e)[:60]


def log_today(name, day_str, market_hours=False):
    """Was the log written today? With market_hours, there must be a line dated
    today between 03:00 and 10:59 UTC (8:30 AM - 4:30 PM IST) - a late-night job
    also writes engine.log, so its file time alone proves nothing."""
    p = os.path.join(BASE, name)
    if not os.path.exists(p):
        return False, "log missing"
    m = datetime.fromtimestamp(os.path.getmtime(p), timezone.utc).date()
    if str(m) < day_str:
        return False, "last written %s" % m
    if not market_hours:
        return True, None
    try:
        with open(p, "rb") as fh:
            fh.seek(max(0, os.path.getsize(p) - 3000000))
            tail = fh.read().decode("utf-8", "ignore")
    except Exception as e:
        return False, "unreadable (%s)" % str(e)[:40]
    for hh in ("03", "04", "05", "06", "07", "08", "09", "10"):
        if "\n%s %s:" % (day_str, hh) in tail:
            return True, None
    return False, "nothing logged during market hours today"


def run(dry):
    now = datetime.now(IST)
    today = now.date()
    utc_today = datetime.now(timezone.utc).date()
    rows = []                         # (status, text)   status: ok / warn / bad / skip

    def add(st, txt):
        rows.append((st, txt))

    # ── India ─────────────────────────────────────────────
    if open_day(today, NSE_HOLIDAYS):
        want = str(today)
        for label, table, col, late_ok in [
            ("Momentum scan", "momentum_stocks", "session_date", False),
            ("Daily OHLC top-up", "daily_ohlc", "d", False),
            ("MTF scan", "momentum_mtf", "session_date", False),
            ("Playbook tracker", "playbook_picks", "session_date", False),
            ("F&O bhav OI", "fo_bhav_oi", "session_date", True),
            ("Shakeout scan", "shakeout_state", "session_date", False),
            ("Market history", "market_history|&market=eq.IN", "d", False),
            ("EP scan", "ep_state", "session_date", False),
        ]:
            table, _, where = table.partition("|")
            got, err = latest(table, col, where)
            if err:
                add("bad", "%s — %s" % (label, err))
            elif got and got >= want:
                add("ok", "%s — %s" % (label, got))
            elif late_ok:
                add("warn", "%s — not in yet (last %s). NSE sometimes publishes late; the 6:30 AM catch-up retries."
                    % (label, got or "none"))
            else:
                add("bad", "%s — last %s, expected %s" % (label, got or "none", want))
        got, err = latest("nextday_picks_v2", "target_date")
        if err:
            add("bad", "Next Day WL — %s" % err)
        elif got and got > want:
            add("ok", "Next Day WL — picks for %s" % got)
        else:
            add("bad", "Next Day WL — no picks for tomorrow (last %s)" % (got or "none"))
        for label, lg, mh in [("Alert engine", "engine.log", True), ("BankNifty commentary", "bnf_commentary.log", False)]:
            ok, why = log_today(lg, str(utc_today), mh)
            add("ok" if ok else "bad", "%s — %s" % (label, "ran today" if ok else why))
    else:
        add("skip", "India — market closed today, nothing expected")

    # ── US: the last completed US session ─────────────────
    us_want = str(prev_open(utc_today, US_HOLIDAYS))
    for label, table, col in [
        ("US scan", "us_momentum_stocks", "session_date"),
        ("US OHLC top-up", "us_daily_ohlc", "d"),
        ("US MTF scan", "us_momentum_mtf", "session_date"),
        ("US playbook tracker", "us_playbook_picks", "session_date"),
        ("US shakeout scan", "us_shakeout_state", "session_date"),
        ("US market history", "market_history|&market=eq.US", "d"),
        ("US EP scan", "us_ep_state", "session_date"),
    ]:
        table, _, where = table.partition("|")
        got, err = latest(table, col, where)
        if err:
            add("bad", "%s — %s" % (label, err))
        elif got and got >= us_want:
            add("ok", "%s — %s" % (label, got))
        else:
            add("bad", "%s — last %s, expected %s" % (label, got or "none", us_want))

    # ── server ────────────────────────────────────────────
    du = shutil.disk_usage("/")
    pct = 100.0 * du.used / du.total
    add("ok" if pct < 80 else ("warn" if pct < 90 else "bad"), "Disk — %.0f%% used" % pct)

    icon = {"ok": "✅", "warn": "⏳", "bad": "❌", "skip": "·"}
    bad = sum(1 for s, _ in rows if s == "bad")
    warn = sum(1 for s, _ in rows if s == "warn")
    head = ("🩺 <b>TrueFlow health · %s</b>\n" % today.strftime("%d %b")) + \
           ("All good ✅" if not bad and not warn else "%d problem(s) · %d waiting" % (bad, warn))
    body = "\n".join("%s %s" % (icon[s], t) for s, t in rows)
    msg = head + "\n\n" + body
    print(msg.replace("<b>", "").replace("</b>", ""))
    if dry:
        print("\nDRY RUN - nothing sent.")
        return
    try:
        requests.post("https://api.telegram.org/bot%s/sendMessage" % CFG.TF_ALERTS_TOKEN,
                      data={"chat_id": CFG.TF_SYSTEM, "text": msg, "parse_mode": "HTML"}, timeout=20)
    except Exception as e:
        print("telegram failed: %s" % e)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    run(ap.parse_args().dry_run)
