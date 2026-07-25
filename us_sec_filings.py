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
from xml.etree import ElementTree as ET
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


# ---------------------------------------------------------------- XML parsing
# Modern 13D/13G filings (SEC structured-filing rules, ~late 2024) ship a clean
# primary_doc.xml. We parse that instead of scraping prose, which gives us:
#   issuerCIK           -> WHO THE FILING IS ABOUT (critical: lets us verify the
#                          filing targets OUR stock and is not our stock filing
#                          about someone else)
#   reportingPersonName -> the investor(s) doing the buying
#   percentOfClass      -> the stake size, as a real field
def _txt(el):
    return (el.text or "").strip() if el is not None else ""


def _tag(el):
    return re.sub(r"\{.*?\}", "", el.tag)


def parse_primary_xml(body):
    """Parse primary_doc.xml for BOTH schedule schemas.

    13D and 13G use different tag names for the same concepts, including a
    different capitalisation of the issuer CIK tag (issuerCIK vs issuerCik),
    so every lookup here is case-insensitive and accepts multiple candidates:

                        13D                     13G
      issuer cik        issuerCIK               issuerCik
      person block      reportingPersonInfo     coverPageHeaderReportingPersonDetails
      percent           percentOfClass          classPercent
      shares            aggregateAmountOwned    reportingPersonBeneficiallyOwned...
      event date        dateOfEvent             eventDateRequiresFilingThisStatement
    """
    try:
        root = ET.fromstring(body.encode("utf-8") if isinstance(body, str) else body)
    except Exception:
        return None

    def norm(el):
        return _tag(el).lower()

    def first_val(*names):
        want = {n.lower() for n in names}
        for e in root.iter():
            if norm(e) in want and _txt(e):
                return _txt(e)
        return None

    def to_float(v):
        if v is None:
            return None
        try:
            return float(str(v).replace(",", "").strip())
        except ValueError:
            return None

    # ── issuer (the company the filing is ABOUT) ──────────────────────
    issuer_cik = None
    raw_cik = first_val("issuerCIK", "issuerCik", "issuercik")
    if raw_cik:
        try:
            issuer_cik = int(str(raw_cik).strip())
        except ValueError:
            issuer_cik = None
    issuer_name = first_val("issuerName")

    # ── reporting persons (the investors) ─────────────────────────────
    PERSON_BLOCKS = {"reportingpersoninfo", "coverpageheaderreportingpersondetails"}
    PCT_TAGS   = {"percentofclass", "classpercent"}
    SHARE_TAGS = {"aggregateamountowned",
                  "reportingpersonbeneficiallyownedaggregatenumberofshares",
                  "amountbeneficiallyowned"}
    persons = []
    for blk in root.iter():
        if norm(blk) not in PERSON_BLOCKS:
            continue
        nm = pct = shares = ptype = None
        for ch in blk.iter():
            tg = norm(ch)
            if tg == "reportingpersonname" and nm is None:
                nm = _txt(ch)
            elif tg in PCT_TAGS and pct is None:
                pct = to_float(_txt(ch))
            elif tg in SHARE_TAGS and shares is None:
                shares = to_float(_txt(ch))
            elif tg == "typeofreportingperson" and ptype is None:
                ptype = _txt(ch)
        if nm:
            persons.append({"name": nm, "pct": pct, "shares": shares, "type": ptype})

    # headline stake = the largest disclosed percentage among the filers
    lead = None
    for p in persons:
        if p["pct"] is not None and (lead is None or p["pct"] > (lead["pct"] or 0)):
            lead = p
    if lead is None and persons:
        lead = persons[0]

    # some 13Gs only carry the percentage in the items section
    pct_final = (lead or {}).get("pct")
    if pct_final is None:
        pct_final = to_float(first_val("classPercent", "percentOfClass"))
    shares_final = (lead or {}).get("shares")
    if shares_final is None:
        shares_final = to_float(first_val("amountBeneficiallyOwned"))

    return {
        "issuer_cik": issuer_cik,
        "issuer_name": issuer_name,
        "filer_name": (lead or {}).get("name") or first_val("reportingPersonName"),
        "pct_of_class": pct_final,
        "shares_owned": shares_final,
        "filer_type": (lead or {}).get("type") or first_val("typeOfReportingPerson"),
        "all_filers": ", ".join([p["name"] for p in persons[:6]]) if persons else None,
        "event_date": first_val("dateOfEvent", "eventDateRequiresFilingThisStatement"),
        "amendment_no": first_val("amendmentNo"),
        "n_filers": len(persons),
    }


PCT_FALLBACK = re.compile(
    r"PERCENT\s+OF\s+CLASS[^0-9%]{0,140}?([0-9]{1,2}(?:\.[0-9]{1,2})?)\s*%", re.I | re.S)


def extract_pct_fallback(text):
    """Only used for pre-2024 style filings that have no primary_doc.xml."""
    if not text:
        return None
    body = re.sub(r"<[^>]+>", " ", text)
    body = re.sub(r"&nbsp;?", " ", body)
    body = re.sub(r"\s+", " ", body)[:60000]
    m = PCT_FALLBACK.search(body)
    if m:
        try:
            v = float(m.group(1))
            if 0 < v <= 100:
                return round(v, 2)
        except ValueError:
            pass
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
    reversed_out = xml_ok = legacy = 0
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

            is_activist = is_activist_form(form)
            accn = acc.replace("-", "")
            folder = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn}"

            # ── Read primary_doc.xml. This is REQUIRED, not optional: it carries
            # issuerCIK, which tells us who the filing is actually ABOUT. A
            # company's SEC record contains filings where it is the SUBJECT and
            # filings where it is the FILER (i.e. it invested in someone else).
            # Without this check we would store "BEN took a stake in itself"
            # when the truth is "BEN took a stake in Clarion".
            info = None
            xr = sec_get(f"{folder}/primary_doc.xml")
            docs_opened += 1
            if xr is not None:
                info = parse_primary_xml(xr.text)

            pct = who = issuer_nm = shares = ftype = allf = evdate = amno = None
            nfilers = None
            if info:
                # VALIDATION: does this filing target our stock?
                if info.get("issuer_cik") is not None and info["issuer_cik"] != cik:
                    reversed_out += 1
                    if reversed_out <= 12:
                        log.info("  SKIP %s %s %s — filing is about %s (CIK %s), not %s",
                                 sym, form, fdate, info.get("issuer_name"),
                                 info.get("issuer_cik"), sym)
                    continue
                pct      = info.get("pct_of_class")
                who      = info.get("filer_name")
                issuer_nm = info.get("issuer_name")
                shares   = info.get("shares_owned")
                ftype    = info.get("filer_type")
                allf     = info.get("all_filers")
                evdate   = info.get("event_date")
                amno     = info.get("amendment_no")
                nfilers  = info.get("n_filers")
                xml_ok += 1
            else:
                # Pre-2024 filings have no primary_doc.xml. We cannot verify the
                # subject, so keep the row but flag it as unverified.
                doc = docs[i] if i < len(docs) else None
                if doc:
                    dr = sec_get(f"{folder}/{doc}")
                    docs_opened += 1
                    if dr is not None:
                        pct = extract_pct_fallback(dr.text)
                legacy += 1

            found += 1
            if found <= 15 or TEST_SYMBOLS > 0:
                log.info("  %s  %s  %s  %s%s", sym, form, fdate,
                         (who or "?"), (f"  {pct}%" if pct is not None else ""))
            if is_activist:
                activist += 1

            # event_date arrives as MM/DD/YYYY in the XML -> normalise to ISO
            ev_iso = None
            if evdate:
                m_ev = re.match(r"(\d{2})/(\d{2})/(\d{4})", evdate.strip())
                if m_ev:
                    ev_iso = f"{m_ev.group(3)}-{m_ev.group(1)}-{m_ev.group(2)}"

            rows.append({
                "accession_no": acc,
                "symbol": sym,
                "company_name": universe.get(sym, ""),
                "form_type": str(form).strip().upper().replace("SCHEDULE ", "SC "),
                "is_activist": bool(is_activist),
                "is_amendment": str(form).strip().endswith("/A"),
                "filer_name": who,
                "all_filers": allf,
                "filer_type": ftype,
                "n_filers": nfilers,
                "pct_of_class": pct,
                "shares_owned": shares,
                "issuer_name": issuer_nm,
                "event_date": ev_iso,
                "amendment_no": amno,
                "xml_verified": bool(info),
                "filing_date": fdate,
                "filing_url": folder + "/",
                "updated_at": dt.datetime.utcnow().isoformat() + "Z",
            })

        if len(rows) >= 250:
            sb_upsert("us_sec_stakes", rows, "accession_no"); rows = []

    sb_upsert("us_sec_stakes", rows, "accession_no")

    log.info("Done: %d companies scanned, %d stake filings KEPT (%d activist 13D), %d docs opened",
             scanned, found, activist, docs_opened)
    log.info("VALIDATION — xml-verified: %d | legacy (no xml): %d | "
             "REJECTED as filings about OTHER companies: %d",
             xml_ok, legacy, reversed_out)
    log.info("FETCH FAILURES — 403/429/503: %d | 404: %d | other: %d | exceptions: %d",
             FAILS["403"], FAILS["404"], FAILS["other"], FAILS["exception"])
    if FAILS["403"] > scanned * 0.1:
        log.error("More than 10%% of requests were rate-limited — results are INCOMPLETE. "
                  "Raise REQ_SLEEP and re-run.")
    telegram(f"🇺🇸 <b>US SEC Stakes — {'Backfill' if backfill else 'Daily'} complete</b>\n"
             f"Companies scanned: {scanned}\n"
             f"📋 Stake filings on your stocks: <b>{found}</b>\n"
             f"⚔️ Activist 13D: <b>{activist}</b> | 🏛️ Passive 13G: {found - activist}\n"
             f"🚫 Rejected (about other companies): {reversed_out}")


if __name__ == "__main__":
    run()
