#!/usr/bin/env python3
# ==============================================================
#  Fix: detect_vcp measures the move in the wrong direction
#
#  THE BUG
#    low_20d  = min(closes[-25:-5])   # the low, from 25 to 5 days ago
#    high_20d = max(closes[-20:])     # the high, from 20 days ago to today
#    move_pct = (high_20d - low_20d) / low_20d * 100
#
#  Those windows overlap and nothing forces the high to come AFTER the low.
#  If a stock peaked 20 days ago and bottomed 6 days ago, this reports the
#  DECLINE as a rally and the stock passes the "moved 20%+" test that is
#  meant to find stocks which have gone up. Downtrends get tagged VCP.
#
#  THE FIX
#    The function already computes the post-low high correctly, two lines
#    above, for move_days:  _post = closes[_lo_i:]
#    Use that same high for move_pct. One line.
#
#  SAFE: makes a timestamped backup first, refuses to run if the line is not
#  found exactly once, and verifies afterwards. Changes nothing else.
#
#  Run:  cd /root/trueflow && ./bin/python patch_vcp.py
# ==============================================================

import os, sys, shutil, datetime

TARGET = "/root/trueflow/momentum_scan.py"

OLD = "    move_pct = (high_20d - low_20d) / low_20d * 100 if low_20d > 0 else 0\n"
NEW = ("    # high_20d can PRE-DATE low_20d, which turns a decline into a fake\n"
       "    # rally and lets downtrends pass the 20% move test. _post already\n"
       "    # holds the closes from the low onward - use the high after the low.\n"
       "    move_high = max(_post) if _post else high_20d\n"
       "    move_pct = (move_high - low_20d) / low_20d * 100 if low_20d > 0 else 0\n")


def main():
    if not os.path.exists(TARGET):
        print("FAILED: %s not found." % TARGET); sys.exit(1)

    src = open(TARGET).read()

    if "move_high = max(_post)" in src:
        print("Already patched - nothing to do."); return

    n = src.count(OLD)
    if n != 1:
        print("FAILED: expected that line exactly once, found %d." % n)
        print("Nothing has been changed. Send me this message and I will look again.")
        sys.exit(1)

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = "%s.bak.%s" % (TARGET, stamp)
    shutil.copy2(TARGET, backup)

    open(TARGET, "w").write(src.replace(OLD, NEW))

    check = open(TARGET).read()
    ok = ("move_high = max(_post)" in check
          and "move_pct = (move_high - low_20d)" in check
          and OLD not in check)

    try:
        import py_compile
        py_compile.compile(TARGET, doraise=True)
        compiles = True
    except Exception as e:
        compiles = False
        print("COMPILE ERROR: %s" % e)

    if ok and compiles:
        print("PATCHED. momentum_scan.py compiles cleanly.")
        print("Backup saved at: %s" % backup)
        print("")
        print("The change only takes effect on the NEXT nightly scan.")
        print("Existing rows keep whatever they were tagged before.")
    else:
        shutil.copy2(backup, TARGET)
        print("VERIFY FAILED - the original file has been restored from backup.")
        sys.exit(1)


if __name__ == "__main__":
    main()
