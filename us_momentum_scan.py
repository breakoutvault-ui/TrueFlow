"""
TrueFlow US Momentum Scanner v1.0
Faithful port of momentum_scan.py (Indian) — identical calculations,
data layer swapped from Kite Connect to yfinance (no login required).
Universe: us_universe table (~1,663 US stocks, price>$10, cap>$2B, $vol>$25M)
Calculates: Daily+Weekly EMAs, Categories A/B/C/AC, Crossovers, Breakouts,
            Sharp Movers, Momentum Score, QM patterns (EP/VCP/HTF), NR4/NR7
Stores: us_momentum_stocks, us_crossover_events tables
Auto-backfills from BACKFILL_FROM on first run.

Cron: 0 22 * * 1-5 (after US close, ~3:30 AM IST)
"""

import time
import logging
import requests
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
import yfinance as yf
from supabase import create_client

# ─── CONFIG ───────────────────────────────────────────
SUPABASE_URL      = "https://tsgltaqbxtisebqmbffg.supabase.co"
SUPABASE_KEY      = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRzZ2x0YXFieHRpc2VicW1iZmZnIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzU1NTEyMDEsImV4cCI6MjA5MTEyNzIwMX0.SQGq9E67S7j977RUA-oJqDGV8KgEhQZb0nHfAvHcFys"
TELEGRAM_TOKEN    = "8768295108:AAEnPXWasPeZJRhhmJPjee-DTmXCauMbjYA"
TELEGRAM_CHAT_ID  = "1202026803"
US_TZ             = ZoneInfo("America/New_York")
BACKFILL_FROM     = date(2026, 5, 1)   # keep short to protect Supabase free-tier storage
FETCH_DAYS        = 400                # calendar days of history (matches Indian daily mode)
BATCH_SIZE        = 100                # symbols per yfinance batch download
BATCH_SLEEP       = 1.0                # seconds between batches

# ─── LOGGING ──────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("/root/trueflow/us_momentum.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("TrueFlow-US-Momentum")

# ─── HELPERS (identical to Indian scanner) ────────────
def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        log.error(f"Telegram error: {e}")

def calc_ema(prices, period):
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return round(ema, 2)

def calc_weekly_candles(daily_hist):
    """Convert daily OHLCV to weekly candles (Monday-Friday)."""
    from collections import defaultdict
    weeks = defaultdict(list)
    for candle in daily_hist:
        d = candle['date'].date() if hasattr(candle['date'], 'date') else candle['date']
        week_key = d.isocalendar()[:2]
        weeks[week_key].append(candle)
    weekly = []
    for week_key in sorted(weeks.keys()):
        candles = weeks[week_key]
        weekly.append({
            'date': candles[-1]['date'],
            'open': candles[0]['open'],
            'high': max(c['high'] for c in candles),
            'low': min(c['low'] for c in candles),
            'close': candles[-1]['close'],
            'volume': sum(c['volume'] for c in candles)
        })
    return weekly

def classify_volume(vol_ratio):
    if vol_ratio >= 3.0:
        return 'Very High'
    elif vol_ratio >= 1.5:
        return 'High'
    else:
        return 'Low'

def last5_summary(closes):
    if len(closes) < 6:
        return '—'
    arrows = []
    for i in range(-5, 0):
        arrows.append('↑' if closes[i] >= closes[i-1] else '↓')
    return ''.join(arrows)

def last5_vol_ratios(volumes):
    if len(volumes) < 25:
        return '-'
    out = []
    for i in range(-5, 0):
        avg20 = sum(volumes[i-20:i]) / 20
        out.append(round(volumes[i] / avg20, 2) if avg20 > 0 else 0)
    return ','.join(str(x) for x in out)

def last20_vol_ratios(volumes):
    if len(volumes) < 40:
        return '-'
    out = []
    for i in range(-20, 0):
        avg20 = sum(volumes[i-20:i]) / 20
        out.append(round(volumes[i] / avg20, 2) if avg20 > 0 else 0)
    return ','.join(str(x) for x in out)

def last5_closes_str(closes):
    if len(closes) < 2:
        return '-'
    return ','.join(str(round(c, 2)) for c in closes[-5:])

def wk_closes_str(weekly_closes):
    if len(weekly_closes) < 2:
        return '-'
    return ','.join(str(round(c, 2)) for c in weekly_closes[-10:])

def wk_vol_ratios_str(weekly_vols):
    if len(weekly_vols) < 16:
        return '-'
    out = []
    for i in range(-8, 0):
        avg = sum(weekly_vols[i-8:i]) / 8
        out.append(round(weekly_vols[i] / avg, 2) if avg > 0 else 0)
    return ','.join(str(x) for x in out)

def calc_momentum_score(category, days_in_uptrend, vol_class, has_breakout, is_trending_sector):
    score = 0
    if 'A' in (category or '') and 'C' in (category or ''):
        score += 40
    elif category == 'A':
        score += 30
    elif category == 'C':
        score += 20
    elif category == 'B':
        score += 10
    score += min(days_in_uptrend // 5, 20)
    if vol_class == 'Very High':
        score += 20
    elif vol_class == 'High':
        score += 10
    if has_breakout:
        score += 20
    if is_trending_sector:
        score += 10
    return min(score, 100)

def get_breakout_type(ltp, h52w, h3y, h5y, hath):
    if hath and ltp >= hath * 0.99:
        return 'ATH'
    elif h5y and ltp >= h5y * 0.99:
        return '5Y'
    elif h3y and ltp >= h3y * 0.99:
        return '3Y'
    elif h52w and ltp >= h52w * 0.99:
        return '52W'
    return None

# ─── LOAD UNIVERSE ────────────────────────────────────
def load_universe(sb):
    """Load US universe from Supabase in batches."""
    try:
        all_stocks = []
        offset = 0
        while True:
            resp = sb.table("us_universe").select("symbol,name,exchange,sector,industry").range(offset, offset+999).execute()
            if not resp.data:
                break
            all_stocks.extend(resp.data)
            if len(resp.data) < 1000:
                break
            offset += 1000
        log.info(f"US universe loaded: {len(all_stocks)} stocks")
        return {s['symbol']: s for s in all_stocks}
    except Exception as e:
        log.error(f"Universe load error: {e}")
        return {}

# ─── BACKFILL CHECK ───────────────────────────────────
def needs_backfill(sb):
    """Backfill needed if very few distinct symbols processed yet."""
    try:
        processed = set()
        offset = 0
        while True:
            resp = sb.table("us_momentum_stocks").select("symbol").range(offset, offset+999).execute()
            if not resp.data:
                break
            for r in resp.data:
                processed.add(r['symbol'])
            if len(resp.data) < 1000:
                break
            offset += 1000
        distinct = len(processed)
        universe_resp = sb.table("us_universe").select("symbol", count="exact").limit(1).execute()
        universe_count = universe_resp.count or 1663
        if distinct < universe_count * 0.85:
            log.info(f"Only {distinct}/{universe_count} symbols processed — backfill required")
            return True
        log.info(f"{distinct}/{universe_count} symbols processed — no backfill needed")
        return False
    except Exception as e:
        log.error(f"Backfill check error: {e}")
        return False

# ─── DATA FETCH (yfinance replaces Kite) ──────────────
def fetch_history_batch(symbols, start_date):
    """Download daily candles for a batch of symbols; return {symbol: hist_list}.
    hist_list = list of {'date','open','high','low','close','volume'} dicts,
    same shape Kite returned, so process_stock stays identical."""
    out = {}
    try:
        data = yf.download(symbols, start=str(start_date), interval="1d",
                           group_by="ticker", auto_adjust=False,
                           threads=True, progress=False)
    except Exception as e:
        log.error(f"Batch download failed: {e}")
        return out
    for sym in symbols:
        try:
            df = data[sym] if len(symbols) > 1 else data
            df = df.dropna(subset=["Close"])
            if df.empty:
                continue
            hist = []
            for idx, row in df.iterrows():
                d = idx.date() if hasattr(idx, 'date') else idx
                vol = row["Volume"]
                hist.append({
                    'date': d,
                    'open': float(row["Open"]),
                    'high': float(row["High"]),
                    'low': float(row["Low"]),
                    'close': float(row["Close"]),
                    'volume': int(vol) if vol == vol else 0,  # NaN-safe
                })
            if hist:
                out[sym] = hist
        except Exception:
            continue
    return out

# ─── QM PATTERN DETECTION (identical) ─────────────────
def detect_ep(hist, closes, volumes):
    if len(closes) < 5 or len(volumes) < 21:
        return None
    vol_avg20 = sum(volumes[-21:-1]) / 20 if sum(volumes[-21:-1]) > 0 else 1
    for lookback in range(0, min(3, len(closes) - 1)):
        idx = -(1 + lookback)
        prev_idx = idx - 1
        if abs(prev_idx) > len(closes):
            continue
        chg = (closes[idx] - closes[prev_idx]) / closes[prev_idx] * 100 if closes[prev_idx] > 0 else 0
        vol_r = volumes[idx] / vol_avg20 if vol_avg20 > 0 else 0
        if chg >= 5.0 and vol_r >= 2.0:
            ep_high = hist[idx]["high"] if abs(idx) <= len(hist) else closes[idx]
            return {"pattern": "EP", "gap_pct": round(chg, 1), "vol_ratio": round(vol_r, 1), "days_ago": lookback, "pivot_level": round(ep_high, 2)}
    return None

def detect_vcp(hist, closes, volumes):
    if len(hist) < 25 or len(closes) < 25:
        return None
    low_20d = min(closes[-25:-5])
    high_20d = max(closes[-20:])
    move_pct = (high_20d - low_20d) / low_20d * 100 if low_20d > 0 else 0
    if move_pct < 20:
        return None
    recent_high = max(h["high"] for h in hist[-5:])
    recent_low = min(h["low"] for h in hist[-5:])
    current_range_pct = (recent_high - recent_low) / closes[-1] * 100 if closes[-1] > 0 else 999
    if current_range_pct > 8:
        return None
    peak = max(h["high"] for h in hist[-20:])
    depth = (peak - closes[-1]) / peak * 100 if peak > 0 else 999
    if depth > 15:
        return None
    vol_recent = sum(volumes[-5:]) / 5 if len(volumes) >= 5 else 0
    vol_prior = sum(volumes[-15:-5]) / 10 if len(volumes) >= 15 else 1
    vol_dryup = vol_recent < vol_prior * 0.7
    vol_dryup_partial = vol_recent < vol_prior * 0.9
    initial_range = high_20d - low_20d
    contraction = (1 - (recent_high - recent_low) / initial_range) * 100 if initial_range > 0 else 0
    base_days = 0
    for i in range(len(closes)-1, max(0, len(closes)-20), -1):
        if abs(closes[i] - closes[-1]) / closes[-1] * 100 < 5:
            base_days += 1
        else:
            break
    pivot = recent_high
    return {"pattern": "VCP", "move_pct": round(move_pct, 1), "base_depth": round(depth, 1), "contraction": round(contraction, 1), "vol_dryup": "Yes" if vol_dryup else ("Partial" if vol_dryup_partial else "No"), "pivot_level": round(pivot, 2), "base_days": base_days}

def detect_htf(hist, closes, volumes):
    if len(hist) < 40 or len(closes) < 40:
        return None
    low_40d = min(closes[-40:])
    low_idx = len(closes) - 40 + closes[-40:].index(low_40d)
    if low_idx >= len(closes) - 1:
        return None
    high_after_low = max(closes[low_idx:])
    move_pct = (high_after_low - low_40d) / low_40d * 100 if low_40d > 0 else 0
    if move_pct < 80:
        return None
    depth = (high_after_low - closes[-1]) / high_after_low * 100 if high_after_low > 0 else 999
    if depth > 25:
        return None
    recent_high = max(h["high"] for h in hist[-5:])
    recent_low = min(h["low"] for h in hist[-5:])
    range_pct = (recent_high - recent_low) / closes[-1] * 100 if closes[-1] > 0 else 999
    if range_pct > 10:
        return None
    vol_recent = sum(volumes[-5:]) / 5 if len(volumes) >= 5 else 0
    vol_prior = sum(volumes[-15:-5]) / 10 if len(volumes) >= 15 else 1
    vol_dryup = vol_recent < vol_prior * 0.7
    vol_dryup_partial = vol_recent < vol_prior * 0.9
    initial_range = high_after_low - low_40d
    contraction = (1 - (recent_high - recent_low) / initial_range) * 100 if initial_range > 0 else 0
    pivot = recent_high
    base_days = 0
    for i in range(len(closes)-1, max(0, len(closes)-30), -1):
        if abs(closes[i] - closes[-1]) / closes[-1] * 100 < 8:
            base_days += 1
        else:
            break
    return {"pattern": "HTF", "move_pct": round(move_pct, 1), "base_depth": round(depth, 1), "contraction": round(contraction, 1), "vol_dryup": "Yes" if vol_dryup else ("Partial" if vol_dryup_partial else "No"), "pivot_level": round(pivot, 2), "base_days": base_days}

def detect_ema_reclaim(closes, ema9d, prev_snapshot):
    if not prev_snapshot or ema9d is None:
        return False
    was_below = not prev_snapshot.get("above_ema9_daily", True)
    now_above = closes[-1] > ema9d
    return was_below and now_above

def detect_qm_patterns(hist, closes, volumes, ema9d, prev_snapshot):
    result = {"qm_pattern": None, "qm_base_depth": None, "qm_contraction": None, "qm_vol_dryup": None, "qm_pivot_level": None, "qm_ema_reclaim": False, "qm_move_pct": None, "qm_base_days": None}
    result["qm_ema_reclaim"] = detect_ema_reclaim(closes, ema9d, prev_snapshot)
    htf = detect_htf(hist, closes, volumes)
    if htf:
        result.update({"qm_pattern": "HTF", "qm_base_depth": htf["base_depth"], "qm_contraction": htf["contraction"], "qm_vol_dryup": htf["vol_dryup"], "qm_pivot_level": htf["pivot_level"], "qm_move_pct": htf["move_pct"], "qm_base_days": htf["base_days"]})
        return result
    vcp = detect_vcp(hist, closes, volumes)
    if vcp:
        result.update({"qm_pattern": "VCP", "qm_base_depth": vcp["base_depth"], "qm_contraction": vcp["contraction"], "qm_vol_dryup": vcp["vol_dryup"], "qm_pivot_level": vcp["pivot_level"], "qm_move_pct": vcp["move_pct"], "qm_base_days": vcp["base_days"]})
        return result
    ep = detect_ep(hist, closes, volumes)
    if ep:
        result.update({"qm_pattern": "EP", "qm_move_pct": ep["gap_pct"], "qm_pivot_level": ep["pivot_level"]})
        return result
    return result

# ─── PROCESS ONE STOCK (identical logic) ──────────────
def process_stock(symbol, hist, today, universe, prev_snapshot, crossover_log):
    """Calculate all metrics for one stock for one date."""
    info = universe.get(symbol, {})

    if len(hist) < 22:
        return None, None

    closes = [h['close'] for h in hist]
    volumes = [h['volume'] for h in hist]

    ltp = closes[-1]
    prev_close = closes[-2] if len(closes) >= 2 else ltp

    # NR4 / NR7 — Narrow Range detection + outcome tracking
    today_range = hist[-1]['high'] - hist[-1]['low']
    ranges_7 = [h['high'] - h['low'] for h in hist[-7:]] if len(hist) >= 7 else None
    ranges_4 = [h['high'] - h['low'] for h in hist[-4:]] if len(hist) >= 4 else None
    is_nr7 = bool(ranges_7 and today_range <= min(ranges_7))
    is_nr4 = bool(ranges_4 and today_range <= min(ranges_4))
    nr_range_pct = round(today_range / ltp * 100, 2) if ltp else None

    p_nr_status = prev_snapshot.get('nr_status') if prev_snapshot else None
    p_nr_high = prev_snapshot.get('nr_day_high') if prev_snapshot else None
    p_nr_low = prev_snapshot.get('nr_day_low') if prev_snapshot else None
    p_nr_src = prev_snapshot.get('nr_source_date') if prev_snapshot else None

    if is_nr7 or is_nr4:
        nr_status = 'coiling'
        nr_day_high = round(hist[-1]['high'], 2)
        nr_day_low = round(hist[-1]['low'], 2)
        nr_source_date = str(today)
    elif p_nr_status == 'coiling' and p_nr_high is not None and p_nr_low is not None:
        nr_day_high = p_nr_high
        nr_day_low = p_nr_low
        nr_source_date = p_nr_src
        if ltp > p_nr_high:
            nr_status = 'expanded_up'
        elif ltp < p_nr_low:
            nr_status = 'expanded_down'
        else:
            nr_status = 'coiling'
    elif p_nr_status in ('expanded_up', 'expanded_down'):
        nr_status = None
        nr_day_high = None
        nr_day_low = None
        nr_source_date = None
    else:
        nr_status = None
        nr_day_high = None
        nr_day_low = None
        nr_source_date = None

    # Daily EMAs
    ema9d  = calc_ema(closes, 9)
    ema20d = calc_ema(closes, 20)
    if not ema9d or not ema20d:
        return None, None

    above9d  = ltp > ema9d
    above20d = ltp > ema20d
    pct9d    = round((ltp - ema9d) / ema9d * 100, 2)
    ema5d   = calc_ema(closes, 5)
    above5d = (ltp > ema5d) if ema5d else None
    pct5d   = round((ltp - ema5d) / ema5d * 100, 2) if ema5d else None

    # Weekly candles + EMAs
    weekly = calc_weekly_candles(hist)
    weekly_closes = [w['close'] for w in weekly]
    ema9w  = calc_ema(weekly_closes, 9)  if len(weekly_closes) >= 9  else None
    ema20w = calc_ema(weekly_closes, 20) if len(weekly_closes) >= 20 else None
    above9w  = (ltp > ema9w)  if ema9w  else None
    above20w = (ltp > ema20w) if ema20w else None
    pct9w    = round((ltp - ema9w) / ema9w * 100, 2) if ema9w else None

    # Category
    cat = None
    if above9d and above20d and above9w and above20w:
        cat = 'A'
    elif not above9d and above9w and above20w:
        cat = 'B'

    # Weekly crossover (C category)
    is_fresh_weekly_cross = False
    weekly_cross_date = None
    if ema9w and ema20w and len(weekly_closes) >= 21:
        for i in range(max(1, len(weekly_closes)-4), len(weekly_closes)):
            prev_9w = calc_ema(weekly_closes[:i], 9)
            prev_20w = calc_ema(weekly_closes[:i], 20)
            curr_9w = calc_ema(weekly_closes[:i+1], 9) if i+1 <= len(weekly_closes) else None
            curr_20w = calc_ema(weekly_closes[:i+1], 20) if i+1 <= len(weekly_closes) else None
            if prev_9w and prev_20w and curr_9w and curr_20w:
                if prev_9w <= prev_20w and curr_9w > curr_20w:
                    is_fresh_weekly_cross = True
                    weekly_cross_date = weekly[i]['date'].date() if hasattr(weekly[i]['date'], 'date') else weekly[i]['date']
                    break

    # Historical weekly crossover
    hist_weekly_cross_date = None
    if ema9w and ema20w and len(weekly_closes) >= 21:
        check_start = max(1, len(weekly_closes) - 52)
        for i in range(check_start, len(weekly_closes)):
            prev_9w = calc_ema(weekly_closes[:i], 9)
            prev_20w = calc_ema(weekly_closes[:i], 20)
            curr_9w = calc_ema(weekly_closes[:i+1], 9) if i+1 <= len(weekly_closes) else None
            curr_20w = calc_ema(weekly_closes[:i+1], 20) if i+1 <= len(weekly_closes) else None
            if prev_9w and prev_20w and curr_9w and curr_20w:
                if prev_9w <= prev_20w and curr_9w > curr_20w:
                    hist_weekly_cross_date = weekly[i]['date'].date() if hasattr(weekly[i]['date'], 'date') else weekly[i]['date']

    if is_fresh_weekly_cross:
        if cat == 'A':
            cat = 'AC'
        elif cat is None:
            cat = 'C'

    # Daily crossover detection
    daily_cross_date = None
    is_fresh_daily_cross = False
    new_crossover_event = None

    if len(closes) >= 21:
        prev_9d_yesterday = calc_ema(closes[:-1], 9)
        prev_20d_yesterday = calc_ema(closes[:-1], 20)
        if prev_9d_yesterday and prev_20d_yesterday:
            if prev_9d_yesterday <= prev_20d_yesterday and ema9d > ema20d:
                is_fresh_daily_cross = True
                daily_cross_date = today
                vol_ratio = volumes[-1] / (sum(volumes[-21:-1])/20) if sum(volumes[-21:-1]) > 0 else 0
                new_crossover_event = {
                    'symbol': symbol,
                    'crossover_date': str(today),
                    'crossover_type': 'daily',
                    'direction': 'golden',
                    'ema9_value': ema9d,
                    'ema20_value': ema20d,
                    'ltp_on_date': ltp,
                    'vol_ratio': round(vol_ratio, 2),
                    'vol_class': classify_volume(vol_ratio),
                }

    # Historical daily crossover date
    if not daily_cross_date and len(closes) >= 21:
        for i in range(len(closes)-1, 20, -1):
            e9 = calc_ema(closes[:i], 9)
            e20 = calc_ema(closes[:i], 20)
            e9_prev = calc_ema(closes[:i-1], 9)
            e20_prev = calc_ema(closes[:i-1], 20)
            if e9 and e20 and e9_prev and e20_prev:
                if e9_prev <= e20_prev and e9 > e20:
                    d = hist[i-1]['date']
                    daily_cross_date = d.date() if hasattr(d, 'date') else d
                    today_dt = today if isinstance(today, date) else today
                    week_start = today_dt - timedelta(days=today_dt.weekday())
                    if daily_cross_date >= week_start:
                        is_fresh_daily_cross = True
                    break

    # Weekly crossover event logging
    if is_fresh_weekly_cross and weekly_cross_date:
        vol_ratio_w = volumes[-1] / (sum(volumes[-21:-1])/20) if sum(volumes[-21:-1]) > 0 else 0
        weekly_cross_event = {
            'symbol': symbol,
            'crossover_date': str(weekly_cross_date),
            'crossover_type': 'weekly',
            'direction': 'golden',
            'ema9_value': ema9w,
            'ema20_value': ema20w,
            'ltp_on_date': ltp,
            'vol_ratio': round(vol_ratio_w, 2),
            'vol_class': classify_volume(vol_ratio_w),
        }
        crossover_log.append(weekly_cross_event)

    # Forming weekly crossover
    is_forming_weekly = False
    today_dt = today if isinstance(today, date) else today
    if today_dt.weekday() < 4:
        if ema9w and ema20w:
            gap = ema9w - ema20w
            if -2 < gap < 0:
                is_forming_weekly = True

    # Re-entry flag
    is_reentry = False
    if prev_snapshot:
        was_below = not prev_snapshot.get('above_ema9_daily', True)
        if was_below and above9d:
            is_reentry = True

    # Volume metrics
    vol_today = volumes[-1]
    vol_avg20 = int(sum(volumes[-21:-1]) / 20) if len(volumes) >= 21 else 0
    vol_ratio = round(vol_today / vol_avg20, 2) if vol_avg20 > 0 else 0
    vol_class = classify_volume(vol_ratio)

    # High levels
    high_52w = round(max(h['high'] for h in hist[-252:]), 2) if len(hist) >= 252 else round(max(h['high'] for h in hist), 2)
    low_52w  = round(min(h['low'] for h in hist[-252:]), 2) if len(hist) >= 252 else round(min(h['low'] for h in hist), 2)
    high_3y  = round(max(h['high'] for h in hist[-756:]), 2) if len(hist) >= 756 else None
    high_5y  = round(max(h['high'] for h in hist[-1260:]), 2) if len(hist) >= 1260 else None
    high_ath = round(max(h['high'] for h in hist), 2)

    # Breakout detection
    breakout_type  = get_breakout_type(ltp, high_52w, high_3y, high_5y, high_ath)
    breakout_date  = str(today) if breakout_type else None
    breakout_price = ltp if breakout_type else None
    breakout_hold  = True if breakout_type else None

    # Breakout volume + consolidation days
    bo_vol = None
    bo_vol_when = None
    if breakout_type and len(volumes) >= 22:
        def _rv(idx):
            a = sum(volumes[idx-20:idx]) / 20
            return round(volumes[idx] / a, 2) if a > 0 else 0
        bo_day_rvol = vol_ratio
        prior = [_rv(i) for i in (-2, -3, -4) if len(volumes) + i - 20 >= 0]
        prior_max = max(prior) if prior else 0
        if bo_day_rvol >= 1.5:
            bo_vol, bo_vol_when = bo_day_rvol, 'day'
        elif prior_max >= 1.5:
            bo_vol, bo_vol_when = prior_max, 'within3'
        else:
            bo_vol, bo_vol_when = round(max(bo_day_rvol, prior_max), 2), 'thin'

    if breakout_type:
        _consol_state = True
    elif high_52w and high_52w > 0 and 0 < (high_52w - ltp) / high_52w * 100 <= 15:
        _consol_state = True
    else:
        _consol_state = False
    if _consol_state:
        consol_days = ((prev_snapshot.get('consol_days') or 0) + 1) if prev_snapshot else 1
    else:
        consol_days = None

    # Sharp movers
    move_3d = round((closes[-1] - closes[-4]) / closes[-4] * 100, 2) if len(closes) >= 4 else None
    move_1m = round((closes[-1] - closes[-21]) / closes[-21] * 100, 2) if len(closes) >= 21 else None
    is_sharp = (move_3d and move_3d >= 20) or (move_1m and move_1m >= 30)

    # Consecutive green days
    consec_green = 0
    for i in range(len(closes)-1, 0, -1):
        if closes[i] >= closes[i-1]:
            consec_green += 1
        else:
            break

    # Days in uptrend
    days_uptrend = 0
    if prev_snapshot:
        days_uptrend = prev_snapshot.get('days_in_uptrend', 0)
        if above9d:
            days_uptrend += 1
        else:
            days_uptrend = 0
    elif above9d:
        days_uptrend = 1

    # Days below 9 EMA
    days_below = 0
    if not above9d:
        if prev_snapshot:
            days_below = prev_snapshot.get('days_below_ema9', 0) + 1
        else:
            days_below = 1

    is_active = days_below < 2

    mom_score = calc_momentum_score(cat, days_uptrend, vol_class, bool(breakout_type), False)

    qm = detect_qm_patterns(hist, closes, volumes, ema9d, prev_snapshot)

    day_chg = round((ltp - prev_close) / prev_close * 100, 2)
    week_chg = round((closes[-1] - closes[-6]) / closes[-6] * 100, 2) if len(closes) >= 6 else None
    month_chg = round((closes[-1] - closes[-22]) / closes[-22] * 100, 2) if len(closes) >= 22 else None

    l5 = last5_summary(closes)

    _adr_window = hist[-20:]
    _adr_ratios = [(_b['high'] / _b['low']) for _b in _adr_window if _b.get('low')]
    adr_pct = round((sum(_adr_ratios) / len(_adr_ratios) - 1) * 100, 2) if _adr_ratios else None

    record = {
        'symbol': symbol,
        'session_date': str(today),
        'is_nr4': is_nr4,
        'is_nr7': is_nr7,
        'nr_day_high': nr_day_high,
        'nr_day_low': nr_day_low,
        'nr_range_pct': nr_range_pct,
        'nr_status': nr_status,
        'nr_source_date': nr_source_date,
        'sector': info.get('sector', ''),
        'industry': info.get('industry', ''),
        'ltp': round(ltp, 2),
        'day_chg_pct': day_chg,
        'week_chg_pct': week_chg,
        'month_chg_pct': month_chg,
        'ema9_daily': ema9d,
        'ema20_daily': ema20d,
        'above_ema9_daily': above9d,
        'above_ema20_daily': above20d,
        'pct_from_ema9_daily': pct9d,
        'ema5_daily': ema5d,
        'above_ema5_daily': above5d,
        'pct_from_ema5_daily': pct5d,
        'ema9_weekly': ema9w,
        'ema20_weekly': ema20w,
        'above_ema9_weekly': above9w,
        'above_ema20_weekly': above20w,
        'pct_from_ema9_weekly': pct9w,
        'category': cat,
        'daily_crossover_date': str(daily_cross_date) if daily_cross_date else None,
        'weekly_crossover_date': str(weekly_cross_date or hist_weekly_cross_date) if (weekly_cross_date or hist_weekly_cross_date) else None,
        'is_fresh_daily_cross': is_fresh_daily_cross,
        'is_fresh_weekly_cross': is_fresh_weekly_cross,
        'is_forming_weekly': is_forming_weekly,
        'is_reentry': is_reentry,
        'vol_today': int(vol_today),
        'vol_avg20': int(vol_avg20),
        'vol_ratio': vol_ratio,
        'qm_pattern': qm['qm_pattern'],
        'qm_base_depth': qm['qm_base_depth'],
        'qm_contraction': qm['qm_contraction'],
        'qm_vol_dryup': qm['qm_vol_dryup'],
        'qm_pivot_level': qm['qm_pivot_level'],
        'qm_ema_reclaim': qm['qm_ema_reclaim'],
        'qm_move_pct': qm['qm_move_pct'],
        'qm_base_days': qm['qm_base_days'],
        'vol_class': vol_class,
        'high_52w': high_52w,
        'low_52w': low_52w,
        'high_3y': high_3y,
        'high_5y': high_5y,
        'high_ath': high_ath,
        'breakout_type': breakout_type,
        'breakout_date': breakout_date,
        'breakout_price': breakout_price,
        'breakout_holding': breakout_hold,
        'move_3d_pct': move_3d,
        'move_1m_pct': move_1m,
        'is_sharp_mover': bool(is_sharp),
        'consecutive_green': consec_green,
        'days_in_uptrend': days_uptrend,
        'momentum_score': mom_score,
        'last5_summary': l5,
        'vol5_ratios': last5_vol_ratios(volumes),
        'vol20_ratios': last20_vol_ratios(volumes),
        'last5_closes': last5_closes_str(closes),
        'wk_closes': wk_closes_str(weekly_closes),
        'wk_vol_ratios': wk_vol_ratios_str([w['volume'] for w in weekly]),
        'bo_vol': bo_vol,
        'bo_vol_when': bo_vol_when,
        'consol_days': consol_days,
        'adr_pct': adr_pct,
        'is_active': is_active,
        'days_below_ema9': days_below,
        'exit_date': str(today) if days_below >= 2 and (not prev_snapshot or prev_snapshot.get('days_below_ema9', 0) < 2) else (prev_snapshot.get('exit_date') if prev_snapshot else None),
        'company_name': info.get('name') or '',
    }

    return record, new_crossover_event

# ─── MAIN ─────────────────────────────────────────────
def run():
    today = datetime.now(US_TZ).date()
    log.info("=" * 60)
    log.info("TrueFlow US Momentum Scanner starting")

    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    log.info("Supabase connected ✅")

    universe = load_universe(sb)
    if not universe:
        log.error("US universe empty. Run us_universe_builder.py first.")
        send_telegram("❌ <b>US Momentum Scanner Failed</b>\nUniverse empty.")
        return

    do_backfill = needs_backfill(sb)

    if do_backfill:
        from_date = BACKFILL_FROM - timedelta(days=FETCH_DAYS)
        log.info(f"🔄 BACKFILL MODE: sessions from {BACKFILL_FROM} (history from {from_date})")
        send_telegram(f"🔄 <b>TrueFlow US Momentum</b>\nBackfill starting from {BACKFILL_FROM}\nThis will take a while. I'll notify when done.")
    else:
        from_date = today - timedelta(days=FETCH_DAYS)

    results = []
    crossover_events = []
    actual_session_date = None
    processed = skipped = cat_a = cat_b = cat_c = cat_ac = sharp = breakouts = 0

    symbols = list(universe.keys())
    total = len(symbols)
    log.info(f"Processing {total} stocks in batches of {BATCH_SIZE}...")

    batches = [symbols[i:i+BATCH_SIZE] for i in range(0, len(symbols), BATCH_SIZE)]
    for bi, batch in enumerate(batches, 1):
        log.info(f"Downloading batch {bi}/{len(batches)}...")
        hist_map = fetch_history_batch(batch, from_date)
        time.sleep(BATCH_SLEEP)

        for symbol in batch:
            try:
                hist = hist_map.get(symbol)
                if not hist or len(hist) < 22:
                    skipped += 1
                    continue

                stock_results = []

                if do_backfill:
                    prev_snapshot = None
                    hist_dates = [h['date'] for h in hist]
                    for end_idx, proc_date in enumerate(hist_dates):
                        if proc_date < BACKFILL_FROM:
                            continue
                        hist_slice = hist[:end_idx+1]
                        if len(hist_slice) < 22:
                            continue
                        record, cross_event = process_stock(symbol, hist_slice, proc_date, universe, prev_snapshot, crossover_events)
                        if record:
                            stock_results.append(record)
                            prev_snapshot = record
                            if cross_event:
                                crossover_events.append(cross_event)
                    if stock_results:
                        for i in range(0, len(stock_results), 100):
                            b = stock_results[i:i+100]
                            try:
                                sb.table("us_momentum_stocks").upsert(b, on_conflict="symbol,session_date").execute()
                            except Exception as ue:
                                log.error(f"{symbol}: Upsert error — {ue}")
                else:
                    try:
                        prev_resp = sb.table("us_momentum_stocks").select(
                            "above_ema9_daily,days_in_uptrend,days_below_ema9,exit_date,category,consol_days,nr_status,nr_day_high,nr_day_low,nr_source_date"
                        ).eq("symbol", symbol).order("session_date", desc=True).limit(1).execute()
                        prev_snapshot = prev_resp.data[0] if prev_resp.data else None
                    except:
                        prev_snapshot = None

                    real_today = hist[-1]['date']
                    if actual_session_date is None:
                        actual_session_date = real_today
                    record, cross_event = process_stock(symbol, hist, real_today, universe, prev_snapshot, crossover_events)
                    if record:
                        stock_results.append(record)
                        results.append(record)
                        if cross_event:
                            crossover_events.append(cross_event)

                if stock_results:
                    last = stock_results[-1]
                    c = last.get('category', '')
                    if c == 'AC': cat_ac += 1
                    elif c == 'A': cat_a += 1
                    elif c == 'B': cat_b += 1
                    elif c == 'C': cat_c += 1
                    if last.get('is_sharp_mover'): sharp += 1
                    if last.get('breakout_type'): breakouts += 1

                processed += 1
                if processed % 100 == 0:
                    log.info(f"Progress: {processed}/{total} stocks processed")

            except Exception as e:
                log.error(f"{symbol}: Error — {e}")
                skipped += 1
                continue

    log.info(f"Processing complete: {processed} done, {skipped} skipped")

    if results and not do_backfill:
        batch_size = 100
        total_batches = (len(results) + batch_size - 1) // batch_size
        for i in range(0, len(results), batch_size):
            batch = results[i:i+batch_size]
            try:
                sb.table("us_momentum_stocks").upsert(batch, on_conflict="symbol,session_date").execute()
                log.info(f"Upserted batch {i//batch_size+1}/{total_batches} ({len(batch)} records)")
            except Exception as e:
                log.error(f"Upsert error batch {i//batch_size+1}: {e}")

    if crossover_events:
        try:
            for i in range(0, len(crossover_events), 200):
                sb.table("us_crossover_events").upsert(crossover_events[i:i+200], on_conflict="symbol,crossover_date,crossover_type,direction").execute()
            log.info(f"Crossover events saved: {len(crossover_events)}")
        except Exception as e:
            log.error(f"Crossover events error: {e}")

    msg = (
        f"🇺🇸 <b>TrueFlow US Momentum Scanner</b>\n"
        f"{'─'*28}\n"
        f"📅 {(actual_session_date or today).strftime('%d %b %Y')}\n"
        f"{'─'*28}\n"
        f"✅ Processed: <b>{processed}</b> stocks\n"
        f"⚠️ Skipped: <b>{skipped}</b>\n"
        f"{'─'*28}\n"
        f"🟢 Category A: <b>{cat_a}</b> (Daily+Weekly Bullish)\n"
        f"🔵 Category C: <b>{cat_c}</b> (Weekly Crossover)\n"
        f"💎 Category A+C: <b>{cat_ac}</b> (Strongest)\n"
        f"🟡 Category B: <b>{cat_b}</b> (Watch Zone)\n"
        f"{'─'*28}\n"
        f"⚡ Sharp Movers: <b>{sharp}</b>\n"
        f"📈 Breakouts: <b>{breakouts}</b>\n"
        f"🔄 Crossover Events: <b>{len(crossover_events)}</b>\n"
        f"{'─'*28}\n"
        f"{'🔄 Backfill complete!' if do_backfill else '✅ Daily scan complete'}"
    )
    send_telegram(msg)
    log.info("US momentum scan complete. Telegram sent. ✅")

if __name__ == "__main__":
    run()
