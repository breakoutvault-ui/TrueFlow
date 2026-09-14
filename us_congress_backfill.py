import requests, collections

SB = "https://tsgltaqbxtisebqmbffg.supabase.co"
K = open("/root/trueflow/.sbkey").read().strip()
H = {"apikey": K, "Authorization": "Bearer " + K}
HW = {**H, "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"}


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


print("loading...")
tr = page(SB + "/rest/v1/us_congress_trades?select=id,member_name,chamber")
mem = page(SB + "/rest/v1/us_congress_members?select=bioguide,full_name,last_name,first_name,party,chamber,state_dst,committees,ruling_side")
print(f"  trades {len(tr)} | members {len(mem)}")

bylast = {}
for m in mem:
    bylast.setdefault((m["last_name"] or "").lower(), []).append(m)


def match(name, chamber=None):
    p = (name or "").split()
    if not p:
        return None
    c = bylast.get(p[-1].lower()) or (bylast.get(p[-2].lower()) if len(p) > 1 else None)
    if not c:
        return None
    if chamber:
        ch = [m for m in c if m.get("chamber") == chamber]
        if ch:
            c = ch
    if len(c) == 1:
        return c[0]
    for m in c:
        if p[0].lower() in (m["full_name"] or "").lower():
            return m
    return c[0]


rows = []
miss = collections.Counter()
for t in tr:
    m = match(t.get("member_name"), t.get("chamber"))
    if not m:
        miss[t.get("member_name")] += 1
        continue
    rows.append({
        "id": t["id"],
        "bioguide": m["bioguide"],
        "party": m["party"],
        "ruling_side": m["ruling_side"],
        "state_dst": m["state_dst"],
        "committees": m["committees"] or [],
    })

print(f"matched: {len(rows)}/{len(tr)}")
if miss:
    print("unmatched names:", dict(miss.most_common(15)))

ok = 0
for i in range(0, len(rows), 200):
    b = rows[i:i + 200]
    r = requests.post(SB + "/rest/v1/us_congress_trades", headers=HW, json=b, timeout=120)
    if r.status_code >= 300:
        print("FAILED", i, r.status_code, r.text[:300])
        break
    ok += len(b)
    if ok % 1000 == 0 or ok == len(rows):
        print("  upserted", ok)
print("DONE:", ok)

chk = page(SB + "/rest/v1/us_congress_trades?select=party,ruling_side,committees")
print("party populated:", sum(1 for x in chk if x["party"]), "/", len(chk))
print("ruling_side populated:", sum(1 for x in chk if x["ruling_side"] is not None), "/", len(chk))
print("committees populated:", sum(1 for x in chk if x["committees"]), "/", len(chk))
print("party split:", dict(collections.Counter(x["party"] for x in chk if x["party"])))
