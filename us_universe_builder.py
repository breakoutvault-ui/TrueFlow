#!/usr/bin/env python3
"""
TrueFlow US - Universe Builder
Filters Nasdaq + NYSE to a QM-tradeable universe:
price > $10, market cap > $2B, avg dollar volume > $25M, common stocks only.
Writes to Supabase table: us_universe. Run weekly. No login needed.
"""
import io, re, sys, time
import requests
import pandas as pd
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor, as_completed
from supabase import create_client

CONFIG = {
    "supabase_url": "https://tsgltaqbxtisebqmbffg.supabase.co",
    "supabase_key": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRzZ2x0YXFieHRpc2VicW1iZmZnIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzU1NTEyMDEsImV4cCI6MjA5MTEyNzIwMX0.SQGq9E67S7j977RUA-oJqDGV8KgEhQZb0nHfAvHcFys",
    "min_price": 10.0,
    "min_market_cap": 2_000_000_000,
    "min_dollar_vol": 25_000_000,
    "batch_size": 150,
    "info_threads": 8,
}

LISTING_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqtraded.txt"
BAD_NAME_WORDS = re.compile(
    r"warrant|right(s)?\b|\bunit(s)?\b|preferred|preference|depositary|"
    r"%|\bnotes?\b|\bdue\b|acquisition corp|acquisition co", re.I)


def log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


def get_listings():
    log("Downloading Nasdaq/NYSE listing file...")
    r = requests.get(LISTING_URL, timeout=60)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text), sep="|")
    df = df[df["Nasdaq Traded"] == "Y"]
    df = df[df["ETF"] == "N"]
    df = df[df["Test Issue"] == "N"]
    df = df[df["Listing Exchange"].isin(["Q", "N"])]  # Q=Nasdaq, N=NYSE
    df = df[df["Symbol"].astype(str).str.fullmatch(r"[A-Z]{1,5}")]
    df = df[~df["Security Name"].astype(str).str.contains(BAD_NAME_WORDS)]
    df = df.rename(columns={"Symbol": "symbol", "Security Name": "name",
                            "Listing Exchange": "exchange"})
    df["exchange"] = df["exchange"].map({"Q": "NASDAQ", "N": "NYSE"})
    df = df[["symbol", "name", "exchange"]].drop_duplicates("symbol")
    log(f"Listings after name/type filters: {len(df)}")
    return df


def price_volume_screen(symbols):
    """Batch-download 2 months of daily candles; compute price, $vol, ADR%."""
    rows = {}
    batches = [symbols[i:i + CONFIG["batch_size"]]
               for i in range(0, len(symbols), CONFIG["batch_size"])]
    for bi, batch in enumerate(batches, 1):
        log(f"Price/volume batch {bi}/{len(batches)} ({len(batch)} symbols)...")
        try:
            data = yf.download(batch, period="2mo", interval="1d",
                               group_by="ticker", auto_adjust=False,
                               threads=True, progress=False)
        except Exception as e:
            log(f"  batch failed, skipping: {e}")
            continue
        for sym in batch:
            try:
                if len(batch) > 1:
                    d = data[sym].dropna(subset=["Close"])
                else:
                    d = data.dropna(subset=["Close"])
                if len(d) < 20:
                    continue
                d = d.tail(20)
                close = float(d["Close"].iloc[-1])
                dollar_vol = float((d["Close"] * d["Volume"]).mean())
                adr = float(((d["High"] / d["Low"]) - 1).mean() * 100)
                if close >= CONFIG["min_price"] and dollar_vol >= CONFIG["min_dollar_vol"]:
                    rows[sym] = {"price": round(close, 2),
                                 "avg_dollar_vol": round(dollar_vol),
                                 "adr_pct": round(adr, 2)}
            except Exception:
                continue
        time.sleep(1)
    log(f"Passed price + dollar-volume filters: {len(rows)}")
    return rows


def fetch_info(sym):
    try:
        info = yf.Ticker(sym).info
        return sym, {
            "quote_type": info.get("quoteType", ""),
            "market_cap": info.get("marketCap"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
        }
    except Exception:
        return sym, None


def enrich_and_filter(rows):
    log(f"Fetching market cap / sector for {len(rows)} survivors...")
    final = {}
    syms = list(rows.keys())
    done = 0
    with ThreadPoolExecutor(max_workers=CONFIG["info_threads"]) as ex:
        futs = {ex.submit(fetch_info, s): s for s in syms}
        for fut in as_completed(futs):
            sym, info = fut.result()
            done += 1
            if done % 100 == 0:
                log(f"  info progress: {done}/{len(syms)}")
            if not info:
                continue
            cap = info["market_cap"]
            if info["quote_type"] != "EQUITY":
                continue
            if not cap or cap < CONFIG["min_market_cap"]:
                continue
            final[sym] = {**rows[sym],
                          "market_cap": int(cap),
                          "sector": info["sector"],
                          "industry": info["industry"]}
    log(f"Passed market-cap + equity filters: {len(final)}")
    return final


def write_supabase(final, listings):
    sb = create_client(CONFIG["supabase_url"], CONFIG["supabase_key"])
    name_map = listings.set_index("symbol")[["name", "exchange"]].to_dict("index")
    payload = []
    for sym, d in final.items():
        meta = name_map.get(sym, {})
        payload.append({
            "symbol": sym,
            "name": meta.get("name"),
            "exchange": meta.get("exchange"),
            "sector": d.get("sector"),
            "industry": d.get("industry"),
            "price": d["price"],
            "market_cap": d["market_cap"],
            "avg_dollar_vol": d["avg_dollar_vol"],
            "adr_pct": d["adr_pct"],
        })
    log(f"Writing {len(payload)} rows to Supabase...")
    for i in range(0, len(payload), 500):
        sb.table("us_universe").upsert(payload[i:i + 500]).execute()
    # remove symbols that dropped out of the universe
    existing = sb.table("us_universe").select("symbol").execute()
    old = {r["symbol"] for r in existing.data}
    stale = list(old - set(final.keys()))
    for i in range(0, len(stale), 200):
        chunk = stale[i:i + 200]
        sb.table("us_universe").delete().in_("symbol", chunk).execute()
    if stale:
        log(f"Removed {len(stale)} stale symbols.")
    log("Supabase write complete.")


def main():
    t0 = time.time()
    listings = get_listings()
    rows = price_volume_screen(listings["symbol"].tolist())
    final = enrich_and_filter(rows)
    if len(final) < 300:
        log(f"WARNING: only {len(final)} stocks passed - not writing (safety guard).")
        sys.exit(1)
    write_supabase(final, listings)
    log(f"DONE. Universe size: {len(final)} stocks. "
        f"Total time: {round((time.time() - t0) / 60, 1)} min.")


if __name__ == "__main__":
    main()
