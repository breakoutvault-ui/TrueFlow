#!/root/trueflow/bin/python
# -*- coding: utf-8 -*-
"""
tg_audit.py - lists every script that carries its OWN Telegram bot token or
chat ID instead of reading tf_config.py. Read-only: changes nothing.
Tokens are never printed - each is shown as the bot's @username (asked from
Telegram), chat IDs as the group name they belong to.

USAGE   cd /root/trueflow && bin/python tg_audit.py
"""
import os, re, sys, glob
import requests
sys.path.insert(0, "/root/trueflow")

TOK = re.compile(r"\b(\d{8,10}:[A-Za-z0-9_-]{30,})\b")
CHAT = re.compile(r"""["'](-100\d{9,11}|-\d{9,11}|\d{9,10})["']""")
KNOWN = {"-1004405719798": "Breakouts", "-1004324853933": "F&O", "-1003951865342": "Chart Alerts",
         "-1003744925337": "US", "-1004387801904": "System", "-1003944949273": "BankNifty",
         "1202026803": "PERSONAL CHAT"}
SKIP = re.compile(r"(\.bak|backup|^patch_|^fix_|^apply_|^tg_audit\.py$|^tf_config\.py$)")

names = {}
def bot(t):
    if t not in names:
        try:
            j = requests.get("https://api.telegram.org/bot%s/getMe" % t, timeout=15).json()
            names[t] = "@" + j["result"]["username"] if j.get("ok") else "INVALID token"
        except Exception:
            names[t] = "unknown (no answer)"
    return names[t]

os.chdir("/root/trueflow")
rows = []
for f in sorted(glob.glob("*.py")):
    if SKIP.search(f):
        continue
    try:
        src = open(f, encoding="utf-8", errors="ignore").read()
    except Exception:
        continue
    toks = sorted(set(TOK.findall(src)))
    chats = sorted(set(c for c in CHAT.findall(src) if c in KNOWN or c.startswith("-100")))
    if not toks and not chats:
        continue
    uses_cfg = "tf_config" in src
    rows.append((f, [bot(t) for t in toks], [KNOWN.get(c, "unknown " + c) for c in chats], uses_cfg))

print("%-30s %-34s %-34s %s" % ("SCRIPT", "OWN BOT TOKEN(S)", "OWN CHAT ID(S)", "tf_config?"))
print("-" * 110)
for f, b, c, u in rows:
    print("%-30s %-34s %-34s %s" % (f, ", ".join(b) or "-", ", ".join(c) or "-", "yes" if u else "no"))
print("-" * 110)
print("%d script(s) carry their own Telegram settings." % len(rows))
