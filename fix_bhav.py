#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_bhav.py — repairs the SyntaxError that has stopped nse_bhav_fetcher.py
from running at all.

THE BUG
-------
Line 579 (and its twin a few lines below) contains a nested f-string whose
inner expression uses backslash-escaped double quotes:

    f"  {', '.join(f'{o[\"symbol\"]} ({o[\"pcr\"]})' for o in high_pcr)}\n\n"

Python cannot parse a backslash inside an f-string expression. The file has
therefore never compiled, cron has fired it every evening at 6:15 PM, and it
has exited instantly every time — which is why fo_bhav_oi has zero rows.

THE FIX
-------
Same output, no nesting and no backslashes:

    f"  {', '.join('%s (%s)' % (o['symbol'], o['pcr']) for o in high_pcr)}\n\n"

SAFETY
------
  * Only rewrites lines that contain BOTH the target variable and a
    backslash-escaped quote. Nothing else in the file is touched.
  * Refuses to run if it doesn't find exactly 2 such lines.
  * Writes a timestamped .bak first.
  * Parses the result BEFORE saving. If the patched version doesn't
    compile, nothing is written and the original stays as it was.

USAGE
-----
    cd /root/trueflow && bin/python fix_bhav.py
"""

import ast
import shutil
import sys
from datetime import datetime

PATH = "/root/trueflow/nse_bhav_fetcher.py"
VARS = ("high_pcr", "low_pcr")


def build_line(indent, var):
    """The replacement line: no nested f-string, no backslashes."""
    return (indent + 'f"  {'
            + "', '.join('%s (%s)' % (o['symbol'], o['pcr']) for o in "
            + var + ")"
            + '}\\n\\n"')


def main():
    try:
        src = open(PATH, encoding="utf-8").read()
    except IOError as e:
        print("FATAL: cannot read %s (%s)" % (PATH, e))
        sys.exit(1)

    # Confirm it is actually broken before touching anything
    try:
        ast.parse(src)
        print("This file already parses cleanly. Nothing to fix.")
        print("If fo_bhav_oi is still empty the cause is elsewhere.")
        sys.exit(0)
    except SyntaxError as e:
        print("Confirmed broken: line %s — %s" % (e.lineno, e.msg))

    lines = src.split("\n")
    hits = []
    for i, ln in enumerate(lines):
        for var in VARS:
            if ("for o in %s)" % var) in ln and '\\"' in ln:
                hits.append((i, var))

    if len(hits) != 2:
        print("FATAL: expected 2 broken lines, found %d." % len(hits))
        print("Not guessing. Send Claude these line numbers:")
        for i, var in hits:
            print("   line %d (%s)" % (i + 1, var))
        sys.exit(1)

    for i, var in hits:
        old = lines[i]
        indent = old[:len(old) - len(old.lstrip())]
        lines[i] = build_line(indent, var)
        print("-" * 60)
        print("line %d  (%s)" % (i + 1, var))
        print("  OLD: %s" % old.strip()[:78])
        print("  NEW: %s" % lines[i].strip()[:78])

    patched = "\n".join(lines)

    # Verify BEFORE writing — this is the whole point
    try:
        ast.parse(patched)
    except SyntaxError as e:
        print("-" * 60)
        print("FATAL: patched version still fails at line %s — %s"
              % (e.lineno, e.msg))
        print("Nothing written. Your file is untouched.")
        sys.exit(1)

    bak = "%s.bak.%s" % (PATH, datetime.now().strftime("%Y%m%d_%H%M%S"))
    shutil.copy2(PATH, bak)
    with open(PATH, "w", encoding="utf-8") as fh:
        fh.write(patched)

    print("-" * 60)
    print("PATCHED and verified.")
    print("Backup: %s" % bak)
    print("Now run:  cd /root/trueflow && bin/python nse_bhav_fetcher.py")


if __name__ == "__main__":
    main()
