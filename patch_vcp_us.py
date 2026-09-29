#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch_vcp_us.py - the same VCP move-window fix India got, for us_momentum_scan.py

THE BUG
  detect_vcp() measures the "prior move" as max(last 20 closes) minus
  min(closes 25..5 sessions ago). That high can come BEFORE the low, so a
  stock that fell from a high to a low passes the 20% move test as if it had
  rallied - downtrends get labelled VCP every night.

THE FIX (copied from momentum_scan.py, India)
  Use the highest close AFTER the low. Nothing else in the file changes.

SAFETY  anchor must match exactly once · timestamped .bak first ·
        the result must parse before it is saved.
USAGE   cd /root/trueflow && bin/python patch_vcp_us.py
"""
import ast, shutil, sys
from datetime import datetime

PATH = "/root/trueflow/us_momentum_scan.py"
OLD = ("    high_20d = max(closes[-20:])\n"
       "    move_pct = (high_20d - low_20d) / low_20d * 100 if low_20d > 0 else 0\n")
NEW = ("    high_20d = max(closes[-20:])\n"
       "    # patch_vcp_us: high_20d can PRE-DATE low_20d, which turns a decline into a\n"
       "    # fake rally. Measure the move to the highest close AFTER the low (as India).\n"
       "    _lo_i = len(closes) - 25 + closes[-25:-5].index(low_20d)\n"
       "    _post = closes[_lo_i:]\n"
       "    move_high = max(_post) if _post else high_20d\n"
       "    move_pct = (move_high - low_20d) / low_20d * 100 if low_20d > 0 else 0\n")

src = open(PATH, encoding="utf-8").read()
if "patch_vcp_us" in src:
    print("Already patched. Nothing to do."); sys.exit(0)
n = src.count(OLD)
print("anchor found %d time(s)" % n)
if n != 1:
    print("ABORTED - expected exactly 1. Nothing written."); sys.exit(1)
out = src.replace(OLD, NEW, 1)
try:
    ast.parse(out)
except SyntaxError as e:
    print("ABORTED - would not parse (line %s: %s). Nothing written." % (e.lineno, e.msg)); sys.exit(1)
bak = "%s.bak.%s" % (PATH, datetime.now().strftime("%Y%m%d_%H%M%S"))
shutil.copy2(PATH, bak)
open(PATH, "w", encoding="utf-8").write(out)
print("PATCHED and verified. Backup: %s" % bak)
