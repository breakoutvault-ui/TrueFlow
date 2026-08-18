#!/usr/bin/env python3
"""
Patches us_momentum_scan.py to add EMA Reclaim v2.

Same detector as the India side. It works on raw candles, so nothing in
it is market-specific - but the US scanner has THREE occurrences of
"'symbol': symbol," (lines ~488, ~520, ~644), so the India patcher's
anchor would be ambiguous here. This one anchors on the qm_ema_reclaim
line instead, which is unique, and inserts the detector call by locating
the record dict that follows the adr_pct calculation.

Safe to run twice. Makes a timestamped backup first.
"""
import shutil, sys, datetime, os, re

P = "/root/trueflow/us_momentum_scan.py"
BLOCK = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "reclaim_v2_block.txt")).read()

s = open(P).read()

if "detect_reclaim_v2" in s:
    print("ALREADY PATCHED - nothing to do.")
    sys.exit(0)

bak = P + ".bak-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
shutil.copy(P, bak)
print("backup: " + bak)

# 1. drop the detector in before detect_qm_patterns
a1 = "def detect_qm_patterns(hist, closes, volumes, ema9d, prev_snapshot):"
assert s.count(a1) == 1, "anchor 1 count=%d" % s.count(a1)
s = s.replace(a1, BLOCK + "\n\n" + a1)

# 2. call it just before the record dict.
#    The record dict we want is the one that follows the adr_pct line -
#    that is unique, unlike "'symbol': symbol,".
m = re.search(r"(    adr_pct = round\([^\n]*\n)(\s*\n)*(    record = \{)", s)
assert m, "could not locate the record dict after adr_pct"
s = (s[:m.start(3)]
     + "    rcl = detect_reclaim_v2(hist, closes, volumes)\n\n"
     + s[m.start(3):])

# 3. add the fields to that record
a3 = "        'qm_ema_reclaim': qm['qm_ema_reclaim'],"
assert s.count(a3) == 1, "anchor 3 count=%d" % s.count(a3)
fields = a3 + "\n" + "\n".join(
    "        '%s': rcl['%s']," % (k, k) for k in [
        "rcl_grade", "rcl_days_below", "rcl_ema_lost", "rcl_dip_pct",
        "rcl_dip_adr", "rcl_prior_move_pct", "rcl_prior_move_days",
        "rcl_slope_ok", "rcl_dip_vol", "rcl_reclaim_vol",
        "rcl_vol_pattern", "rcl_stop_level", "rcl_risk_pct"])
s = s.replace(a3, fields)

open(P, "w").write(s)
print("PATCHED OK")
