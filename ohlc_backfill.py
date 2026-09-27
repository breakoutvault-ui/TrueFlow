#!/usr/bin/env python3
# ==============================================================
#  TrueFlow - daily OHLC backfill
#
#  Pulls real open/high/low/close/volume from Kite into daily_ohlc.
#
#  WHY: momentum_stocks.day_high and day_low are empty on every one of
#  264,077 rows. Anything needing an intraday high or low - stop studies,
#  pivot-break entries, candle charts beyond 120 sessions - has no data.
#
#  Run the SQL first, then:
#    cd /root/trueflow && ./bin/python ohlc_backfill.py            # 3 years
#    cd /root/trueflow && ./bin/python ohlc_backfill.py --append   # nightly top-up
#
#  RESUMABLE. It records each symbol as it finishes, so if it dies or you
#  Ctrl-C it, running it again picks up where it stopped instead of
#  starting over. ~1,450 symbols takes roughly 15-25 minutes the first time.
# ==============================================================

import sys, os, json, time, argparse
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/root/trueflow")
import requests
import tf_config as CFG
from kiteconnect import KiteConnect

SB_URL = getattr(CFG, "SUPABASE_URL")
SB_KEY = getattr(CFG, "SUPABASE_KEY")
KITE_KEY = getattr(CFG, "KITE_API_KEY")
TOKEN_PATH = getattr(CFG, "ACCESS_TOKEN_PATH", "/root/trueflow/access_token.txt")

H = {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY,
     "Content-Type": "application/json"}

PROGRESS = "/root/trueflow/ohlc_backfill_done.json"
IST = timezone(timedelta(hours=5, minutes=30))

# Kite's historical endpoint allows 3 requests a second. 0.4s keeps a margin
# so a burst never trips the limit and costs a retry.
SLEEP = 0.40
CHUNK = 1000          # rows per Supabase write

INDICES = ["NIFTY 50", "NIFTY BANK", "NIFTY MIDCAP 100", "NIFTY SMLCAP 100",
           "NIFTY 500", "NIFTY NEXT 50", "INDIA VIX"]


def log(m):
    print("%s  %s" % (datetime.now(IST).strftime("%H:%M:%S"), m), flush=True)


def kite():
    k = KiteConnect(api_key=KITE_KEY)
    k.set_access_token(open(TOKEN_PATH).read().strip())
    return k


def symbols_wanted():
    """Every symbol the scan has ever seen. Paged - one page holds 1000."""
    seen, cur = [], None
    while True:
        q = "select=symbol&order=symbol.asc&limit=1000"
        if cur:
            q += "&symbol=gt.%s" % cur
        r = requests.get("%s/rest/v1/momentum_stocks?%s" % (SB_URL, q),
                         headers=H, timeout=60)
        r.raise_for_status()
        j = r.json()
        if not j:
            break
        page = sorted({x["symbol"] for x in j})
        new = [s for s in page if s > (cur or "")]
        if not new:
            break
        seen.extend(new)
        cur = new[-1]
    return sorted(set(seen))


def token_map(k, wanted):
    """tradingsymbol -> instrument_token, for NSE equities and the indices."""
    log("loading the NSE instrument list...")
    inst = k.instruments("NSE")
    eq, ix = {}, {}
    for r in inst:
        ts = r.get("tradingsymbol")
        if not ts:
            continue
        seg = (r.get("segment") or "")
        if seg == "NSE" and r.get("instrument_type") == "EQ":
            eq[ts] = r["instrument_token"]
        elif "INDICES" in seg:
            ix[ts] = r["instrument_token"]
    out = {s: eq[s] for s in wanted if s in eq}
    missing = [s for s in wanted if s not in eq]
    for name in INDICES:
        if name in ix:
            out[name] = ix[name]
    return out, missing, [n for n in INDICES if n in ix]


def load_done():
    if os.path.exists(PROGRESS):
        try:
            return set(json.load(open(PROGRESS)))
        except Exception:
            return set()
    return set()


def save_done(done):
    tmp = PROGRESS + ".tmp"
    json.dump(sorted(done), open(tmp, "w"))
    os.replace(tmp, PROGRESS)      # atomic, so a crash mid-write cannot corrupt it


def push(rows):
    """Upsert. The (symbol, d) primary key makes re-runs harmless."""
    for i in range(0, len(rows), CHUNK):
        part = rows[i:i + CHUNK]
        r = requests.post("%s/rest/v1/daily_ohlc" % SB_URL,
                          headers=dict(H, Prefer="resolution=merge-duplicates,return=minimal"),
                          json=part, timeout=120)
        if r.status_code >= 300:
            raise RuntimeError("write failed %s: %s" % (r.status_code, r.text[:300]))


def fetch(k, token, frm, to):
    for attempt in (1, 2, 3):
        try:
            return k.historical_data(token, frm, to, "day")
        except Exception as e:
            msg = str(e)
            if "many requests" in msg.lower() or "429" in msg:
                time.sleep(2 * attempt)      # backed off, then retried
                continue
            if attempt == 3:
                raise
            time.sleep(1)
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=3.0)
    ap.add_argument("--append", action="store_true",
                    help="only the last 10 days - use this nightly")
    ap.add_argument("--restart", action="store_true",
                    help="ignore previous progress and do every symbol again")
    a = ap.parse_args()

    k = kite()
    try:
        k.profile()
    except Exception as e:
        log("Kite is not logged in: %s" % e)
        log("Do today's Kite login first, then run this again.")
        sys.exit(1)

    wanted = symbols_wanted()
    log("%d symbols in momentum_stocks" % len(wanted))
    tmap, missing, idx_found = token_map(k, wanted)
    log("%d matched on NSE, %d unmatched, %d indices added"
        % (len(tmap) - len(idx_found), len(missing), len(idx_found)))
    if missing:
        log("unmatched (delisted or renamed, skipped): %s%s"
            % (", ".join(missing[:12]), " ..." if len(missing) > 12 else ""))

    done = set() if (a.restart or a.append) else load_done()
    todo = [s for s in tmap if s not in done]
    if not todo:
        log("nothing left to do - delete %s to force a full re-run" % PROGRESS)
        return

    to = datetime.now(IST)
    frm = to - timedelta(days=10 if a.append else int(a.years * 365))
    log("pulling %s to %s for %d symbols\n"
        % (frm.date(), to.date(), len(todo)))

    rows_total, failed, t0 = 0, [], time.time()
    for n, sym in enumerate(sorted(todo), 1):
        try:
            candles = fetch(k, tmap[sym], frm, to)
        except Exception as e:
            failed.append((sym, str(e)[:80]))
            time.sleep(SLEEP)
            continue

        batch = []
        for c in candles:
            if c.get("close") is None:
                continue
            batch.append({"symbol": sym, "d": c["date"].strftime("%Y-%m-%d"),
                          "o": c.get("open"), "h": c.get("high"),
                          "l": c.get("low"), "c": c.get("close"),
                          "v": c.get("volume")})
        if batch:
            try:
                push(batch)
                rows_total += len(batch)
            except Exception as e:
                failed.append((sym, str(e)[:80]))
                time.sleep(SLEEP)
                continue

        done.add(sym)
        if n % 25 == 0:
            save_done(done)
            rate = n / max(1e-6, time.time() - t0)
            left = (len(todo) - n) / max(1e-6, rate) / 60
            log("  %d/%d symbols, %d rows, ~%.0f min left"
                % (n, len(todo), rows_total, left))
        time.sleep(SLEEP)

    save_done(done)
    log("\ndone: %d symbols, %d rows in %.1f min"
        % (len(done), rows_total, (time.time() - t0) / 60))
    if failed:
        log("%d symbols failed:" % len(failed))
        for s, e in failed[:15]:
            log("   %-14s %s" % (s, e))
        log("Run the same command again - it retries only what is missing.")


if __name__ == "__main__":
    main()
