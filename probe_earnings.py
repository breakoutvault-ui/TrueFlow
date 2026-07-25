#!/usr/bin/env python3
"""
TrueFlow US — Earnings Data Probe (read-only, writes nothing)
Tests exactly what earnings data yfinance gives us for US stocks,
so we design the tracker around real data instead of guesses.
Run: /root/trueflow/bin/python /root/trueflow/probe_earnings.py
"""
import yfinance as yf
import pandas as pd
import datetime as dt

TESTS = ["AAPL", "NVDA", "JPM", "GTLB", "DPC", "PLTR", "CRWD", "SMCI"]

print("=" * 70)
print("US EARNINGS DATA PROBE —", dt.date.today())
print("=" * 70)

for sym in TESTS:
    print(f"\n{'─'*60}\n{sym}")
    try:
        t = yf.Ticker(sym)
    except Exception as e:
        print("  ticker init failed:", e); continue

    # 1) earnings_dates — the key table (past + next few, with EPS est/actual)
    try:
        ed = t.get_earnings_dates(limit=12)
        if ed is not None and len(ed):
            print(f"  earnings_dates: {len(ed)} rows | cols: {list(ed.columns)}")
            # show the most recent 4
            for idx, row in ed.head(6).iterrows():
                d = idx.date() if hasattr(idx, 'date') else idx
                est = row.get('EPS Estimate')
                act = row.get('Reported EPS')
                sur = row.get('Surprise(%)')
                print(f"      {d}  est={est}  actual={act}  surprise%={sur}")
        else:
            print("  earnings_dates: EMPTY")
    except Exception as e:
        print("  earnings_dates ERROR:", str(e)[:120])

    # 2) calendar — next earnings date (dict form)
    try:
        cal = t.calendar
        if isinstance(cal, dict):
            ed_next = cal.get('Earnings Date')
            print(f"  calendar next earnings: {ed_next}")
        else:
            print(f"  calendar type: {type(cal)}")
    except Exception as e:
        print("  calendar ERROR:", str(e)[:120])

print("\n" + "=" * 70)
print("PROBE COMPLETE")
print("=" * 70)
