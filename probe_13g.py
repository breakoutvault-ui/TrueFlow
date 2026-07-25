#!/usr/bin/env python3
"""
Dump the XML structure of a PASSIVE 13G filing. The earlier probe only looked at
13D filings; 13G appears to use a different schema, which is why filer names and
percentages come back empty. Read-only.

Run: /root/trueflow/bin/python /root/trueflow/probe_13g.py
"""
import re, time, requests
from xml.etree import ElementTree as ET

UA = {"User-Agent": "TrueFlow personal research breakoutvault@gmail.com",
      "Accept-Encoding": "gzip, deflate"}
S = requests.Session(); S.headers.update(UA)

# ACLS had many 13G filings in the test run — walk its recent filings and grab
# the first 13G we can find, so this works without depending on our own table.
TARGETS = [("ACLS", 1113232), ("ACIW", 935036), ("ABNB", 1559720)]

def get(u):
    r = S.get(u, timeout=35); time.sleep(0.25); return r

def walk(el, depth=0, out=None):
    tag = re.sub(r"\{.*?\}", "", el.tag)
    txt = (el.text or "").strip()
    if out is None: out = []
    out.append(("  " * depth + tag, txt[:95]))
    for ch in list(el):
        walk(ch, depth + 1, out)
    return out

shown = 0
for sym, cik in TARGETS:
    if shown >= 2:
        break
    r = get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    if r.status_code != 200:
        print(f"{sym}: submissions {r.status_code}"); continue
    rec = r.json().get("filings", {}).get("recent", {})
    forms = rec.get("form", []); dates = rec.get("filingDate", []); accs = rec.get("accessionNumber", [])
    picked = None
    for i, f in enumerate(forms):
        fu = str(f).upper()
        if fu.startswith("SCHEDULE 13G") and dates[i] >= "2026-01-01":
            picked = (f, dates[i], accs[i]); break
    if not picked:
        print(f"{sym}: no recent 13G found"); continue
    form, fdate, acc = picked
    folder = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-','')}"
    print("=" * 78)
    print(f"{sym}  {form}  {fdate}")
    print(folder)
    print("=" * 78)

    idx = get(folder + "/index.json")
    if idx.status_code == 200:
        try:
            print("\nfiles:")
            for it in idx.json()["directory"]["item"][:12]:
                print(f"   {it.get('name'):48} {it.get('size'):>10}")
        except Exception as e:
            print("index parse:", e)

    xr = get(folder + "/primary_doc.xml")
    if xr.status_code != 200:
        print(f"\nprimary_doc.xml status {xr.status_code} — this filing may use a different file")
        continue
    try:
        root = ET.fromstring(xr.text.encode("utf-8"))
    except Exception as e:
        print("XML parse error:", e); print(xr.text[:1000]); continue

    print("\n--- FULL TREE ---")
    for name, val in walk(root):
        print(f"{name:<50} | {val}" if val else f"{name:<50} |")

    print("\n--- ALL DISTINCT TAGS ---")
    tags = sorted({re.sub(r'\{.*?\}', '', e.tag) for e in root.iter()})
    print("   " + ", ".join(tags))
    shown += 1
    print()

print("=" * 78)
print("PROBE COMPLETE")
print("=" * 78)
