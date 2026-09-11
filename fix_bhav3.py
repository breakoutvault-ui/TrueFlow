#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_bhav3.py — repairs the two write bugs in nse_bhav_fetcher.py

Replaces fix_bhav2.py, whose anchors were only ever tested on a stand-in
copy of the fetcher. This one touches exactly two lines, both seen verbatim
on the server (grep, 3 Sep):

  122: def supabase_upsert(table, rows, on_conflict):
  535:     chain_count = supabase_upsert("oi_chain", index_chain, "symbol,session_date,strike")

BUG 1  fo_bhav_oi keeps ~13 of ~215 rows.
       PostgREST PGRST102 "All object keys must match" throws away a WHOLE
       batch when rows differ in shape. Fix: every row is padded to the same
       set of keys before it is sent. Protects every table this script writes.

BUG 2  oi_chain always gets 0 rows.
       oi_chain belongs to oi_scanner.py (UNIQUE session_date, data jsonb) and
       cannot hold per-strike rows. Fix: per-strike rows go to the new table
       oi_chain_eod, stored as session_date / symbol / expiry / strike plus the
       rest of each row in a `data` jsonb column — so no column name has to be
       guessed. oi_chain is not touched.

SAFETY  Both anchors must match exactly once, or nothing is written.
        Timestamped .bak first. The result must parse before it is saved.

USAGE   cd /root/trueflow && bin/python fix_bhav3.py
"""

import ast
import shutil
import sys
from datetime import datetime

PATH = "/root/trueflow/nse_bhav_fetcher.py"

A1_OLD = 'def supabase_upsert(table, rows, on_conflict):'
A1_NEW = '''def _tf_prepare_rows(table, rows):
    """Added by fix_bhav3.py.
    1. oi_chain_eod rows are packed as session_date/symbol/expiry/strike + data.
    2. Every row is padded to the same key set (PostgREST PGRST102 guard)."""
    if not rows:
        return rows
    if table == "oi_chain_eod":
        packed = {}
        for r in rows:
            exp = str(r.get("expiry") or "")
            key = (str(r.get("session_date")), str(r.get("symbol")), exp,
                   str(r.get("strike")))
            data = {k: v for k, v in r.items()
                    if k not in ("session_date", "symbol", "expiry", "strike")}
            packed[key] = {"session_date": r.get("session_date"),
                           "symbol": r.get("symbol"), "expiry": exp,
                           "strike": r.get("strike"), "data": data}
        rows = list(packed.values())
    keys = set()
    for r in rows:
        keys.update(r.keys())
    return [{k: r.get(k) for k in keys} for r in rows]


def supabase_upsert(table, rows, on_conflict):
    rows = _tf_prepare_rows(table, rows)'''

A2_OLD = '    chain_count = supabase_upsert("oi_chain", index_chain, "symbol,session_date,strike")'
A2_NEW = ('    chain_count = supabase_upsert("oi_chain_eod", index_chain, '
          '"session_date,symbol,expiry,strike")  # fix_bhav3: not oi_chain')


def main():
    try:
        src = open(PATH, encoding="utf-8").read()
    except IOError as e:
        print("FATAL: cannot read %s (%s)" % (PATH, e))
        sys.exit(1)
    print("Read %s — %d lines" % (PATH, src.count("\n") + 1))

    if "_tf_prepare_rows" in src:
        print("Already patched by fix_bhav3. Nothing to do.")
        sys.exit(0)
    if "download_with_retry" in src:
        print("fix_bhav2 was applied earlier — stop and tell Claude before going on.")
        sys.exit(1)

    c1, c2 = src.count(A1_OLD), src.count(A2_OLD)
    print("anchor 1 (supabase_upsert)   found %d" % c1)
    print("anchor 2 (oi_chain upsert)   found %d" % c2)
    if c1 != 1 or c2 != 1:
        print("ABORTED — each anchor must be found exactly once.")
        print("Nothing written. Your file is untouched.")
        sys.exit(1)

    out = src.replace(A1_OLD, A1_NEW, 1).replace(A2_OLD, A2_NEW, 1)
    try:
        ast.parse(out)
    except SyntaxError as e:
        print("ABORTED — patched file would not parse (line %s: %s)." % (e.lineno, e.msg))
        print("Nothing written. Your file is untouched.")
        sys.exit(1)

    bak = "%s.bak.%s" % (PATH, datetime.now().strftime("%Y%m%d_%H%M%S"))
    shutil.copy2(PATH, bak)
    with open(PATH, "w", encoding="utf-8") as fh:
        fh.write(out)
    print("PATCHED and verified. Backup: %s" % bak)


if __name__ == "__main__":
    main()
