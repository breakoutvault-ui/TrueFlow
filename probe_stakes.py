#!/usr/bin/env python3
"""
Probe: how does EDGAR actually index SC 13D / 13G filings?
Read-only. Writes nothing. Answers three questions with evidence:

  Q1. Does a subject company's submissions record contain 13D/13G filings?
      (my original assumption — test against companies that certainly have them)
  Q2. Does the daily form index list 13D/13G, and whose CIK is in the row —
      the investor (filer) or the target company (subject)?
  Q3. Can we read the SUBJECT COMPANY CIK out of a filing header?

Run: /root/trueflow/bin/python /root/trueflow/probe_stakes.py
"""
import re, time, json, datetime as dt
import requests

UA = {"User-Agent": "TrueFlow personal research breakoutvault@gmail.com",
      "Accept-Encoding": "gzip, deflate"}
S = requests.Session(); S.headers.update(UA)

def get(url, tries=3):
    for i in range(tries):
        try:
            r = S.get(url, timeout=30)
            if r.status_code == 200:
                time.sleep(0.15); return r
            if r.status_code == 404:
                return None
            time.sleep(3 * (i + 1))
        except Exception:
            time.sleep(2 * (i + 1))
    return None

print("=" * 72)
print("EDGAR 13D/13G INDEXING PROBE —", dt.date.today())
print("=" * 72)

# ---------------------------------------------------------------- Q1
print("\nQ1. Do 13D/13G filings appear on the SUBJECT company's submissions record?")
# well-known activist targets / widely-held names
TESTS = {"AAPL": 320193, "GTLB": 1653482, "SMCI": 1375365, "PARA": 813828}
for sym, cik in TESTS.items():
    r = get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    if r is None:
        print(f"   {sym}: submissions fetch FAILED"); continue
    try:
        rec = r.json().get("filings", {}).get("recent", {})
        forms = rec.get("form", []) or []
    except Exception as e:
        print(f"   {sym}: parse error {e}"); continue
    from collections import Counter
    c = Counter(forms)
    stake = {k: v for k, v in c.items() if k.startswith("SC 13")}
    print(f"   {sym}: {len(forms)} recent filings | 13D/G present: {stake if stake else 'NONE'}")
    print(f"        form types seen: {sorted(set(forms))[:14]}")

# ---------------------------------------------------------------- Q2
print("\nQ2. Daily form index — are 13D/13G listed, and whose CIK is in the row?")
def quarter(d): return (d.month - 1) // 3 + 1
found_rows = []
day = dt.date.today()
for back in range(1, 12):          # walk back to find a business day with data
    d = day - dt.timedelta(days=back)
    if d.weekday() >= 5:
        continue
    url = (f"https://www.sec.gov/Archives/edgar/daily-index/{d.year}/"
           f"QTR{quarter(d)}/form.{d.strftime('%Y%m%d')}.idx")
    r = get(url)
    if r is None:
        continue
    lines = [l for l in r.text.splitlines() if l.startswith("SC 13")]
    print(f"   {d}: idx OK, {len(lines)} SC 13* rows")
    for l in lines[:6]:
        parts = re.split(r"\s{2,}", l.strip())
        print("      ", parts[:5])
        found_rows.append(parts)
    if lines:
        break

# ---------------------------------------------------------------- Q3
print("\nQ3. Can we read SUBJECT COMPANY out of the filing header?")
if found_rows:
    p = found_rows[0]
    path = p[-1] if p else None
    if path:
        r = get("https://www.sec.gov/Archives/" + path.lstrip("/"))
        if r is None:
            print("   header fetch FAILED")
        else:
            head = r.text[:4000]
            subj = re.search(r"SUBJECT COMPANY:(.*?)(?:FILED BY|</SEC-HEADER>)", r.text[:20000], re.S)
            filer = re.search(r"FILED BY:(.*?)(?:</SEC-HEADER>|SUBJECT COMPANY:)", r.text[:20000], re.S)
            def pull(block, label):
                if not block: return None
                m = re.search(label + r":\s*([^\n]+)", block.group(1))
                return m.group(1).strip() if m else None
            print("   SUBJECT COMPANY name:", pull(subj, "COMPANY CONFORMED NAME"))
            print("   SUBJECT COMPANY CIK :", pull(subj, "CENTRAL INDEX KEY"))
            print("   SUBJECT ticker/sym  :", pull(subj, "TRADING SYMBOL"))
            print("   FILED BY name       :", pull(filer, "COMPANY CONFORMED NAME"))
            print("   FILED BY CIK        :", pull(filer, "CENTRAL INDEX KEY"))
            print("\n   --- first 900 chars of header for reference ---")
            print("   " + head[:900].replace("\n", "\n   "))
else:
    print("   no rows found in Q2, cannot test")

print("\n" + "=" * 72)
print("PROBE COMPLETE")
print("=" * 72)
