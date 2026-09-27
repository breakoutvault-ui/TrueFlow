#!/usr/bin/env python3
# ==============================================================
#  alert_engine.py - split its five message streams
#
#  This script currently sends everything to one personal chat, with
#  three bots. Worse, ONE function carries both your F&O trade alerts
#  and the engine's own housekeeping - so "ORB triggered on SUZLON"
#  arrives next to "reconnecting WebSocket".
#
#  After this:
#    send_breakouts_telegram  -> TrueFlow Breakouts   (coil breaks)
#    send_telegram            -> TrueFlow F&O         (ORB + F&O alerts)
#    send_system_telegram     -> TrueFlow System      (engine messages)   NEW
#    send_login_telegram      -> your personal chat   (login only)        NEW
#    send_bn_telegram         -> TrueFlow BankNifty   (stage alerts)
#
#  Call sites are matched on the TEXT of each message, not line numbers,
#  so this still works if the file shifts.
#
#  SAFE: backs up first, checks the result compiles, restores on failure.
#  Run it twice and the second run says there is nothing to do.
#
#  Run:  cd /root/trueflow && ./bin/python patch_engine_routing.py
# ==============================================================

import os, sys, shutil, datetime, py_compile

TARGET   = "/root/trueflow/alert_engine.py"
PERSONAL = "1202026803"

TF_FO        = "-5407697732"
TF_BREAKOUTS = "-5463362876"
TF_SYSTEM    = "-4998741519"
TF_BANKNIFTY = "-5419812169"

# engine housekeeping, matched on wording
SYSTEM_MARKERS = [
    "Kite token invalid",
    "New Kite token detected",
    "reconnecting WebSocket",
    "No ticks for",
    "after ORB window",
    "Engine CRASHED",
]
LOGIN_MARKER = "Login Successful"


def main():
    if not os.path.exists(TARGET):
        print("FAILED: %s not found" % TARGET); sys.exit(1)

    src = open(TARGET).read()
    if "send_system_telegram" in src:
        print("Already routed - nothing to do."); return

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = TARGET + ".bak." + stamp
    shutil.copy2(TARGET, backup)

    out, notes = src, []

    # ---- 1. the three existing destinations ----
    pairs = [
        ('"telegram_chat_id"  : "%s"' % PERSONAL,
         '"telegram_chat_id"  : "%s"' % TF_FO, "send_telegram -> TrueFlow F&O"),
        ('"bn_telegram_chat"  : "%s"' % PERSONAL,
         '"bn_telegram_chat"  : "%s"' % TF_BANKNIFTY, "send_bn_telegram -> TrueFlow BankNifty"),
        ('payload = {"chat_id": "%s", "text": message, "parse_mode": "HTML"}' % PERSONAL,
         'payload = {"chat_id": "%s", "text": message, "parse_mode": "HTML"}' % TF_BREAKOUTS,
         "send_breakouts_telegram -> TrueFlow Breakouts"),
    ]
    for old, new, note in pairs:
        if out.count(old) != 1:
            shutil.copy2(backup, TARGET)
            print("FAILED: expected this once, found %d:\n  %s" % (out.count(old), old))
            print("Nothing changed. Send me this message."); sys.exit(1)
        out = out.replace(old, new)
        notes.append(note)

    # ---- 2. two new senders ----
    anchor = "def send_breakouts_telegram(message: str):"
    if anchor not in out:
        shutil.copy2(backup, TARGET)
        print("FAILED: could not find send_breakouts_telegram to anchor to."); sys.exit(1)
    new_fns = '''def send_system_telegram(message: str):
    """Engine housekeeping - token expiry, reconnects, crashes. These are not
    trade alerts and should never sit between two of them."""
    try:
        url = "https://api.telegram.org/bot%s/sendMessage" % CONFIG["telegram_token"]
        payload = {"chat_id": "''' + TF_SYSTEM + '''", "text": message, "parse_mode": "HTML"}
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        log.warning("System telegram failed: %s", e)


def send_login_telegram(message: str):
    """The daily Kite login stays in the personal chat on purpose - it is the
    one message that must be obvious every morning, not buried in a group."""
    try:
        url = "https://api.telegram.org/bot%s/sendMessage" % CONFIG["telegram_token"]
        payload = {"chat_id": "''' + PERSONAL + '''", "text": message, "parse_mode": "HTML"}
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        log.warning("Login telegram failed: %s", e)


'''
    out = out.replace(anchor, new_fns + anchor, 1)
    notes.append("added send_system_telegram and send_login_telegram")

    # ---- 3. repoint the call sites, by message text not line number ----
    lines, sysn, logn = out.split("\n"), 0, 0
    for i, ln in enumerate(lines):
        if "send_telegram(" not in ln or "def send_telegram" in ln:
            continue
        if LOGIN_MARKER in ln:
            lines[i] = ln.replace("send_telegram(", "send_login_telegram("); logn += 1
        elif any(m in ln for m in SYSTEM_MARKERS):
            lines[i] = ln.replace("send_telegram(", "send_system_telegram("); sysn += 1
    out = "\n".join(lines)
    notes.append("%d engine messages -> System" % sysn)
    notes.append("%d login message -> personal chat" % logn)

    open(TARGET, "w").write(out)

    try:
        py_compile.compile(TARGET, doraise=True)
    except Exception as e:
        shutil.copy2(backup, TARGET)
        print("COMPILE FAILED - original restored.\n  %s" % e); sys.exit(1)

    print("\nROUTED")
    for n in notes:
        print("  " + n)
    print("\nBackup: %s" % backup)
    if sysn < 5:
        print("\nNOTE: only %d engine messages matched. Expected about 6 -" % sysn)
        print("some may have been reworded. Worth a look, not a failure.")
    print("\nRESTART REQUIRED: the engine is long-running, so it keeps the old")
    print("routing until it restarts. It restarts by cron at 9:00 AM IST.")


if __name__ == "__main__":
    main()
