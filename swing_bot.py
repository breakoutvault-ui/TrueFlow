#!/usr/bin/env python3
# ==============================================================
#  TrueFlow - Swing Execution Bot  (Phase 1)
#  Reads triggers -> works out stop + size -> asks on Telegram ->
#  places the order and its GTT stop only after you tap Confirm.
#
#  IT NEVER BUYS ANYTHING ON ITS OWN.
#
#  Run:  cd /root/trueflow && ./bin/python swing_bot.py
#  Cron: * 3-10 * * 1-5  cd /root/trueflow && ./bin/python swing_bot.py
#        (a lock file means only one copy ever runs; if it dies the
#         next minute restarts it clean)
#
#  SAFETY: DRY_RUN below is True. Nothing reaches Zerodha while it is
#  True - you still get every Telegram message, so you can watch it
#  work for a few days before it can spend a rupee.
# ==============================================================

import os, sys, json, time, fcntl, traceback
from datetime import datetime, date, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def now_ist():
    """The droplet's clock is UTC. Every time decision here is IST."""
    return datetime.now(timezone.utc).astimezone(IST)


def today_ist():
    return now_ist().date()

# ---------------- YOUR SETTINGS -------------------------------
DRY_RUN            = True     # <<< set to False only when you are ready

RISK_RUPEES        = 1250     # 5% of a 25,000 test account
MAX_OPEN_POSITIONS = 2
DAILY_LOSS_CAP     = 2500     # bot halts for the day past this
MAX_TRADES_PER_DAY = 3

# stop ladder, as a fraction of the stock's ADR
STOP_MIN_ADR       = 0.40     # anything tighter than this gets skipped over
STOP_MAX_ADR       = 1.20     # if even the widest low is further, no trade
BO_CANDLE_INTERVAL = "15minute"

# how far price may run past your level before the bot refuses
DRIFT_MAX_R        = 0.33     # a third of the way to the stop
DRIFT_MAX_PCT      = 2.0      # and never more than 2%, whichever is smaller

CONFIRM_WINDOW_SEC = 300      # the Confirm button dies after 5 minutes
ENTRY_BUFFER_PCT   = 0.15     # limit price sits this % above the trigger

TRIGGER_POLL_SEC   = 20       # how often new triggers are picked up
SESSION_START      = (9, 14)
SESSION_END        = (15, 30)

HALT_FILE          = "/root/trueflow/SWING_HALT"   # create this file = bot stops
LOCK_FILE          = "/tmp/swing_bot.lock"
OFFSET_FILE        = "/root/trueflow/swing_tg_offset.txt"
LOG_FILE           = "/root/trueflow/swing_bot.log"
# --------------------------------------------------------------

sys.path.insert(0, "/root/trueflow")
import requests

try:
    import tf_config as CFG
except Exception as e:
    print("FATAL: cannot import tf_config.py -", e); sys.exit(1)


def cfg(*names, default=None):
    """tf_config's variable names are read defensively so nothing is guessed."""
    for n in names:
        v = getattr(CFG, n, None)
        if v:
            return v
    return default


KITE_KEY   = cfg("KITE_API_KEY", "API_KEY", "KITE_KEY", "KEY")
TOKEN_PATH = cfg("ACCESS_TOKEN_PATH", "TOKEN_PATH", "KITE_TOKEN_PATH",
                 default="/root/trueflow/access_token.txt")
SB_URL     = cfg("SUPABASE_URL", "SB_URL")
SB_KEY     = cfg("SUPABASE_KEY", "SUPABASE_ANON_KEY", "SB_KEY", "ANON_KEY")
TG_TOKEN   = cfg("TELEGRAM_TOKEN", "TG_TOKEN", "TELEGRAM_BOT_TOKEN")
TG_CHAT    = cfg("TELEGRAM_CHAT_ID", "TG_CHAT_ID", "CHAT_ID")

for label, val in [("Kite key", KITE_KEY), ("Supabase URL", SB_URL),
                   ("Supabase key", SB_KEY), ("Telegram token", TG_TOKEN),
                   ("Telegram chat", TG_CHAT)]:
    if not val:
        print("FATAL: %s not found in tf_config.py" % label); sys.exit(1)


def log(msg):
    line = "%s  %s" % (now_ist().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------- Supabase ------------------------------------
SB_H = {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY,
        "Content-Type": "application/json"}


def sb_get(table, query=""):
    r = requests.get("%s/rest/v1/%s?%s" % (SB_URL, table, query),
                     headers=SB_H, timeout=15)
    r.raise_for_status()
    return r.json()


def sb_patch(table, query, body):
    r = requests.patch("%s/rest/v1/%s?%s" % (SB_URL, table, query),
                       headers=dict(SB_H, Prefer="return=representation"),
                       json=body, timeout=15)
    r.raise_for_status()
    return r.json()


def sb_insert(table, body):
    r = requests.post("%s/rest/v1/%s" % (SB_URL, table),
                      headers=dict(SB_H, Prefer="return=representation"),
                      json=body, timeout=15)
    r.raise_for_status()
    return r.json()


# ---------------- Telegram ------------------------------------
TG_API = "https://api.telegram.org/bot%s/" % TG_TOKEN


def tg_send(text, buttons=None):
    body = {"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}
    if buttons:
        body["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    try:
        r = requests.post(TG_API + "sendMessage", json=body, timeout=15).json()
        return r.get("result", {}).get("message_id")
    except Exception as e:
        log("telegram send failed: %s" % e)
        return None


def tg_edit(message_id, text):
    try:
        requests.post(TG_API + "editMessageText",
                      json={"chat_id": TG_CHAT, "message_id": message_id,
                            "text": text, "parse_mode": "HTML"}, timeout=15)
    except Exception as e:
        log("telegram edit failed: %s" % e)


def tg_answer(callback_id, text=""):
    try:
        requests.post(TG_API + "answerCallbackQuery",
                      json={"callback_query_id": callback_id, "text": text},
                      timeout=10)
    except Exception:
        pass


def tg_updates():
    """Long-poll for button taps. Outbound only - no open ports needed."""
    try:
        off = 0
        if os.path.exists(OFFSET_FILE):
            off = int(open(OFFSET_FILE).read().strip() or 0)
        r = requests.get(TG_API + "getUpdates",
                         params={"offset": off + 1, "timeout": 3},
                         timeout=12).json()
        ups = r.get("result", [])
        if ups:
            with open(OFFSET_FILE, "w") as f:
                f.write(str(ups[-1]["update_id"]))
        return ups
    except Exception as e:
        log("telegram poll failed: %s" % e)
        return []


# ---------------- Kite ----------------------------------------
from kiteconnect import KiteConnect

_kite = None


def kite():
    global _kite
    if _kite is None:
        tok = open(TOKEN_PATH).read().strip()
        k = KiteConnect(api_key=KITE_KEY)
        k.set_access_token(tok)
        _kite = k
    return _kite


def quote_of(symbol):
    d = kite().ltp(["NSE:" + symbol])["NSE:" + symbol]
    return d["last_price"], d["instrument_token"]


def candles(token, interval, days):
    to = now_ist()
    frm = to - timedelta(days=days)
    return kite().historical_data(token, frm, to, interval)


def adr_pct(daily, n=20):
    rows = [c for c in daily if c.get("close")][-n:]
    if len(rows) < 5:
        return None
    vals = [(c["high"] - c["low"]) / c["close"] * 100.0 for c in rows if c["close"]]
    return round(sum(vals) / len(vals), 2) if vals else None


def cash_available():
    try:
        m = kite().margins("equity")["available"]
        for k in ("live_balance", "cash", "opening_balance"):
            if m.get(k) is not None:
                return float(m[k])
    except Exception as e:
        log("margins failed: %s" % e)
    return 0.0


# ---------------- the stop ladder -----------------------------
def work_out_stop(symbol, entry):
    """
    Tightest sensible low first, widening only when the tighter one is
    too close. Returns (stop, source, adr) or (None, reason, adr).
    """
    try:
        _, token = quote_of(symbol)
        daily = candles(token, "day", 60)
        intr = candles(token, BO_CANDLE_INTERVAL, 3)
    except Exception as e:
        return None, "could not read candles (%s)" % e, None

    adr = adr_pct(daily)
    if not adr:
        return None, "not enough history to measure ADR", None

    today = today_ist()
    today_bars = [c for c in intr if c["date"].date() == today]
    if not today_bars:
        return None, "no candles for today yet", adr

    bo_low = today_bars[-1]["low"]                      # the candle it broke out on
    day_low = min(c["low"] for c in today_bars)         # lowest since the open
    prev = [c for c in daily if c["date"].date() < today]
    prev_low = prev[-1]["low"] if prev else None

    min_gap = entry * (STOP_MIN_ADR * adr) / 100.0
    max_gap = entry * (STOP_MAX_ADR * adr) / 100.0

    # rank by real distance - yesterday's low can sit ABOVE today's, so the
    # three are not reliably in order and must not be assumed to be
    cands = [(entry - low, low, name)
             for low, name in ((bo_low, "bo_candle"), (day_low, "day_low"),
                               (prev_low, "prev_day_low"))
             if low is not None and low < entry]
    cands.sort()
    for gap, low, name in cands:
        if gap < min_gap:
            continue                     # too tight, it would get whipped out
        if gap > max_gap:
            break                        # the rest are wider still
        return round(low, 2), name, adr

    return None, ("no low sits between %.0f%% and %.0f%% of ADR (%.2f%%) "
                  "- risk/reward is not there" %
                  (STOP_MIN_ADR * 100, STOP_MAX_ADR * 100, adr)), adr


def work_out_size(entry, stop):
    per_share = entry - stop
    if per_share <= 0:
        return 0, 0.0
    by_risk = int(RISK_RUPEES // per_share)
    by_cash = int(cash_available() // entry)
    qty = max(0, min(by_risk, by_cash))
    return qty, round(qty * per_share, 2)


# ---------------- day state / caps ----------------------------
def day_row():
    today = today_ist().isoformat()
    rows = sb_get("swing_day", "day=eq.%s" % today)
    if rows:
        return rows[0]
    return sb_insert("swing_day", {"day": today})[0]


def blocked_reason():
    if os.path.exists(HALT_FILE):
        return "kill switch file is present"
    d = day_row()
    if d.get("halted"):
        return d.get("halt_reason") or "halted for the day"
    if float(d.get("realised_pnl") or 0) <= -abs(DAILY_LOSS_CAP):
        return "daily loss cap hit"
    if int(d.get("trades_taken") or 0) >= MAX_TRADES_PER_DAY:
        return "max trades for the day reached"
    open_n = len(sb_get("swing_orders", "status=in.(sent,complete,partial)"
                                        "&closed_at=is.null&select=id"))
    if open_n >= MAX_OPEN_POSITIONS:
        return "already holding %d positions" % open_n
    return None


# ---------------- offering a trade ----------------------------
def offer(t):
    sym = t["symbol"]
    why = blocked_reason()
    if why:
        sb_patch("swing_triggers", "id=eq.%s" % t["id"],
                 {"status": "rejected", "reject_reason": why,
                  "decided_at": now_ist().isoformat()})
        tg_send("\u26d4 <b>%s</b> skipped - %s" % (sym, why))
        return

    try:
        ltp, _ = quote_of(sym)
    except Exception as e:
        sb_patch("swing_triggers", "id=eq.%s" % t["id"],
                 {"status": "rejected", "reject_reason": "no price (%s)" % e})
        return

    level = float(t.get("alert_level") or t.get("trigger_price") or ltp)
    stop, src, adr = work_out_stop(sym, level)
    if stop is None:
        sb_patch("swing_triggers", "id=eq.%s" % t["id"],
                 {"status": "rejected", "reject_reason": src, "adr_pct": adr,
                  "decided_at": now_ist().isoformat()})
        tg_send("\u26d4 <b>%s</b> skipped - %s" % (sym, src))
        return

    qty, risk = work_out_size(level, stop)
    if qty < 1:
        msg = "cannot afford even 1 share (need Rs %.0f, have Rs %.0f)" % (
            level, cash_available())
        sb_patch("swing_triggers", "id=eq.%s" % t["id"],
                 {"status": "rejected", "reject_reason": msg, "stop": stop,
                  "stop_source": src, "adr_pct": adr,
                  "decided_at": now_ist().isoformat()})
        tg_send("\u26d4 <b>%s</b> skipped - %s" % (sym, msg))
        return

    drift_ok, drift_note = check_drift(level, stop, ltp)
    tag = "" if drift_ok else "\n\u26a0\ufe0f <b>%s</b>" % drift_note

    txt = ("\U0001f7e2 <b>%s</b>%s\n"
           "Level  <b>%.2f</b>   Now  <b>%.2f</b>\n"
           "Stop   <b>%.2f</b>  (%s)\n"
           "Qty    <b>%d</b>   Cost  Rs %.0f\n"
           "Risk   <b>Rs %.0f</b>   ADR %.2f%%%s%s") % (
        sym, "  [DRY RUN]" if DRY_RUN else "", level, ltp, stop,
        src.replace("_", " "), qty, qty * level, risk, adr, tag,
        "\n\nExpires in %d min." % (CONFIRM_WINDOW_SEC // 60))

    mid = tg_send(txt, buttons=[[
        {"text": "\u2705 Confirm", "callback_data": "Y:%s" % t["id"]},
        {"text": "\u274c Skip", "callback_data": "N:%s" % t["id"]}]])

    sb_patch("swing_triggers", "id=eq.%s" % t["id"],
             {"status": "offered", "entry": level, "stop": stop,
              "stop_source": src, "qty": qty, "risk_rupees": risk,
              "adr_pct": adr, "tg_message_id": mid,
              "offered_at": now_ist().isoformat()})
    log("offered %s qty=%d stop=%.2f (%s)" % (sym, qty, stop, src))


def check_drift(entry, stop, ltp):
    if ltp <= entry:
        return True, ""
    allowed = min((entry - stop) * DRIFT_MAX_R, entry * DRIFT_MAX_PCT / 100.0)
    if (ltp - entry) <= allowed:
        return True, ""
    return False, ("price has run %.2f past your level - more than the %.2f allowed"
                   % (ltp - entry, allowed))


# ---------------- acting on a tap -----------------------------
def on_confirm(t):
    sym = t["symbol"]
    why = blocked_reason()
    if why:
        return False, "refused - %s" % why

    try:
        ltp, _ = quote_of(sym)
    except Exception as e:
        return False, "refused - could not read price (%s)" % e

    entry, stop = float(t["entry"]), float(t["stop"])
    ok, note = check_drift(entry, stop, ltp)
    if not ok:
        return False, "refused - " + note

    qty, risk = work_out_size(entry, stop)
    if qty < 1:
        return False, "refused - not enough cash any more"

    limit = round(max(ltp, entry) * (1 + ENTRY_BUFFER_PCT / 100.0), 1)

    if DRY_RUN:
        sb_insert("swing_orders", {
            "trigger_id": t["id"], "symbol": sym, "qty": qty,
            "limit_price": limit, "stop_price": stop,
            "status": "sent", "error": "DRY RUN - not sent to Zerodha"})
        return True, ("DRY RUN - would buy %d %s at limit %.2f, stop %.2f (risk Rs %.0f)"
                      % (qty, sym, limit, stop, risk))

    try:
        k = kite()
        oid = k.place_order(
            variety=k.VARIETY_REGULAR, exchange="NSE", tradingsymbol=sym,
            transaction_type=k.TRANSACTION_TYPE_BUY, quantity=qty,
            product=k.PRODUCT_CNC, order_type=k.ORDER_TYPE_LIMIT,
            price=limit, validity=k.VALIDITY_DAY, tag="tfswing")
    except Exception as e:
        sb_insert("swing_orders", {"trigger_id": t["id"], "symbol": sym,
                                   "qty": qty, "limit_price": limit,
                                   "status": "rejected", "error": str(e)[:400]})
        return False, "Zerodha rejected it: %s" % e

    row = sb_insert("swing_orders", {
        "trigger_id": t["id"], "symbol": sym, "qty": qty, "limit_price": limit,
        "stop_price": stop, "kite_order_id": str(oid), "status": "sent"})[0]

    filled, avg = wait_for_fill(oid)
    if filled < 1:
        sb_patch("swing_orders", "id=eq.%s" % row["id"],
                 {"status": "sent", "error": "no fill yet - stop NOT placed"})
        return True, ("order %s is in but not filled yet. I will not place the "
                      "stop until it fills - watch it." % oid)

    sb_patch("swing_orders", "id=eq.%s" % row["id"],
             {"status": "complete" if filled == qty else "partial",
              "filled_qty": filled, "avg_price": avg})

    gtt_id, gerr = place_stop(sym, filled, stop)
    d = day_row()
    sb_patch("swing_day", "day=eq.%s" % d["day"],
             {"trades_taken": int(d.get("trades_taken") or 0) + 1})

    if gtt_id:
        sb_patch("swing_orders", "id=eq.%s" % row["id"],
                 {"gtt_id": str(gtt_id), "gtt_status": "active"})
        return True, ("bought %d %s at %.2f. Stop placed at %.2f (GTT %s)."
                      % (filled, sym, avg, stop, gtt_id))
    return True, ("bought %d %s at %.2f but the STOP FAILED (%s). "
                  "PLACE IT MANUALLY NOW." % (filled, sym, avg, gerr))


def wait_for_fill(oid, seconds=45):
    end = time.time() + seconds
    while time.time() < end:
        try:
            h = kite().order_history(oid)
            if h:
                last = h[-1]
                if last["status"] == "COMPLETE":
                    return int(last["filled_quantity"]), float(last["average_price"])
                if last["status"] in ("REJECTED", "CANCELLED"):
                    return 0, 0.0
                if int(last.get("filled_quantity") or 0) > 0:
                    return int(last["filled_quantity"]), float(last["average_price"])
        except Exception as e:
            log("order_history failed: %s" % e)
        time.sleep(3)
    return 0, 0.0


def place_stop(symbol, qty, stop):
    try:
        k = kite()
        ltp, _ = quote_of(symbol)
        gid = k.place_gtt(
            trigger_type=k.GTT_TYPE_SINGLE, tradingsymbol=symbol, exchange="NSE",
            trigger_values=[stop], last_price=ltp,
            orders=[{"transaction_type": k.TRANSACTION_TYPE_SELL,
                     "quantity": qty, "order_type": k.ORDER_TYPE_LIMIT,
                     "product": k.PRODUCT_CNC,
                     "price": round(stop * 0.995, 1)}])
        return gid, None
    except Exception as e:
        return None, str(e)[:300]


def handle_taps():
    for u in tg_updates():
        cb = u.get("callback_query")
        if not cb:
            continue
        data = cb.get("data", "")
        tg_answer(cb["id"], "working...")
        if ":" not in data:
            continue
        act, tid = data.split(":", 1)
        rows = sb_get("swing_triggers", "id=eq.%s" % tid)
        if not rows:
            continue
        t = rows[0]
        if t["status"] != "offered":
            tg_edit(t.get("tg_message_id"),
                    "This one is already %s." % t["status"])
            continue

        if act == "N":
            sb_patch("swing_triggers", "id=eq.%s" % tid,
                     {"status": "skipped",
                      "decided_at": now_ist().isoformat()})
            tg_edit(t["tg_message_id"], "\u274c <b>%s</b> skipped." % t["symbol"])
            continue

        ok, note = on_confirm(t)
        sb_patch("swing_triggers", "id=eq.%s" % tid,
                 {"status": "placed" if ok else "rejected",
                  "reject_reason": None if ok else note,
                  "decided_at": now_ist().isoformat()})
        tg_edit(t["tg_message_id"],
                ("\u2705 <b>%s</b> - %s" if ok else "\u26d4 <b>%s</b> - %s")
                % (t["symbol"], note))
        log("%s %s -> %s" % (t["symbol"], act, note))


def expire_old():
    cutoff = (now_ist() - timedelta(seconds=CONFIRM_WINDOW_SEC)).isoformat()
    for t in sb_get("swing_triggers",
                    "status=eq.offered&offered_at=lt.%s" % cutoff):
        sb_patch("swing_triggers", "id=eq.%s" % t["id"],
                 {"status": "expired", "decided_at": now_ist().isoformat()})
        tg_edit(t.get("tg_message_id"),
                "\u23f1 <b>%s</b> expired - you did not tap in time." % t["symbol"])


# ---------------- main ----------------------------------------
def in_session():
    n = now_ist()
    return SESSION_START <= (n.hour, n.minute) <= SESSION_END


def main():
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        return                      # another copy is already running

    if not in_session():
        return

    log("swing_bot up  DRY_RUN=%s  risk=Rs%d  cap=%d positions"
        % (DRY_RUN, RISK_RUPEES, MAX_OPEN_POSITIONS))
    tg_send("\U0001f916 Swing bot started%s" % (" <b>[DRY RUN]</b>" if DRY_RUN else ""))

    tick = 0
    while in_session():
        try:
            handle_taps()
            if tick % max(1, TRIGGER_POLL_SEC // 5) == 0:
                for t in sb_get("swing_triggers",
                                "status=eq.new&order=created_at.asc&limit=5"):
                    offer(t)
                expire_old()
        except Exception:
            log("loop error:\n" + traceback.format_exc())
        tick += 1
        time.sleep(5)

    expire_old()
    log("swing_bot done for the day")


if __name__ == "__main__":
    main()
