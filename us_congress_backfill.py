"""
TrueFlow - Congress trades member backfill
Fills bioguide, party, ruling_side, committees on us_congress_trades
by joining to us_congress_members on member name.

The match() and page() functions are copied VERBATIM from
us_congress_agg.py so that the per-trade party shown in the dashboard
can never disagree with the aggregated party counts.

Usage:
    /root/trueflow/bin/python us_congress_backfill.py test   # prints only, writes nothing
    /root/trueflow/bin/python us_congress_backfill.py full   # writes

Writes via PATCH grouped by member (never upsert) so that no row can
ever be created by this script - it can only update rows that exist.
"""

import sys
import requests
import collections

SB = "https://tsgltaqbxtisebqmbffg.supabase.co"
K = open("/root/trueflow/.sbkey").read().strip()
H = {"apikey": K, "Authorization": "Bearer " + K}
HW = {**H, "Content-Type": "application/json", "Prefer": "return=minimal"}

MODE = (sys.argv[1] if len(sys.argv) > 1 else "test").lower()
if MODE not in ("test", "full"):
    print("usage: us_congress_backfill.py [test|full]")
    sys.exit(1)

ID_CHUNK = 100          # ids per PATCH request, keeps the URL short
FAIL_LIMIT = 5          # stop after this many consecutive write failures


# ---- copied verbatim from us_congress_agg.py -------------------------------
def page(url):
    out = []
    o = 0
    while True:
        b = requests.get(url + f"&offset={o}&limit=1000", headers=H, timeout=90).json()
        if not isinstance(b, list):
            print("  ERR", b)
            break
        out += b
        o += 1000
        if len(b) < 1000:
            break
    return out
# ---------------------------------------------------------------------------


print(f"MODE = {MODE}")
print("loading...")

tr = page(SB + "/rest/v1/us_congress_trades?select=id,member_name,chamber,is_amendment")
mem = page(SB + "/rest/v1/us_congress_members"
                "?select=bioguide,full_name,last_name,party,ruling_side,committees")
print(f"  trades {len(tr)} | members {len(mem)}")

if not tr:
    print("ABORT: no trades loaded")
    sys.exit(1)
if not mem:
    print("ABORT: no members loaded")
    sys.exit(1)

bylast = {}
for m in mem:
    bylast.setdefault((m["last_name"] or "").lower(), []).append(m)


# ---- copied verbatim from us_congress_agg.py -------------------------------
def match(name):
    p = (name or "").split()
    if not p:
        return None
    c = bylast.get(p[-1].lower()) or (bylast.get(p[-2].lower()) if len(p) > 1 else None)
    if not c:
        return None
    if len(c) == 1:
        return c[0]
    for m in c:
        if p[0].lower() in (m["full_name"] or "").lower():
            return m
    return c[0]
# ---------------------------------------------------------------------------


# group trade ids by the member they matched to
groups = collections.defaultdict(list)     # bioguide -> [trade id, ...]
member_by_bio = {}
unmatched = []

for t in tr:
    m = match(t.get("member_name"))
    if not m:
        unmatched.append(t)
        continue
    groups[m["bioguide"]].append(t["id"])
    member_by_bio[m["bioguide"]] = m

matched_rows = sum(len(v) for v in groups.values())
print(f"\n  matched   {matched_rows}/{len(tr)} rows "
      f"({matched_rows * 100.0 / len(tr):.1f}%)  across {len(groups)} members")
print(f"  unmatched {len(unmatched)} rows")

# amendments are included on purpose - sub-tab B displays them
amd = sum(1 for t in tr if t.get("is_amendment"))
amd_matched = sum(1 for t in tr if t.get("is_amendment") and match(t.get("member_name")))
print(f"  of which amendments: {amd} total, {amd_matched} matched "
      f"(agg.py skips these entirely)")

# party distribution across matched TRADES (not members)
pc = collections.Counter()
rc = collections.Counter()
cc = collections.Counter()
for bio, ids in groups.items():
    m = member_by_bio[bio]
    pc[m.get("party")] += len(ids)
    rc[m.get("ruling_side")] += len(ids)
    cc[bool(m.get("committees"))] += len(ids)

print("\n  party split across matched trades:")
for k, v in pc.most_common():
    print(f"    {str(k):14} {v}")
print("  ruling_side split:")
for k, v in rc.most_common():
    print(f"    {str(k):14} {v}")
print(f"  trades whose member has committee data: {cc[True]}  "
      f"| no committee data: {cc[False]}")

if unmatched:
    names = collections.Counter((t.get("member_name") or "(blank)") for t in unmatched)
    print(f"\n  top unmatched names ({len(names)} distinct):")
    for n, c in names.most_common(15):
        print(f"    {c:5}  {n}")

print("\n  sample of what will be written:")
shown = 0
for bio, ids in groups.items():
    m = member_by_bio[bio]
    print(f"    {ids[0]:22} -> {(m['full_name'] or '')[:26]:26} "
          f"{str(m['party'])[:11]:11} ruling={str(m['ruling_side']):5} "
          f"cmte={len(m.get('committees') or [])}")
    shown += 1
    if shown >= 12:
        break

if MODE == "test":
    print("\nTEST MODE - nothing written. Re-run with 'full' to write.")
    sys.exit(0)


# ---- write ----------------------------------------------------------------
def quote(v):
    return '"' + str(v).replace('"', '') + '"'


print("\nwriting...")
written = 0
failed = 0
consec = 0

for bio, ids in groups.items():
    m = member_by_bio[bio]
    payload = {
        "bioguide": m["bioguide"],
        "party": m.get("party"),
        "ruling_side": m.get("ruling_side"),
        "committees": m.get("committees") or [],
    }
    for i in range(0, len(ids), ID_CHUNK):
        chunk = ids[i:i + ID_CHUNK]
        url = (SB + "/rest/v1/us_congress_trades?id=in.("
               + ",".join(quote(x) for x in chunk) + ")")
        try:
            r = requests.patch(url, headers=HW, json=payload, timeout=120)
        except Exception as e:
            failed += len(chunk)
            consec += 1
            print(f"  EXCEPTION {type(e).__name__} {str(e)[:160]}")
        else:
            if r.status_code >= 300:
                failed += len(chunk)
                consec += 1
                print(f"  FAILED {m.get('full_name')} {r.status_code} {r.text[:200]}")
            else:
                written += len(chunk)
                consec = 0
        if consec >= FAIL_LIMIT:
            print(f"\nABORT: {FAIL_LIMIT} consecutive write failures. "
                  f"written={written} failed={failed}")
            sys.exit(1)

print(f"\nDONE: written {written} | failed {failed} | left NULL {len(unmatched)}")

# verify from the database rather than trusting the counters above
ver = {**H, "Prefer": "count=exact", "Range": "0-0"}
for f in ("", "&party=is.null", "&ruling_side=is.null",
          "&bioguide=is.null", "&committees=is.null"):
    r = requests.get(SB + "/rest/v1/us_congress_trades?select=id" + f,
                     headers=ver, timeout=60)
    print(f"  {(f or 'TOTAL'):22} {r.headers.get('content-range')}")
