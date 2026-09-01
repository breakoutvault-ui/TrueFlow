import requests, collections
from datetime import date, timedelta

SB = "https://tsgltaqbxtisebqmbffg.supabase.co"
K = open("/root/trueflow/.sbkey").read().strip()
H = {"apikey": K, "Authorization": "Bearer " + K}
HW = {**H, "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"}

ACTIONABLE_DAYS = 21

CONFLICT = {
    "armed services": ["Industrials", "Technology"],
    "financial services": ["Financial Services", "Real Estate"],
    "banking": ["Financial Services", "Real Estate"],
    "energy and commerce": ["Energy", "Healthcare", "Utilities", "Communication Services"],
    "energy and natural resources": ["Energy", "Utilities", "Basic Materials"],
    "agriculture": ["Consumer Defensive", "Basic Materials"],
    "health": ["Healthcare"],
    "transportation": ["Industrials"],
    "commerce, science": ["Technology", "Communication Services", "Industrials"],
    "homeland security": ["Industrials", "Technology"],
    "intelligence": ["Technology", "Industrials"],
    "veterans": ["Healthcare"],
    "judiciary": ["Technology", "Communication Services"],
    "natural resources": ["Energy", "Basic Materials", "Utilities"],
    "science, space": ["Technology"],
}


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
tr = page(SB + "/rest/v1/us_congress_trades?select=*")
mem = page(SB + "/rest/v1/us_congress_members?select=full_name,last_name,party,committees")
print(f"  trades {len(tr)} | members {len(mem)}")

bylast = {}
for m in mem:
    bylast.setdefault((m["last_name"] or "").lower(), []).append(m)


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


live = [t for t in tr if not t.get("is_amendment")]
print("  non-amendment:", len(live))
print(f"  name match rate: {sum(1 for t in live if match(t.get('member_name')))}/{len(live)}")

today = date.today()
d30 = today - timedelta(days=30)

insider, activist, delayed = set(), set(), set()
try:
    for s in page(SB + "/rest/v1/us_smart_money_scores?select=symbol,is_cluster"):
        if s.get("is_cluster"):
            insider.add(s["symbol"])
    print("  insider clusters:", len(insider))
except Exception as e:
    print("  insider load failed:", type(e).__name__, str(e)[:120])

try:
    for s in page(SB + "/rest/v1/us_sec_stakes?select=symbol,is_activist"):
        if s.get("is_activist"):
            activist.add(s["symbol"])
    print("  activist stakes:", len(activist))
except Exception as e:
    print("  activist load failed:", type(e).__name__, str(e)[:120])

try:
    for s in page(SB + "/rest/v1/us_earnings_moves?select=symbol,is_delayed_ep"):
        if s.get("is_delayed_ep"):
            delayed.add(s["symbol"])
    print("  delayed EP:", len(delayed))
except Exception as e:
    print("  delayed EP load failed:", type(e).__name__, str(e)[:120])

g = collections.defaultdict(list)
for t in live:
    if t.get("ticker"):
        g[t["ticker"]].append(t)
print("  tickers:", len(g))

rows = []
for tk, ts in g.items():
    buys = [x for x in ts if x["txn_type"] == "buy"]
    sells = [x for x in ts if x["txn_type"] == "sell"]
    b30 = [x for x in buys if x["disclosure_date"] and x["disclosure_date"] >= d30.isoformat()]
    parties = collections.Counter()
    conflict = False
    for x in buys:
        m = match(x["member_name"])
        if not m:
            continue
        parties[m["party"]] += 1
        sec = x.get("sector")
        for c in (m.get("committees") or []):
            cl = c.lower()
            for key, secs in CONFLICT.items():
                if key in cl and sec in secs:
                    conflict = True
    rets = [x["ret_since_disclosure"] for x in buys if x["ret_since_disclosure"] is not None]
    lastb = max((x["disclosure_date"] for x in buys if x["disclosure_date"]), default=None)
    ci, ca, cd = tk in insider, tk in activist, tk in delayed
    rows.append({
        "ticker": tk,
        "company": (ts[0].get("company") or "")[:200],
        "sector": ts[0].get("sector"),
        "industry": ts[0].get("industry"),
        "in_universe": bool(ts[0].get("in_universe")),
        "buys_6m": len(buys),
        "sells_6m": len(sells),
        "distinct_buyers": len({x["member_name"] for x in buys}),
        "distinct_buyers_30d": len({x["member_name"] for x in b30}),
        "buyers_republican": parties.get("Republican", 0),
        "buyers_democrat": parties.get("Democrat", 0),
        "buyers_independent": parties.get("Independent", 0),
        "net_amount_mid": round(sum(x["amount_mid"] or 0 for x in buys) - sum(x["amount_mid"] or 0 for x in sells), 2),
        "last_buy_date": lastb,
        "last_sell_date": max((x["disclosure_date"] for x in sells if x["disclosure_date"]), default=None),
        "best_ret_since_disclosure": max(rets) if rets else None,
        "is_cluster": len({x["member_name"] for x in b30}) >= 3,
        "has_committee_conflict": conflict,
        "conv_insider_cluster": ci,
        "conv_activist_stake": ca,
        "conv_delayed_ep": cd,
        "has_convergence": (ci or ca or cd),
        "still_actionable": bool(lastb and lastb >= (today - timedelta(days=ACTIONABLE_DAYS)).isoformat()),
    })

print("built:", len(rows))
ok = 0
for i in range(0, len(rows), 200):
    b = rows[i:i + 200]
    r = requests.post(SB + "/rest/v1/us_congress_ticker_agg", headers=HW, json=b, timeout=120)
    if r.status_code >= 300:
        print("FAILED", i, r.status_code, r.text[:300])
        break
    ok += len(b)
print("DONE:", ok)

print("congress clusters (3+ buyers/30d):", sum(1 for r in rows if r["is_cluster"]))
print("committee conflicts:", sum(1 for r in rows if r["has_committee_conflict"]))
print("convergence ANY:", sum(1 for r in rows if r["has_convergence"]))
print("  - insider cluster:", sum(1 for r in rows if r["conv_insider_cluster"]))
print("  - activist stake:", sum(1 for r in rows if r["conv_activist_stake"]))
print("  - delayed EP:", sum(1 for r in rows if r["conv_delayed_ep"]))
print(f"still actionable ({ACTIONABLE_DAYS}d):", sum(1 for r in rows if r["still_actionable"]))
print("in universe:", sum(1 for r in rows if r["in_universe"]))

print("\nTOP BY DISTINCT BUYERS (30d):")
for r in sorted(rows, key=lambda z: -z["distinct_buyers_30d"])[:10]:
    print(f"  {r['ticker']:6} {r['distinct_buyers_30d']}/30d  {r['distinct_buyers']}/6m | R{r['buyers_republican']}/D{r['buyers_democrat']} | {(r['sector'] or '-')[:22]:22} | conf={r['has_committee_conflict']} conv={r['has_convergence']}")

print("\nCONVERGENCE + ACTIONABLE (the ones that matter):")
hits = [r for r in rows if r["has_convergence"] and r["still_actionable"] and r["buys_6m"] > 0]
for r in sorted(hits, key=lambda z: -z["distinct_buyers"])[:15]:
    src = ",".join(s for s, v in [("insider", r["conv_insider_cluster"]), ("activist", r["conv_activist_stake"]), ("delayedEP", r["conv_delayed_ep"])] if v)
    print(f"  {r['ticker']:6} buyers={r['distinct_buyers']} last={r['last_buy_date']} | {src} | conf={r['has_committee_conflict']} | {(r['sector'] or '-')[:20]}")
if not hits:
    print("  (none)")
