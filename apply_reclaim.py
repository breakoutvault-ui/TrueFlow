#!/usr/bin/env python3
"""
Patches momentum_scan.py to add EMA Reclaim v2.
Safe to run twice - it detects if the patch is already applied.
Makes a timestamped backup first.
"""
import shutil, sys, datetime, os

P = "/root/trueflow/momentum_scan.py"
BLOCK = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "reclaim_v2_block.txt")).read()

s = open(P).read()

if "detect_reclaim_v2" in s:
    print("ALREADY PATCHED - nothing to do.")
    sys.exit(0)

bak = P + ".bak-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
shutil.copy(P, bak)
print("backup: " + bak)

# 1. insert the detector just before detect_qm_patterns
a1 = "def detect_qm_patterns(hist, closes, volumes, ema9d, prev_snapshot):"
assert s.count(a1) == 1, "anchor 1 not unique"
s = s.replace(a1, BLOCK + "\n\n" + a1)

# 2. call it inside process_stock, right before the record dict
a2 = "    record = {\n        'symbol': symbol,"
assert s.count(a2) == 1, "anchor 2 not unique"
s = s.replace(a2, "    rcl = detect_reclaim_v2(hist, closes, volumes)\n\n" + a2)

# 3. add the fields to the record
a3 = "        'qm_ema_reclaim': qm['qm_ema_reclaim'],"
assert s.count(a3) == 1, "anchor 3 not unique"
fields = a3 + "\n" + "\n".join(
    "        '%s': rcl['%s']," % (k, k) for k in [
        "rcl_grade","rcl_days_below","rcl_ema_lost","rcl_dip_pct",
        "rcl_dip_adr","rcl_prior_move_pct","rcl_prior_move_days",
        "rcl_slope_ok","rcl_dip_vol","rcl_reclaim_vol","rcl_vol_pattern",
        "rcl_stop_level","rcl_risk_pct"])
s = s.replace(a3, fields)

open(P, "w").write(s)
print("PATCHED OK")
