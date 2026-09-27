#!/usr/bin/env python3
# ==============================================================
#  Telegram routing - send each script to its own group
#
#  Today every script posts to chat 1202026803, which is your personal
#  chat. Roughly twenty messages a day, trade alerts mixed in among
#  "bhav copy complete" - which is how a real alert gets scrolled past.
#
#  After this:
#    TrueFlow US            - every US script
#    TrueFlow F&O           - OI, bhav, tomorrow's ORB watchlist
#    TrueFlow Breakouts     - breakout monitor
#    TrueFlow Chart Alerts  - price levels you set on the chart
#    TrueFlow System        - scans finished, backups, archives
#
#  NOT touched, on purpose:
#    alert_engine.py   - carries THREE bot tokens and routes different
#                        message types to different bots. Changing it
#                        blind could silently redirect your live coil
#                        alerts. Handled separately once we have read it.
#    swing_bot.py      - trade cards stay a private message until they
#                        get their own bot
#    telegram_ai.py    - the login message stays where it is
#    banknifty_*       - unchanged
#
#  SAFE: backs up every file it touches, checks each still compiles, and
#  restores everything if any single file fails. Run it twice and the
#  second run says there is nothing to do.
#
#  Run:  cd /root/trueflow && ./bin/python patch_telegram.py
# ==============================================================

import os, sys, shutil, datetime, py_compile

HOME = "/root/trueflow"
OLD  = "1202026803"                 # the personal chat everything uses today

GROUPS = {
    "TF_US":        "-5455662450",
    "TF_FO":        "-5407697732",
    "TF_BREAKOUTS": "-5463362876",
    "TF_CHART":     "-5436222389",
    "TF_SYSTEM":    "-4998741519",
}

# file -> which group its messages belong in
ROUTE = {
    "us_momentum_scan.py":    "TF_US",
    "us_rs.py":               "TF_US",
    "us_smart_money.py":      "TF_US",
    "us_sec_filings.py":      "TF_US",
    "us_earnings_tracker.py": "TF_US",
    "nse_bhav_fetcher.py":    "TF_FO",
    "oi_scanner.py":          "TF_FO",
    "watchlist_generator.py": "TF_FO",
    "breakout_monitor.py":    "TF_BREAKOUTS",
    "momentum_scan.py":       "TF_SYSTEM",
    "eod_ema.py":             "TF_SYSTEM",
}

# these read tf_config instead of hardcoding, so they need a name swap
CFG_USERS = {
    "price_alerts.py": ("TELEGRAM_CHAT_ID", "TF_CHART"),
}

SHELL = {                            # .sh files, same idea
    "mtf_snapshot.sh": "TF_SYSTEM",
    "db_backup.sh":    "TF_SYSTEM",
    "db_archive.sh":   "TF_SYSTEM",
}


def main():
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backups, changed, skipped = [], [], []

    # ---- 1. the group ids go in tf_config, so a future move is one line ----
    cfg = os.path.join(HOME, "tf_config.py")
    src = open(cfg).read()
    if "TF_SYSTEM" in src:
        print("tf_config.py already has the group ids.")
    else:
        shutil.copy2(cfg, cfg + ".bak." + stamp)
        backups.append((cfg + ".bak." + stamp, cfg))
        add = ["", "# Telegram groups. TELEGRAM_CHAT_ID above stays the private chat,",
               "# which is what the swing bot uses for trade cards.", ""]
        for k, v in GROUPS.items():
            add.append('%s = "%s"' % (k, v))
        open(cfg, "a").write("\n".join(add) + "\n")
        changed.append("tf_config.py  (added 5 group ids)")

    # ---- 2. scripts with the chat id written into them ----
    for fn, grp in ROUTE.items():
        path = os.path.join(HOME, fn)
        if not os.path.exists(path):
            skipped.append(fn + "  (not found)"); continue
        s = open(path).read()
        n = s.count(OLD)
        if n == 0:
            skipped.append(fn + "  (chat id not found - already moved?)"); continue
        shutil.copy2(path, path + ".bak." + stamp)
        backups.append((path + ".bak." + stamp, path))
        open(path, "w").write(s.replace(OLD, GROUPS[grp]))
        changed.append("%-24s %s  (%d place%s)" % (fn, grp, n, "" if n == 1 else "s"))

    # ---- 3. scripts that read tf_config ----
    for fn, (oldname, newname) in CFG_USERS.items():
        path = os.path.join(HOME, fn)
        if not os.path.exists(path):
            skipped.append(fn + "  (not found)"); continue
        s = open(path).read()
        if newname in s:
            skipped.append(fn + "  (already routed)"); continue
        if oldname not in s:
            skipped.append(fn + "  (%s not referenced)" % oldname); continue
        shutil.copy2(path, path + ".bak." + stamp)
        backups.append((path + ".bak." + stamp, path))
        open(path, "w").write(s.replace(oldname, newname))
        changed.append("%-24s %s  (via tf_config)" % (fn, newname))

    # ---- 4. the shell scripts ----
    for fn, grp in SHELL.items():
        path = os.path.join(HOME, fn)
        if not os.path.exists(path):
            skipped.append(fn + "  (not found)"); continue
        s = open(path).read()
        if "c.TELEGRAM_CHAT_ID" not in s:
            skipped.append(fn + "  (no chat reference)"); continue
        shutil.copy2(path, path + ".bak." + stamp)
        backups.append((path + ".bak." + stamp, path))
        open(path, "w").write(s.replace("c.TELEGRAM_CHAT_ID", "c." + grp))
        changed.append("%-24s %s" % (fn, grp))

    # ---- 5. everything must still compile, or nothing changes ----
    bad = []
    for _b, live in backups:
        if live.endswith(".py"):
            try:
                py_compile.compile(live, doraise=True)
            except Exception as e:
                bad.append("%s: %s" % (os.path.basename(live), e))
    if bad:
        for b, live in backups:
            shutil.copy2(b, live)
        print("COMPILE FAILED - every file restored from backup:")
        for x in bad:
            print("  " + x)
        sys.exit(1)

    print("\nROUTED")
    for c in changed:
        print("  " + c)
    if skipped:
        print("\nSKIPPED")
        for x in skipped:
            print("  " + x)
    print("\nBackups: *.bak.%s" % stamp)
    print("\nNot touched on purpose: alert_engine.py (three bot tokens),")
    print("swing_bot.py (private trade cards), telegram_ai.py (login),")
    print("banknifty_tracker.py and bnf_live_commentary.py.")
    print("\nNothing sends twice: each script has exactly one destination.")


if __name__ == "__main__":
    main()
