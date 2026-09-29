#!/root/trueflow/bin/python
# -*- coding: utf-8 -*-
"""
us_price_alerts.py - watches the price levels you set on US charts

The US twin of price_alerts.py. Same rules: reads active rows from
us_price_alerts, gets the latest price of just those symbols, sends one
Telegram message (TrueFlow US group) when a level is crossed, and marks each
fired alert done so it never repeats.

Differences from India, stated plainly:
  * Prices come from Yahoo (yfinance 1-minute bars) - one request per run,
    whatever the number of alerts. Yahoo can lag by a minute or two.
  * Market hours are 9:30 AM - 4:00 PM New York time, Mon-Fri, checked in
    New York time so daylight-saving changes need no cron edits. US market
    holidays are not skipped - it simply finds no fresh price and does nothing.

USAGE
    /root/trueflow/bin/python us_price_alerts.py            # one pass
    /root/trueflow/bin/python us_price_alerts.py --dry-run  # show, send nothing
    /root/trueflow/bin/python us_price_alerts.py --force    # ignore market hours
"""
import sys, argparse, math
from datetime import datetime
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
OPEN_T, CLOSE_T = (9, 30), (16, 0)


def in_market_hours(now):
    if now.weekday() > 4:
        return False
    t = now.hour * 60 + now.minute
    return OPEN_T[0] * 60 + OPEN_T[1] <= t <= CLOSE_T[0] * 60 + CLOSE_T[1]


def yahoo(sym):
    return sym.replace(".", "-").replace("/", "-")


def last_prices(syms):
    """{symbol: last price} from one yfinance request of 1-minute bars."""
    import yfinance as yf
    data = yf.download([yahoo(s) for s in syms], period="1d", interval="1m",
                       auto_adjust=False, actions=False, group_by="ticker",
                       threads=True, progress=False)
    out = {}
    if data is None or len(data) == 0:
        return out
    multi = getattr(data.columns, "nlevels", 1) > 1
    for s in syms:
        try:
            df = data[yahoo(s)] if multi else data
            col = df["Close"].dropna()
            if len(col):
                v = float(col.iloc[-1])
                if not (math.isnan(v) or math.isinf(v)):
                    out[s] = v
        except Exception:
            pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="run outside market hours")
    args = ap.parse_args()

    now = datetime.now(NY)
    stamp = now.strftime("%H:%M ET")
    if not args.force and not in_market_hours(now):
        return

    try:
        import tf_config as CFG
        from supabase import create_client
    except ImportError as e:
        print("%s  missing package: %s" % (stamp, e))
        return

    try:
        sb = create_client(CFG.SUPABASE_URL, CFG.SUPABASE_KEY)
        rows = (sb.table("us_price_alerts").select("*")
                  .eq("status", "active").limit(500).execute().data) or []
    except Exception as e:
        print("%s  supabase read failed: %s" % (stamp, str(e)[:120]))
        return
    if not rows:
        return

    syms = sorted(set(r["symbol"] for r in rows if r.get("symbol")))
    try:
        px = last_prices(syms)
    except Exception as e:
        print("%s  yahoo failed (%d alerts pending): %s" % (stamp, len(rows), str(e)[:120]))
        return

    fired = []
    for r in rows:
        ltp = px.get(r.get("symbol"))
        try:
            lvl = float(r["level"])
        except (TypeError, ValueError):
            continue
        if ltp is None:
            continue
        above = str(r.get("direction", "above")).lower() == "above"
        if (ltp >= lvl) if above else (ltp <= lvl):
            r["_ltp"] = ltp
            fired.append(r)

    if not fired:
        print("%s  %d alert(s) watched, none hit" % (stamp, len(rows)))
        return

    lines = ["🔔 <b>US price alert</b>"]
    for r in fired:
        ltp, lvl = r["_ltp"], float(r["level"])
        moved = ""
        try:
            if r.get("created_price"):
                moved = "  ·  %+.1f%% since you set it" % ((ltp / float(r["created_price"]) - 1) * 100)
        except (TypeError, ValueError, ZeroDivisionError):
            moved = ""
        lines.append("\n<b>%s</b>  %s $%g\nnow <b>$%.2f</b>%s%s" % (
            r["symbol"],
            "crossed above" if str(r.get("direction", "above")).lower() == "above" else "fell below",
            lvl, ltp, moved, ("\n<i>%s</i>" % r["note"]) if r.get("note") else ""))
    msg = "\n".join(lines)

    if args.dry_run:
        print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
        return

    try:
        import requests
        resp = requests.post("https://api.telegram.org/bot%s/sendMessage" % CFG.TF_ALERTS_TOKEN,
                             data={"chat_id": CFG.TF_US, "text": msg, "parse_mode": "HTML"}, timeout=15)
        if resp.status_code != 200:
            print("%s  telegram %s: %s" % (stamp, resp.status_code, resp.text[:100]))
            return      # leave them active so the next run tries again
    except Exception as e:
        print("%s  telegram failed: %s" % (stamp, str(e)[:100]))
        return

    try:
        for r in fired:
            if r.get("id") is None:
                continue
            (sb.table("us_price_alerts")
               .update({"status": "triggered", "triggered_at": datetime.now(NY).isoformat(),
                        "triggered_price": round(r["_ltp"], 4)})
               .eq("id", r["id"]).execute())
    except Exception as e:
        print("%s  sent, but marking them done failed: %s" % (stamp, str(e)[:120]))

    print("%s  fired %d: %s" % (stamp, len(fired), ", ".join(r["symbol"] for r in fired)))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("FATAL: %s" % e)
        sys.exit(0)
