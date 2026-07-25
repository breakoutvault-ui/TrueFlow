#!/usr/bin/env python3
"""
Probe: where does the FILER (investor) name actually live in a modern 13D?
Read-only. Takes one real activist filing from our own table, fetches it, and
shows the document structure so we stop guessing.

Run: /root/trueflow/bin/python /root/trueflow/probe_filer.py
"""
import re, time, requests

SUPABASE_URL = "https://tsgltaqbxtisebqmbffg.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRzZ2x0YXFieHRpc2VicW1iZmZnIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzU1NTEyMDEsImV4cCI6MjA5MTEyNzIwMX0.SQGq9E67S7j977RUA-oJqDGV8KgEhQZb0nHfAvHcFys"
SB = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
UA = {"User-Agent": "TrueFlow personal research breakoutvault@gmail.com",
      "Accept-Encoding": "gzip, deflate"}
S = requests.Session(); S.headers.update(UA)

print("=" * 74)
print("13D FILER-NAME PROBE")
print("=" * 74)

# 1) grab a few recent activist filings we already stored
r = requests.get(f"{SUPABASE_URL}/rest/v1/us_sec_stakes"
                 f"?select=symbol,form_type,filing_date,filing_url,accession_no,pct_of_class"
                 f"&is_activist=is.true&order=filing_date.desc&limit=3", headers=SB, timeout=40)
rows = r.json()
print(f"\nsample activist filings from our table: {len(rows)}")
for x in rows:
    print("  ", x["symbol"], x["form_type"], x["filing_date"], x["filing_url"])

if not rows:
    print("no rows — stop")
    raise SystemExit

for row in rows[:2]:
    print("\n" + "=" * 74)
    print(f"INSPECTING {row['symbol']}  {row['form_type']}  {row['filing_date']}")
    print("=" * 74)
    base = row["filing_url"].rstrip("/")

    # 2) list the files in the filing folder
    idx = S.get(base + "/index.json", timeout=30)
    time.sleep(0.2)
    if idx.status_code == 200:
        try:
            items = idx.json()["directory"]["item"]
            print("\nfiles in this filing:")
            for it in items[:14]:
                print(f"   {it.get('name'):45} {it.get('size'):>10}  {it.get('type','')}")
        except Exception as e:
            print("index.json parse issue:", e)
            items = []
    else:
        print("index.json status:", idx.status_code); items = []

    # 3) the full submission .txt holds the SEC header with FILED BY
    acc = row["accession_no"]
    txt_url = f"{base}/{acc}.txt"
    print(f"\nfetching header from {txt_url}")
    rr = S.get(txt_url, timeout=45, stream=True)
    time.sleep(0.2)
    if rr.status_code != 200:
        print("   status:", rr.status_code)
    else:
        head = rr.raw.read(40000, decode_content=True).decode("utf-8", "ignore")
        rr.close()
        subj = re.search(r"SUBJECT COMPANY:(.*?)(?:FILED BY|</SEC-HEADER>)", head, re.S)
        filed = re.search(r"FILED BY:(.*?)(?:</SEC-HEADER>|SUBJECT COMPANY:)", head, re.S)
        def pull(block, label):
            if not block: return None
            m = re.search(label + r":\s*([^\n]+)", block.group(1))
            return m.group(1).strip() if m else None
        print("   SUBJECT name :", pull(subj, "COMPANY CONFORMED NAME"))
        print("   FILED BY name:", pull(filed, "COMPANY CONFORMED NAME"))
        print("   FILED BY CIK :", pull(filed, "CENTRAL INDEX KEY"))
        n = head.count("FILED BY:")
        print(f"   'FILED BY:' blocks in header: {n}")
        if not filed:
            print("\n   --- header first 1500 chars (no FILED BY found) ---")
            print("   " + head[:1500].replace("\n", "\n   "))

    # 4) look at the primary document itself — is it XML or HTML?
    prim = None
    for it in (items or []):
        nm = (it.get("name") or "").lower()
        if nm.endswith((".htm", ".html", ".xml")) and "index" not in nm:
            prim = it.get("name"); break
    if prim:
        purl = f"{base}/{prim}"
        print(f"\nprimary document: {prim}")
        pr = S.get(purl, timeout=40)
        time.sleep(0.2)
        if pr.status_code == 200:
            body = pr.text
            print("   looks like XML:", body.lstrip()[:80].startswith("<?xml") or "<edgarSubmission" in body[:3000])
            # any tag that mentions a reporting person / name
            tags = re.findall(r"<([a-zA-Z][a-zA-Z0-9_]{2,40})>", body[:60000])
            interesting = sorted({x for x in tags if re.search(r"name|person|filer|owner", x, re.I)})
            print("   name-ish XML tags:", interesting[:20] if interesting else "none")
            for tag in interesting[:6]:
                m = re.search(r"<" + tag + r">\s*([^<]{2,90})</" + tag + r">", body)
                if m:
                    print(f"      <{tag}> = {m.group(1).strip()[:80]}")
            plain = re.sub(r"<[^>]+>", " ", body)
            plain = re.sub(r"&nbsp;?", " ", plain)
            plain = re.sub(r"\s+", " ", plain)
            m = re.search(r"(NAMES?\s+OF\s+REPORTING\s+PERSONS?.{0,160})", plain, re.I)
            print("   'NAMES OF REPORTING PERSON' context:", (m.group(1)[:160] if m else "NOT FOUND"))
        else:
            print("   status:", pr.status_code)

print("\n" + "=" * 74)
print("PROBE COMPLETE")
print("=" * 74)
