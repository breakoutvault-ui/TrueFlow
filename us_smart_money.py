#!/usr/bin/env python3
"""
TrueFlow US Smart Money Scanner
================================
Source: SEC EDGAR Form 4 insider filings (free, no auth).
Captures open-market insider BUYs (code P) and SELLs (code S) for the
US universe, stores them in us_smart_money_deals, and computes
us_smart_money_scores (tier + cooking score) mirroring the Indian
smart money system.

Modes (auto-detected):
  - BACKFILL : us_smart_money_deals empty -> process last 90 days
  - DAILY    : process the last 5 calendar days (catches late filings),
               dedupe by accession number, recompute scores.

Run:  /root/trueflow/bin/python /root/trueflow/us_smart_money.py
"""

import os, re, sys, time, gzip, json, logging, datetime as dt
from io import BytesIO
from xml.etree import ElementTree as ET

import requests

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
SUPABASE_URL = "https://tsgltaqbxtisebqmbffg.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRzZ2x0YXFieHRpc2VicW1iZmZnIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzU1NTEyMDEsImV4cCI6MjA5MTEyNzIwMX0.SQGq9E67S7j977RUA-oJqDGV8KgEhQZb0nHfAvHcFys"

TELEGRAM_TOKEN = os.environ.get("TF_TG_TOKEN", "")
TELEGRAM_CHAT  = "1202026803"

SEC_UA = {"User-Agent": "TrueFlow personal research breakoutvault@gmail.com",
          "Accept-Encoding": "gzip, deflate"}

BACKFILL_DAYS   = 90     # calendar days for first run
DAILY_LOOKBACK  = 5      # calendar days re-checked every daily run
MIN_TX_VALUE    = 10_000 # ignore micro transactions below $10k
REQ_SLEEP       = 0.15   # ~6-7 req/sec (SEC fair-access limit is 10/sec)
CLUSTER_WINDOW      = 30   # days — window in which a "cluster" must form
CLUSTER_MIN_BUYERS  = 3    # distinct insiders buying to count as a cluster
CLUSTER_MIN_RATIO   = 2.0  # buy value must be >= 2x sell value in that window
STAR_TITLE_RE   = re.compile(r"c\.?e\.?o|chief exec|c\.?f\.?o|chief fin|president|chair", re.I)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("us_sm")

S = requests.Session()
S.headers.update(SEC_UA)

SB_HEADERS = {"apikey": SUPABASE_KEY,
              "Authorization": f"Bearer {SUPABASE_KEY}",
              "Content-Type": "application/json"}

# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def sec_get(url, retries=4):
    for i in range(retries):
        try:
            r = S.get(url, timeout=30)
            if r.status_code == 200:
                time.sleep(REQ_SLEEP)
                return r
            if r.status_code == 404:
                time.sleep(REQ_SLEEP)
                return None
            if r.status_code in (403, 429, 503):
                wait = 5 * (i + 1)
                log.warning("SEC %s on %s — cooling %ss", r.status_code, url[-60:], wait)
                time.sleep(wait)
                continue
            time.sleep(REQ_SLEEP)
        except Exception as e:
            log.warning("SEC fetch error %s (%s) try %d", url[-60:], e, i + 1)
            time.sleep(3 * (i + 1))
    return None


def sb_get(path):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=SB_HEADERS, timeout=60)
    r.raise_for_status()
    return r.json()


def sb_upsert(table, rows, on_conflict):
    if not rows:
        return
    h = dict(SB_HEADERS)
    h["Prefer"] = "resolution=merge-duplicates"
    for i in range(0, len(rows), 500):
        chunk = rows[i:i + 500]
        r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}",
                          headers=h, json=chunk, timeout=120)
        if r.status_code >= 300:
            log.error("Supabase upsert %s failed: %s %s", table, r.status_code, r.text[:300])


def telegram(msg):
    if not TELEGRAM_TOKEN:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT, "text": msg,
                            "parse_mode": "HTML"}, timeout=20)
    except Exception as e:
        log.warning("telegram failed: %s", e)

# ----------------------------------------------------------------------------
# universe + CIK map
# ----------------------------------------------------------------------------

def load_universe():
    rows = sb_get("us_universe?select=symbol,name")
    syms = {r["symbol"].upper(): r.get("name") or "" for r in rows}
    log.info("Universe: %d symbols", len(syms))
    return syms


def load_cik_map(universe):
    """SEC ticker->CIK mapping. SEC uses '-' for class shares (BRK-B) same as yfinance."""
    r = sec_get("https://www.sec.gov/files/company_tickers.json")
    if r is None:
        raise RuntimeError("Could not fetch SEC company_tickers.json")
    data = r.json()
    cik2sym = {}
    for item in data.values():
        t = str(item.get("ticker", "")).upper()
        if t in universe:
            cik2sym[int(item["cik_str"])] = t
    log.info("CIK map: %d of %d universe symbols matched", len(cik2sym), len(universe))
    return cik2sym

# ----------------------------------------------------------------------------
# EDGAR daily index -> Form 4 filings
# ----------------------------------------------------------------------------

def quarter_of(d):
    return (d.month - 1) // 3 + 1


def day_form4_entries(day, cik2sym):
    """Return [(cik, accession_path)] of Form 4 filings for one date."""
    url = (f"https://www.sec.gov/Archives/edgar/daily-index/"
           f"{day.year}/QTR{quarter_of(day)}/form.{day.strftime('%Y%m%d')}.idx")
    r = sec_get(url)
    if r is None:
        return []
    out = []
    for line in r.text.splitlines():
        if not line.startswith("4 ") and not line.startswith("4/A "):
            continue
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 5:
            continue
        form, _company, cik_s, _date, path = parts[0], parts[1], parts[2], parts[3], parts[4]
        if form != "4":            # skip amendments to keep signal clean
            continue
        try:
            cik = int(cik_s)
        except ValueError:
            continue
        if cik in cik2sym:
            out.append((cik, path.strip()))
    return out

# ----------------------------------------------------------------------------
# Form 4 parsing
# ----------------------------------------------------------------------------

def _txt(el, path):
    if el is None:
        return ""
    node = el.find(path)
    if node is None:
        return ""
    return (node.text or "").strip()


def parse_form4(txt_content):
    """Extract ownershipDocument XML from the full .txt submission and parse it."""
    m = re.search(r"<XML>(.*?)</XML>", txt_content, re.S | re.I)
    if not m:
        return None
    xml_raw = m.group(1).strip()
    xml_raw = xml_raw[xml_raw.find("<"):]
    try:
        root = ET.fromstring(xml_raw)
    except ET.ParseError:
        xml_raw = re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;)", "&amp;", xml_raw)
        try:
            root = ET.fromstring(xml_raw)
        except ET.ParseError:
            return None

    owner = root.find("reportingOwner")
    name = _txt(owner, "reportingOwnerId/rptOwnerName").title()
    rel = owner.find("reportingOwnerRelationship") if owner is not None else None
    is_dir   = _txt(rel, "isDirector") in ("1", "true")
    is_off   = _txt(rel, "isOfficer") in ("1", "true")
    is_ten   = _txt(rel, "isTenPercentOwner") in ("1", "true")
    title    = _txt(rel, "officerTitle")

    if is_ten:
        inv_class = "institutional"
    elif is_off:
        inv_class = "officer"
    elif is_dir:
        inv_class = "director"
    else:
        inv_class = "other"

    is_star = bool(is_ten or (title and STAR_TITLE_RE.search(title)))
    role = title or ("10% Owner" if is_ten else ("Director" if is_dir else "Insider"))

    txs = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _txt(tx, "transactionCoding/transactionCode").upper()
        if code not in ("P", "S"):
            continue
        d = _txt(tx, "transactionDate/value")
        try:
            shares = float(_txt(tx, "transactionAmounts/transactionShares/value") or 0)
            price = float(_txt(tx, "transactionAmounts/transactionPricePerShare/value") or 0)
        except ValueError:
            continue
        value = shares * price
        if value < MIN_TX_VALUE or not d:
            continue
        txs.append({"date": d[:10],
                    "side": "BUY" if code == "P" else "SELL",
                    "qty": shares, "price": price, "value": value})
    if not txs:
        return None
    return {"name": name, "inv_class": inv_class, "is_star": is_star,
            "role": role, "txs": txs}

# ----------------------------------------------------------------------------
# ingest
# ----------------------------------------------------------------------------

def existing_accessions(since):
    try:
        rows = sb_get(f"us_smart_money_deals?select=accession_no&deal_date=gte.{since}&limit=100000")
        return {r["accession_no"] for r in rows}
    except Exception:
        return set()


def ingest_days(days, cik2sym, universe, skip_accessions):
    rows, filings_done, filings_hit = [], 0, 0
    for day in days:
        entries = day_form4_entries(day, cik2sym)
        if not entries:
            continue
        log.info("%s: %d in-universe Form 4 filings", day, len(entries))
        for cik, path in entries:
            acc_m = re.search(r"(\d{10}-\d{2}-\d{6})", path)
            accession = acc_m.group(1) if acc_m else path
            if accession in skip_accessions:
                continue
            skip_accessions.add(accession)
            r = sec_get("https://www.sec.gov/Archives/" + path.lstrip("/"))
            filings_done += 1
            if r is None:
                continue
            parsed = parse_form4(r.text)
            if not parsed:
                continue
            sym = cik2sym[cik]
            filings_hit += 1
            for i, tx in enumerate(parsed["txs"]):
                rows.append({
                    "accession_no": accession, "tx_seq": i,
                    "symbol": sym, "scrip_name": universe.get(sym, ""),
                    "client_name": f'{parsed["name"]} ({parsed["role"]})'[:120],
                    "investor_class": parsed["inv_class"],
                    "is_star": parsed["is_star"],
                    "side": tx["side"], "quantity": tx["qty"],
                    "price": round(tx["price"], 4),
                    "value": round(tx["value"], 2),
                    "deal_type": "insider", "deal_date": tx["date"],
                })
        if len(rows) >= 400:
            sb_upsert("us_smart_money_deals", rows, "accession_no,tx_seq")
            rows = []
    sb_upsert("us_smart_money_deals", rows, "accession_no,tx_seq")
    log.info("Ingest done: %d filings fetched, %d had open-market P/S trades",
             filings_done, filings_hit)
    return filings_done, filings_hit

# ----------------------------------------------------------------------------
# scoring  (mirrors Indian tier system)
# ----------------------------------------------------------------------------

def compute_scores():
    # 45-day accumulation window: long enough to catch selling a 30d window misses,
    # short enough to stay "recent". 7d used only for the fast-flow column.
    since45 = (dt.date.today() - dt.timedelta(days=45)).isoformat()
    since7  = (dt.date.today() - dt.timedelta(days=7)).isoformat()
    MIN_NET_BUY = 250_000   # net buying must clear this to qualify as a buying tier
    deals = sb_get(f"us_smart_money_deals?select=symbol,client_name,is_star,side,value,deal_date"
                   f"&deal_date=gte.{since45}&limit=100000")
    by = {}
    for d in deals:
        by.setdefault(d["symbol"], []).append(d)

    out, now = [], dt.datetime.utcnow().isoformat() + "Z"
    for sym, ds in by.items():
        buys  = [d for d in ds if d["side"] == "BUY"]
        sells = [d for d in ds if d["side"] == "SELL"]
        buy30  = sum(d["value"] for d in buys)      # (name kept for column compatibility; 45d window)
        sell30 = sum(d["value"] for d in sells)
        net30  = buy30 - sell30
        net7   = sum(d["value"] for d in buys  if d["deal_date"] >= since7) - \
                 sum(d["value"] for d in sells if d["deal_date"] >= since7)
        buyers = {}
        for d in buys:
            buyers.setdefault(d["client_name"], set()).add(d["deal_date"])
        n_buyers = len(buyers)
        repeat   = any(len(v) >= 2 for v in buyers.values())
        stars    = len({d["client_name"] for d in buys if d["is_star"]})
        last_deal = max(d["deal_date"] for d in ds)

        # selling that dwarfs buying always wins, regardless of who nibbled
        heavy_selling = sell30 > 2 * max(buy30, 1)

        # ── CLUSTER DETECTION ────────────────────────────────────────────
        # Research (Lakonishok & Lee; Cohen/Malloy/Pomorski): several insiders
        # buying in the same short window is roughly twice as predictive as a
        # lone buyer. We use a tighter 30-day window than the scoring window,
        # and require buying to genuinely dominate selling (ratio >= 2).
        cl_since = (dt.date.today() - dt.timedelta(days=CLUSTER_WINDOW)).isoformat()
        cl_buys  = [d for d in buys  if d["deal_date"] >= cl_since]
        cl_sells = [d for d in sells if d["deal_date"] >= cl_since]
        cl_buy_val  = sum(d["value"] for d in cl_buys)
        cl_sell_val = sum(d["value"] for d in cl_sells)
        cl_buyers_map = {}
        for d in cl_buys:
            cl_buyers_map.setdefault(d["client_name"], []).append(d["deal_date"])
        cluster_buyers = len(cl_buyers_map)
        cluster_stars  = len({d["client_name"] for d in cl_buys if d["is_star"]})
        buy_sell_ratio = (cl_buy_val / cl_sell_val) if cl_sell_val > 0 else (999.0 if cl_buy_val > 0 else 0.0)
        is_cluster = (cluster_buyers >= CLUSTER_MIN_BUYERS
                      and buy_sell_ratio >= CLUSTER_MIN_RATIO
                      and cl_buy_val >= MIN_NET_BUY)
        cluster_first = min((min(v) for v in cl_buyers_map.values()), default=None)
        cluster_last  = max((max(v) for v in cl_buyers_map.values()), default=None)
        # span in days over which the cluster formed — tighter is stronger
        if cluster_first and cluster_last:
            cluster_span = (dt.date.fromisoformat(cluster_last) - dt.date.fromisoformat(cluster_first)).days
        else:
            cluster_span = None

        if heavy_selling or net30 < -25_000:
            tier = "OFFLOADING"
        elif net30 >= MIN_NET_BUY and (stars >= 1 or (n_buyers >= 2 and repeat)):
            tier = "LOADING_UP"
        elif net30 >= MIN_NET_BUY and (n_buyers >= 2 or repeat):
            tier = "ACCUMULATING"
        elif net30 > 0:
            tier = "NIBBLING"
        else:
            tier = "QUIET"

        score = 0.0
        if tier in ("LOADING_UP", "ACCUMULATING", "NIBBLING"):
            score += min(40.0, net30 / 2_000_000 * 40)
            score += min(25.0, n_buyers * 8)
            score += 20.0 if stars else 0.0
            days_ago = (dt.date.today() - dt.date.fromisoformat(last_deal)).days
            score += 15 if days_ago <= 7 else (10 if days_ago <= 14 else 5)
            # cluster bonus: multiple insiders acting together is the strongest tell
            if is_cluster:
                score += 12.0
                if cluster_stars >= 1:
                    score += 6.0
                if cluster_span is not None and cluster_span <= 7:
                    score += 4.0     # tight, coordinated cluster
            score = min(score, 100.0)
        out.append({"symbol": sym, "tier": tier,
                    "cooking_score": round(score, 1),
                    "net_value_7d": round(net7, 2),
                    "net_value_30d": round(net30, 2),
                    "buy_value_30d": round(buy30, 2),
                    "sell_value_30d": round(sell30, 2),
                    "star_buyers_30d": stars,
                    "inst_buyers_30d": n_buyers,
                    "deals_30d": len(ds),
                    "last_deal": last_deal,
                    "is_cluster": bool(is_cluster),
                    "cluster_buyers": cluster_buyers,
                    "cluster_stars": cluster_stars,
                    "cluster_span_days": cluster_span,
                    "cluster_buy_value": round(cl_buy_val, 2),
                    "buy_sell_ratio": round(min(buy_sell_ratio, 999.0), 2),
                    "cluster_first_date": cluster_first,
                    "cluster_last_date": cluster_last,
                    "updated_at": now})

    # symbols with no 30d activity -> remove stale score rows
    requests.delete(f"{SUPABASE_URL}/rest/v1/us_smart_money_scores?symbol=neq.__none__",
                    headers=SB_HEADERS, timeout=60)
    sb_upsert("us_smart_money_scores", out, "symbol")
    log.info("Scores: %d symbols scored", len(out))
    return out

# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def main():
    universe = load_universe()
    cik2sym = load_cik_map(universe)

    try:
        n = sb_get("us_smart_money_deals?select=id&limit=1")
        backfill = len(n) == 0
    except Exception:
        backfill = True

    today = dt.date.today()
    span = BACKFILL_DAYS if backfill else DAILY_LOOKBACK
    days = [today - dt.timedelta(days=i) for i in range(span, -1, -1)
            if (today - dt.timedelta(days=i)).weekday() < 5]

    log.info("MODE: %s — scanning %d weekdays", "BACKFILL" if backfill else "DAILY", len(days))
    if backfill:
        telegram("🇺🇸 <b>US Smart Money</b> — backfill starting (90 days of SEC insider filings). "
                 "This will take a while ⏳")

    since = days[0].isoformat()
    skip = existing_accessions(since)
    log.info("Already have %d accessions since %s", len(skip), since)

    filings, hits = ingest_days(days, cik2sym, universe, skip)
    scores = compute_scores()

    tiers = {}
    for s in scores:
        tiers[s["tier"]] = tiers.get(s["tier"], 0) + 1
    clusters = sum(1 for s in scores if s.get("is_cluster"))
    msg = (f"🇺🇸 <b>US Smart Money — {'Backfill' if backfill else 'Daily'} complete</b>\n"
           f"Filings processed: {filings} | with open-market trades: {hits}\n"
           f"Symbols scored (30d): {len(scores)}\n"
           f"🎯 <b>Insider clusters: {clusters}</b>\n"
           f"🔥 Loading up: {tiers.get('LOADING_UP',0)} | "
           f"📈 Accumulating: {tiers.get('ACCUMULATING',0)}\n"
           f"👀 Nibbling: {tiers.get('NIBBLING',0)} | "
           f"🧊 Offloading: {tiers.get('OFFLOADING',0)}")
    telegram(msg)
    log.info("US smart money scan complete. ✅")


if __name__ == "__main__":
    main()
