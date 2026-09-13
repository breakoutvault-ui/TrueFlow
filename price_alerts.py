#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
price_alerts.py — watches the price levels you set from the dashboard

Every run (cron: every 2 minutes, 9:15 AM to 3:30 PM IST on weekdays) it reads
the alerts you created in the stock card, asks Kite for the last price of just
those symbols in one call, and sends a Telegram message the moment a level is
crossed. Each alert fires once and then marks itself done.

  * One Kite call per run, whatever the number of alerts (up to 500 symbols).
  * Nothing is checked outside market hours, so it costs nothing overnight.
  * A missing token or a Kite hiccup is logged and skipped, never crashes cron.

USAGE
    /root/trueflow/bin/python price_alerts.py            # one pass
    /root/trueflow/bin/python price_alerts.py --dry-run  # show, send nothing
    /root/trueflow/bin/python price_alerts.py --force    # ignore market hours
"""

import sys
import argparse
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
OPEN_T = (9, 15)
CLOSE_T = (15, 30)
BATCH = 450          # Kite accepts up to 500 instruments per ltp() call


def in_market_hours(now):
    if now.weekday() > 4:
        return False
    t = now.hour * 60 + now.minute
    return (OPEN_T[0] * 60 + OPEN_T[1]) <= t <= (CLOSE_T[0] * 60 + CLOSE_T[1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="run outside market hours")
    args = ap.parse_args()

    now = datetime.now(IST)
    if not args.force and not in_market_hours(now):
        return

    try:
        import tf_config as CFG
        from kiteconnect import KiteConnect
        from supabase import create_client
    except ImportError as e:
        print("%s  missing package: %s" % (now.strftime("%H:%M"), e))
        return

    try:
        sb = create_client(CFG.SUPABASE_URL, CFG.SUPABASE_KEY)
        rows = (sb.table("price_alerts").select("*")
                  .eq("status", "active").limit(500).execute().data) or []
    except Exception as e:
        print("%s  supabase read failed: %s" % (now.strftime("%H:%M"), str(e)[:120]))
        return

    if not rows:
        return

    syms = sorted(set(r["symbol"] for r in rows if r.get("symbol")))
    keys = ["NSE:%s" % s for s in syms]

    try:
        kite = KiteConnect(api_key=CFG.KITE_API_KEY)
        tp = getattr(CFG, "ACCESS_TOKEN_PATH", "/root/trueflow/access_token.txt")
        with open(tp) as fh:
            kite.set_access_token(fh.read().strip())
        quotes = {}
        for i in range(0, len(keys), BATCH):
            quotes.update(kite.ltp(keys[i:i + BATCH]) or {})
    except Exception as e:
        print("%s  kite failed (%d alerts pending): %s"
              % (now.strftime("%H:%M"), len(rows), str(e)[:120]))
        return

    fired = []
    for r in rows:
        q = quotes.get("NSE:%s" % r["symbol"])
        if not q:
            continue
        ltp = q.get("last_price")
        try:
            lvl = float(r["level"])
        except (TypeError, ValueError):
            continue
        if ltp is None:
            continue
        above = str(r.get("direction", "above")).lower() == "above"
        hit = (ltp >= lvl) if above else (ltp <= lvl)
        if hit:
            r["_ltp"] = ltp
            fired.append(r)

    if not fired:
        print("%s  %d alert(s) watched, none hit" % (now.strftime("%H:%M"), len(rows)))
        return

    lines = ["🔔 <b>Price alert</b>"]
    for r in fired:
        ltp, lvl = r["_ltp"], float(r["level"])
        base = r.get("created_price")
        moved = ""
        if base:
            try:
                moved = "  ·  %+.1f%% since you set it" % ((ltp / float(base) - 1) * 100)
            except (TypeError, ValueError, ZeroDivisionError):
                moved = ""
        lines.append("\n<b>%s</b>  %s %s\nnow <b>%s</b>%s%s" % (
            r["symbol"],
            "crossed above" if str(r.get("direction", "above")).lower() == "above" else "fell below",
            ("%g" % lvl), ("%g" % ltp), moved,
            ("\n<i>%s</i>" % r["note"]) if r.get("note") else ""))
    msg = "\n".join(lines)

    if args.dry_run:
        print(msg.replace("<b>", "").replace("</b>", "")
                 .replace("<i>", "").replace("</i>", ""))
        return

    try:
        import requests
        requests.post("https://api.telegram.org/bot%s/sendMessage" % CFG.TELEGRAM_TOKEN,
                      data={"chat_id": CFG.TELEGRAM_CHAT_ID, "text": msg,
                            "parse_mode": "HTML"}, timeout=15)
    except Exception as e:
        print("%s  telegram failed: %s" % (now.strftime("%H:%M"), str(e)[:100]))
        return          # leave them active so the next run tries again

    try:
        ids = [r["id"] for r in fired if r.get("id") is not None]
        if ids:
            (sb.table("price_alerts")
               .update({"status": "triggered",
                        "triggered_at": now.isoformat(),
                        "triggered_price": None})
               .in_("id", ids).execute())
            for r in fired:
                if r.get("id") is None:
                    continue
                (sb.table("price_alerts")
                   .update({"triggered_price": r["_ltp"]})
                   .eq("id", r["id"]).execute())
    except Exception as e:
        print("%s  sent, but marking them done failed: %s" % (now.strftime("%H:%M"), str(e)[:120]))

    print("%s  fired %d: %s" % (now.strftime("%H:%M"), len(fired),
                                ", ".join(r["symbol"] for r in fired)))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("FATAL: %s" % e)
        sys.exit(0)          # never let cron see a failure loop
