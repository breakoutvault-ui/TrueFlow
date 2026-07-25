#!/usr/bin/env python3
"""
Dump the real structure of a modern 13D/13G primary_doc.xml so the parser can be
written against facts, not assumptions. Read-only.

Run: /root/trueflow/bin/python /root/trueflow/probe_xml.py
"""
import re, time, requests
from xml.etree import ElementTree as ET

SUPABASE_URL = "https://tsgltaqbxtisebqmbffg.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRzZ2x0YXFieHRpc2VicW1iZmZnIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzU1NTEyMDEsImV4cCI6MjA5MTEyNzIwMX0.SQGq9E67S7j977RUA-oJqDGV8KgEhQZb0nHfAvHcFys"
SB = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
UA = {"User-Agent": "TrueFlow personal research breakoutvault@gmail.com",
      "Accept-Encoding": "gzip, deflate"}
S = requests.Session(); S.headers.update(UA)

r = requests.get(f"{SUPABASE_URL}/rest/v1/us_sec_stakes"
                 f"?select=symbol,form_type,filing_date,filing_url,pct_of_class"
                 f"&is_activist=is.true&order=filing_date.desc&limit=4", headers=SB, timeout=40)
rows = r.json()

def walk(el, depth=0, out=None, path=""):
    if out is None: out = []
    tag = re.sub(r"\{.*?\}", "", el.tag)
    txt = (el.text or "").strip()
    here = f"{path}/{tag}"
    out.append(("  " * depth + tag, txt[:90], here))
    for ch in list(el):
        walk(ch, depth + 1, out, here)
    return out

for row in rows[:2]:
    url = row["filing_url"].rstrip("/") + "/primary_doc.xml"
    print("=" * 78)
    print(f"{row['symbol']}  {row['form_type']}  {row['filing_date']}   stored pct={row['pct_of_class']}")
    print(url)
    print("=" * 78)
    rr = S.get(url, timeout=40); time.sleep(0.25)
    if rr.status_code != 200:
        print("   status", rr.status_code); continue
    body = rr.text
    try:
        root = ET.fromstring(body.encode("utf-8"))
    except ET.ParseError as e:
        print("   XML parse error:", e)
        print(body[:1200]); continue

    print("\n--- FULL TREE (tag | value) ---")
    for name, val, path in walk(root):
        if val:
            print(f"{name:<46} | {val}")
        else:
            print(f"{name:<46} |")

    print("\n--- KEY LOOKUPS ---")
    def find_all(tagname):
        return [ (e.text or '').strip() for e in root.iter()
                 if re.sub(r'\{.*?\}', '', e.tag) == tagname and (e.text or '').strip() ]
    for tag in ("issuerName", "issuerCIK", "reportingPersonName", "filingPersonName",
                "personName", "percentOfClass", "aggregateAmount", "cusip",
                "classTitle", "typeOfReportingPerson", "date5PercentOwnership"):
        vals = find_all(tag)
        print(f"   {tag:26} -> {vals[:4] if vals else '—'}")
    print()
