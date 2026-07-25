#!/usr/bin/env python3
"""
TrueFlow US — SEC 13D / 13G Activist & Large-Stake Tracker
===========================================================
WHAT THIS DATA IS (plain language)
----------------------------------
US law says: if an outside investor (a hedge fund, an activist, another company)
buys more than 5% of a public company's shares, they MUST tell the public within
days by filing a form with the SEC.

  • SC 13D  = "ACTIVE intent."  The filer may push for board seats, a strategy
              change, a sale of the company. This is the aggressive one, and
              historically the stock reacts strongly when it appears.
  • SC 13G  = "PASSIVE."        The filer owns a big stake but says they're just
              holding (typically index funds, pension money).
  • /A suffix = an AMENDMENT to a previous filing — often the filer INCREASING
              or decreasing their stake, which is itself a signal.

Why it matters to a swing trader: a fresh 13D is a hard, dated, public catalyst.
Something is happening at that company, and the market usually has to reprice.

HOW WE GET IT
-------------
For each stock in the universe we read that company's own SEC filing history
(data.sec.gov submissions API — free, no key). EDGAR lists 13D/13G filings on
the SUBJECT company's record, which is exactly what we want: "who filed on this
stock". We store each filing event and best-effort extract the ownership %.

Modes (auto-detected):
  BACKFILL : us_sec_stakes empty -> scan LOOKBACK_DAYS of filings
  DAILY    : only look at filings in the last DAILY_LOOKBACK days

Run: /root/trueflow/bin/python /root/trueflow/us_sec_filings.py
"""
import os, re, time, logging, datetime as dt
import requests

SUPABASE_URL = "https://tsgltaqbxtisebqmbffg.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRzZ2x0YXFieHRpc2VicW1iZmZnIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzU1NTEyMDEsImV4cCI6MjA5MTEyNzIwMX0.SQGq9E67S7j977RUA-oJqDGV8KgEhQZb0nHfAvHcFys"

TELEGRAM_TOKEN = os.environ.get("TF_TG_TOKEN", "")
TELEGRAM_CHAT  = "1202026803"

SEC_UA = {"User-Agent": "TrueFlow personal research breakoutvault@gmail.com",
          "Accept-Encoding": "gzip, deflate"}

LOOKBACK_DAYS   = 180    # first run: how far back to keep filings
DAILY_LOOKBACK  = 10     # daily run: catch late/amended filings
REQ_SLEEP       = 0.22   # SEC fair-access is 10 req/sec; stay comfortably under
FETCH_DOC_LIMIT = 400    # max filing documents to open per run for % extraction
TEST_SYMBOLS    = int(os.environ.get("TF_TEST_N", "0"))  # >0 = only scan this many symbols

# SEC renamed these forms (structured 13D/G rules, ~late 2024):
#   modern : SCHEDULE 13D / SCHEDULE 13D/A / SCHEDULE 13G / SCHEDULE 13G/A
#   legacy : SC 13D / SC 13D/A / SC 13G / SC 13G/A
# We accept both so historical filings still match.
FORMS = ("SCHEDULE 13D", "SCHEDULE 13D/A", "SCHEDULE 13G", "SCHEDULE 13G/A",
         "SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A")

def is_stake_form(f):
    s = str(f or "").strip().upper()
    return s in FORMS

def is_activist_form(f):
    """13D = active intent. 13G = passive holder."""
    s = str(f or "").strip().upper()
    return s.startswith("SCHEDULE 13D") or s.startswith("SC 13D")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("us_sec")

S = requests.Session(); S.headers.update(SEC_UA)
SB = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
      "Content-Type": "application/json"}


# ---------------------------------------------------------------- helpers
FAILS = {"403": 0, "404": 0, "other": 0, "exception": 0}

def sec_get(url, retries=4):
    """Returns a response or None. NEVER fails silently - every give-up is counted
    and logged, because a swallowed 403 looks identical to 'no data found'."""
    last = None
    for i in range(retries):
        try:
            r = S.get(url, timeout=30)
            if r.status_code == 200:
                time.sleep(REQ_SLEEP); return r
            if r.status_code == 404:
                FAILS["404"] += 1
                time.sleep(REQ_SLEEP); return None
            last = r.status_code
            if r.status_code in (403, 429, 503):
                time.sleep(5 * (i + 1)); continue
            time.sleep(REQ_SLEEP)
        except Exception as e:
            last = "exception"
            log.debug("fetch err %s: %s", url[-50:], str(e)[:70])
            time.sleep(3 * (i + 1))
    if last == 403 or last == 429 or last == 503:
        FAILS["403"] += 1
    elif last == "exception":
        FAILS["exception"] += 1
    elif last is not None:
        FAILS["other"] += 1
    log.warning("GAVE UP on %s (last status %s)", url[-60:], last)
    return None


def sb_get(path):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=SB, timeout=60)
    r.raise_for_status(); return r.json()


def sb_upsert(table, rows, on_conflict):
    if not rows:
        return
    h = dict(SB); h["Prefer"] = "resolution=merge-duplicates"
    for i in range(0, len(rows), 300):
        chunk = rows[i:i + 300]
        r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}",
                          headers=h, json=chunk, timeout=120)
        if r.status_code >= 300:
            log.warning("batch upsert failed (%s) — retrying row by row", r.status_code)
            for one in chunk:
                rr = requests.post(f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}",
                                   headers=h, json=[one], timeout=60)
                if rr.status_code >= 300:
                    log.error("row %s failed: %s %s", one.get("symbol"), rr.status_code, rr.text[:160])


def telegram(msg):
    if not TELEGRAM_TOKEN:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"}, timeout=20)
    except Exception as e:
        log.warning("telegram failed: %s", e)


# ---------------------------------------------------------------- universe / CIK
def load_universe():
    rows, off = {}, 0
    while True:
        page = sb_get(f"us_universe?select=symbol,name&limit=1000&offset={off}")
        if not page:
            break
        for r in page:
            rows[r["symbol"].upper()] = r.get("name") or ""
        if len(page) < 1000:
            break
        off += 1000
    log.info("Universe: %d symbols", len(rows))
    return rows


def load_cik_map(universe):
    r = sec_get("https://www.sec.gov/files/company_tickers.json")
    if r is None:
        raise RuntimeError("could not fetch SEC ticker->CIK map")
    out = {}
    for item in r.json().values():
        t = str(item.get("ticker", "")).upper()
        if t in universe:
            out[t] = int(item["cik_str"])
    log.info("CIK map: %d of %d symbols matched", len(out), len(universe))
    return out


# ---------------------------------------------------------------- % extraction
PCT_PATTERNS = [
    re.compile(r"PERCENT\s+OF\s+CLASS\s+REPRESENTED\s+BY\s+AMOUNT\s+IN\s+ROW.{0,120}?([0-9]{1,2}(?:\.[0-9]{1,2})?)\s*%", re.I | re.S),
    re.compile(r"Percent\s+of\s+class[^0-9%]{0,80}([0-9]{1,2}(?:\.[0-9]{1,2})?)\s*%", re.I | re.S),
    re.compile(r"aggregate\s+(?:amount|percentage)[^0-9%]{0,120}([0-9]{1,2}(?:\.[0-9]{1,2})?)\s*%", re.I | re.S),
]


def extract_pct(text):
    """Best-effort ownership % from the filing body. Returns None if unsure."""
    if not text:
        return None
    body = re.sub(r"<[^>]+>", " ", text)
    body = re.sub(r"&nbsp;?", " ", body)
    body = re.sub(r"\s+", " ", body)[:60000]
    for pat in PCT_PATTERNS:
        m = pat.search(body)
        if m:
            try:
                v = float(m.group(1))
                if 0 < v <= 100:
                    return round(v, 2)
            except ValueError:
                pass
    return None


def filer_name(text):
    """Best-effort filer (the investor) name from the cover page."""
    if not text:
        return None
    body = re.sub(r"<[^>]+>", " ", text)
    body = re.sub(r"\s+", " ", body)
    m = re.search(r"NAME[S]?\s+OF\s+REPORTING\s+PERSON[S]?\s*:?\s*(?:I\.?R\.?S\.?[^A-Za-z]{0,40})?([A-Z][A-Za-z0-9 .,&'\-/]{3,70})", body)
    if m:
        nm = m.group(1).strip(" .,")
        nm = re.sub(r"\s+(I\.?R\.?S|S\.?S\.? or).*$", "", nm, flags=re.I)
        if 3 < len(nm) < 72:
            return nm.title()
    return None


# ---------------------------------------------------------------- main
def run():
    today = dt.date.today()
    log.info("=" * 60)
    log.info("US SEC 13D/13G Stake Tracker — %s", today)
    log.info("=" * 60)

    universe = load_universe()
    cikmap = load_cik_map(universe)

    try:
        backfill = len(sb_get("us_sec_stakes?select=id&limit=1")) == 0
    except Exception:
        backfill = True
    span = LOOKBACK_DAYS if backfill else DAILY_LOOKBACK
    cutoff = (today - dt.timedelta(days=span)).isoformat()
    log.info("MODE: %s — filings on/after %s", "BACKFILL" if backfill else "DAILY", cutoff)
    if backfill:
        telegram("🇺🇸 <b>US SEC Stake Tracker</b> — backfill starting "
                 "(13D/13G activist filings). This takes a while ⏳")

    # accessions we already stored, so we never re-open the same document
    try:
        have = set()
        for _p in range(200):   # Supabase caps every response at 1000 rows -> page
            _c = sb_get(f"us_sec_stakes?select=id,accession_no&filing_date=gte.{cutoff}"
                        f"&order=id.asc&limit=1000&offset={_p*1000}")
            if not _c:
                break
            have |= {r["accession_no"] for r in _c}
            if len(_c) < 1000:
                break
    except Exception:
        have = set()
    log.info("Already stored: %d filings since %s", len(have), cutoff)

    rows, docs_opened, found, scanned, activist = [], 0, 0, 0, 0
    syms = sorted(cikmap.keys())
    if TEST_SYMBOLS > 0:
        # put known 13D/13G-heavy names first so a small test is meaningful
        seed = [s for s in ("AAPL", "SMCI", "PARA", "GTLB", "INTC", "WBD") if s in cikmap]
        rest = [s for s in syms if s not in seed]
        syms = (seed + rest)[:TEST_SYMBOLS]
        log.info("TEST MODE: scanning only %d symbols: %s", len(syms), syms[:10])

    for n, sym in enumerate(syms, 1):
        if n % 200 == 0:
            log.info("… %d/%d companies scanned (%d stake filings found)", n, len(syms), found)
        cik = cikmap[sym]
        r = sec_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
        scanned += 1
        if r is None:
            log.warning("%s: submissions fetch failed — skipped", sym)
            continue
        try:
            recent = r.json().get("filings", {}).get("recent", {})
        except Exception:
            continue
        forms = recent.get("form", []) or []
        dates = recent.get("filingDate", []) or []
        accs  = recent.get("accessionNumber", []) or []
        docs  = recent.get("primaryDocument", []) or []

        for i, form in enumerate(forms):
            if not is_stake_form(form):
                continue
            fdate = dates[i] if i < len(dates) else None
            if not fdate or fdate < cutoff:
                continue
            acc = accs[i] if i < len(accs) else None
            if not acc or acc in have:
                continue
            have.add(acc)
            found += 1
            if found <= 15 or TEST_SYMBOLS > 0:
                log.info("  %s  %s  %s", sym, form, fdate)

            is_activist = is_activist_form(form)
            pct, who = None, None
            # 13D = activist, rare and high-signal -> ALWAYS read the document.
            # 13G = passive index/pension holders, very high volume -> cap it.
            if is_activist or docs_opened < FETCH_DOC_LIMIT:
                accn = acc.replace("-", "")
                doc = docs[i] if i < len(docs) else None
                if doc:
                    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/{doc}"
                    dr = sec_get(url)
                    docs_opened += 1
                    if dr is not None:
                        pct = extract_pct(dr.text)
                        who = filer_name(dr.text)

            if is_activist:
                activist += 1
            rows.append({
                "accession_no": acc,
                "symbol": sym,
                "company_name": universe.get(sym, ""),
                "form_type": str(form).strip().upper().replace("SCHEDULE ", "SC "),
                "is_activist": bool(is_activist),
                "is_amendment": form.endswith("/A"),
                "filer_name": who,
                "pct_of_class": pct,
                "filing_date": fdate,
                "filing_url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-','')}/",
                "updated_at": dt.datetime.utcnow().isoformat() + "Z",
            })

        if len(rows) >= 250:
            sb_upsert("us_sec_stakes", rows, "accession_no"); rows = []

    sb_upsert("us_sec_stakes", rows, "accession_no")

    log.info("Done: %d companies scanned, %d stake filings (%d activist 13D), %d docs opened",
             scanned, found, activist, docs_opened)
    log.info("FETCH FAILURES — 403/429/503: %d | 404: %d | other: %d | exceptions: %d",
             FAILS["403"], FAILS["404"], FAILS["other"], FAILS["exception"])
    if FAILS["403"] > scanned * 0.1:
        log.error("More than 10%% of requests were rate-limited — results are INCOMPLETE. "
                  "Raise REQ_SLEEP and re-run.")
    telegram(f"🇺🇸 <b>US SEC Stakes — {'Backfill' if backfill else 'Daily'} complete</b>\n"
             f"Companies scanned: {scanned}\n"
             f"📋 Stake filings found: <b>{found}</b>\n"
             f"⚔️ Activist 13D: <b>{activist}</b> | 🏛️ Passive 13G: {found - activist}\n"
             f"Documents parsed for ownership %: {docs_opened}")


if __name__ == "__main__":
    run()
