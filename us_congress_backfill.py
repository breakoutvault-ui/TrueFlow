"""
TrueFlow - Congress trades member backfill   (v2)
Fills bioguide, party, ruling_side, committees on us_congress_trades
by joining to us_congress_members on member name.

The match() and page() functions are copied VERBATIM from
us_congress_agg.py so that the per-trade party shown in the dashboard
can never disagree with the aggregated party counts.

v2 adds DIAGNOSTICS ONLY. The write path is unchanged from v1 and still
uses the original match(). The new reporting runs in test mode and
writes nothing.

Usage:
    /root/trueflow/bin/python us_congress_backfill.py test   # prints only, writes nothing
    /root/trueflow/bin/python us_congress_backfill.py full   # writes

Writes via PATCH grouped by member (never upsert) so that no row can
ever be created by this script - it can only update rows that exist.
"""

import sys
import re
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


print(f"MODE = {MODE}   (v2)")
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


# ---- same decisions as match(), instrumented to report WHICH branch fired --
def match_traced(name):
    """Returns (member_or_None, path, candidates). Decides identically to match()."""
    p = (name or "").split()
    if not p:
        return None, "no_name", []
    c = bylast.get(p[-1].lower()) or (bylast.get(p[-2].lower()) if len(p) > 1 else None)
    if not c:
        return None, "surname_not_found", []
    if len(c) == 1:
        return c[0], "unique_surname", c
    for m in c:
        if p[0].lower() in (m["full_name"] or "").lower():
            return m, "firstname_resolved", c
    return c[0], "ARBITRARY_c0", c
# ---------------------------------------------------------------------------


# ---- PROPOSED improved matcher - PREVIEW ONLY, never used to write --------
SUFFIX = {"jr", "sr", "ii", "iii", "iv", "v", "md", "phd"}


def norm(tok):
    return re.sub(r"[^a-z]", "", (tok or "").lower())


bylast2 = {}
for m in mem:
    bylast2.setdefault(norm(m["last_name"]), []).append(m)


def match2(name):
    """Returns (member_or_None, path). Strips punctuation and name suffixes,
    and tries a two-token surname (e.g. 'Van Epps') before a single token.
    Refuses to guess when candidates remain ambiguous."""
    p = [norm(t) for t in (name or "").split()]
    p = [t for t in p if t]
    if not p:
        return None, "no_name"
    q = p[:]
    while q and q[-1] in SUFFIX:
        q.pop()
    if not q:
        return None, "all_suffix"
    keys = []
    if len(q) >= 2:
        keys.append(q[-2] + q[-1])      # 'van' + 'epps' -> 'vanepps'
    keys.append(q[-1])
    if len(q) >= 2:
        keys.append(q[-2])
    for k in keys:
        c = bylast2.get(k)
        if not c:
            continue
        if len(c) == 1:
            return c[0], "unique_surname"
        for m in c:
            if q[0] in norm(m["full_name"]):
                return m, "firstname_resolved"
        return None, "AMBIGUOUS_unresolved"   # refuses to guess
    return None, "surname_not_found"
# ---------------------------------------------------------------------------


# group trade ids by the member they matched to  (original match(), unchanged)
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

amd = sum(1 for t in tr if t.get("is_amendment"))
amd_matched = sum(1 for t in tr if t.get("is_amendment") and match(t.get("member_name")))
print(f"  of which amendments: {amd} total, {amd_matched} matched "
      f"(agg.py skips these entirely)")

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

# ===========================================================================
# v2 DIAGNOSTIC 1 - which branch of match() decided each row
# ===========================================================================
print("\n" + "=" * 70)
print("DIAGNOSTIC 1 - how match() reached its decision")
print("=" * 70)

path_rows = collections.Counter()
arb = collections.defaultdict(int)      # member_name -> row count
arb_cands = {}

for t in tr:
    nm = t.get("member_name")
    m, path, cands = match_traced(nm)
    path_rows[path] += 1
    if path == "ARBITRARY_c0":
        arb[nm] += 1
        arb_cands[nm] = cands

for p, n in path_rows.most_common():
    flag = "   <-- SILENT GUESS" if p == "ARBITRARY_c0" else ""
    print(f"  {p:22} {n:6} rows{flag}")

if arb:
    print(f"\n  {sum(arb.values())} rows were decided by the arbitrary c[0] fallback,")
    print(f"  across {len(arb)} distinct filed names:\n")
    for nm, n in sorted(arb.items(), key=lambda z: -z[1]):
        print(f"    {n:5} rows  filed as: {nm}")
        for idx, cand in enumerate(arb_cands[nm]):
            mark = "  <-- CHOSEN" if idx == 0 else ""
            print(f"             candidate: {(cand['full_name'] or '')[:34]:34} "
                  f"{str(cand['party'])[:11]:11}{mark}")
        parties = {c.get("party") for c in arb_cands[nm]}
        if len(parties) > 1:
            print("             *** candidates span parties "
                  f"{sorted(str(x) for x in parties)} "
                  "- a wrong pick shows a WRONG PARTY ***")
        print()
else:
    print("\n  No rows used the arbitrary fallback. match() never guessed.")

# ===========================================================================
# v2 DIAGNOSTIC 2 - would the improved matcher help, and would it break
#                   anything that currently works?  PREVIEW ONLY.
# ===========================================================================
print("=" * 70)
print("DIAGNOSTIC 2 - improved matcher PREVIEW (nothing is written from this)")
print("=" * 70)

distinct = collections.Counter((t.get("member_name") or "") for t in tr)

recovered = []      # currently NULL, match2 finds a member
changed = []        # currently matched, match2 disagrees
still_missing = []  # unmatched by both
agreed = 0

for nm, nrows in distinct.items():
    old = match(nm)
    new, npath = match2(nm)
    if old is None and new is not None:
        recovered.append((nm, nrows, new, npath))
    elif old is None and new is None:
        still_missing.append((nm, nrows, npath))
    elif old is not None and new is None:
        changed.append((nm, nrows, old, None, npath))
    elif old["bioguide"] != new["bioguide"]:
        changed.append((nm, nrows, old, new, npath))
    else:
        agreed += nrows

print(f"\n  agreed with current matcher: {agreed} rows")

print(f"\n  WOULD RECOVER {sum(r[1] for r in recovered)} rows "
      f"({len(recovered)} names) that are NULL today:")
for nm, nrows, m, npath in sorted(recovered, key=lambda z: -z[1]):
    print(f"    {nrows:5} rows  {nm[:32]:32} -> {(m['full_name'] or '')[:26]:26} "
          f"{str(m['party'])[:11]:11} ({npath})")
if not recovered:
    print("    (none)")

print(f"\n  WOULD CHANGE {sum(r[1] for r in changed)} rows "
      f"({len(changed)} names) that are matched today:")
for nm, nrows, old, new, npath in sorted(changed, key=lambda z: -z[1]):
    newtxt = (new["full_name"] if new else "NO MATCH (refuses to guess)")
    print(f"    {nrows:5} rows  {nm[:30]:30}")
    print(f"           now: {(old['full_name'] or '')[:30]:30} {str(old['party'])[:11]}")
    print(f"           new: {str(newtxt)[:30]:30} "
          f"{str(new['party'])[:11] if new else '-'} ({npath})")
if not changed:
    print("    (none - the improved matcher agrees with every current match)")

print(f"\n  STILL UNMATCHED: {sum(r[1] for r in still_missing)} rows "
      f"({len(still_missing)} names)")
for nm, nrows, npath in sorted(still_missing, key=lambda z: -z[1]):
    print(f"    {nrows:5} rows  {(nm or '(blank)')[:40]:40} ({npath})")
if not still_missing:
    print("    (none)")

print("\n" + "=" * 70)
print("Diagnostics above are PREVIEW ONLY. The write path below still uses")
print("the original match(), unchanged from v1 and identical to agg.py.")
print("=" * 70)

if MODE == "test":
    print("\nTEST MODE - nothing written. Re-run with 'full' to write.")
    sys.exit(0)


# ---- write  (unchanged from v1) -------------------------------------------
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

ver = {**H, "Prefer": "count=exact", "Range": "0-0"}
for f in ("", "&party=is.null", "&ruling_side=is.null",
          "&bioguide=is.null", "&committees=is.null"):
    r = requests.get(SB + "/rest/v1/us_congress_trades?select=id" + f,
                     headers=ver, timeout=60)
    print(f"  {(f or 'TOTAL'):22} {r.headers.get('content-range')}")
