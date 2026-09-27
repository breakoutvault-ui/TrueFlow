#!/usr/bin/env python3
# ==============================================================
#  Detailed group guides, v2 - written so someone outside the
#  system can read one and understand what the group is for.
#
#  Run:  cd /root/trueflow && ./bin/python send_group_guides_v2.py
#
#  Delete the old pinned message first, then pin the new one.
# ==============================================================

import re, sys, requests
sys.path.insert(0, "/root/trueflow")
import tf_config as c

eng = open("/root/trueflow/alert_engine.py").read()
BN_TOK = re.search(r"8664203514:[A-Za-z0-9_-]{30,}", eng).group(0)
BR_TOK = re.search(r"8665254687:[A-Za-z0-9_-]{30,}", eng).group(0)
AL_TOK = c.TF_ALERTS_TOKEN

GUIDES = [

# ─────────────────────────── BANKNIFTY ───────────────────────────
("BankNifty", BN_TOK, "-1003944949273", """🏦 <b>TRUEFLOW BANKNIFTY</b>
<i>Intraday index options. Stage alerts, live.</i>

━━━━━━━━━━━━━━━━━━━━
<b>WHAT THIS GROUP IS</b>

BankNifty options, traded intraday. The system watches the index all session and posts an alert the moment a setup completes.

The method: a <b>15-minute opening range breakout</b>, taken only when the 5-minute and 15-minute EMAs agree, using an option <b>one strike out of the money</b>.

━━━━━━━━━━━━━━━━━━━━
⭐ <b>THE STAGES — READ THIS PART</b>

Every alert is Stage 1, 2, 3 or 4. Two questions decide which:

<b>Q1. Which timeframes have confirmed?</b>
• Only the 5-minute EMA → Stage 1 or 3
• The 5-minute <b>and</b> the 15-minute → Stage 2 or 4

<b>Q2. Is it going with the daily trend, or against it?</b>
• Against the daily trend → Stage 1 or 2
• With the daily trend → Stage 3 or 4

Put together:

1️⃣ <b>Stage 1</b> — 5m only · <b>against</b> the daily trend
2️⃣ <b>Stage 2</b> — 5m + 15m · <b>against</b> the daily trend
3️⃣ <b>Stage 3</b> — 5m only · <b>with</b> the daily trend
4️⃣ <b>Stage 4</b> — 5m + 15m · <b>with</b> the daily trend

━━━━━━━━━━━━━━━━━━━━
📊 <b>WHAT THE NUMBERS SAY</b>

Measured across <b>5,753 alerts</b>:

<b>S1</b> · 25.5% win · +0.176 R · n=1,998
<b>S2</b> · <b>44.1% win · +0.261 R</b> · n=444
<b>S3</b> · 24.3% win · +0.158 R · n=1,579
<b>S4</b> · 39.1% win · +0.168 R · n=335

The surprise: <b>the 15-minute confirmation is the edge, not the trend alignment.</b> Stage 2 is the best performer <i>despite</i> being counter-trend. Both two-timeframe stages beat both single-timeframe ones.

⚠️ S2 and S4 have only ~400 samples each. Suggestive, not settled.

━━━━━━━━━━━━━━━━━━━━
⏰ <b>WHEN THEY FIRE</b>

<b>09:55</b> — the earliest an S1 or S3 can appear
<b>10:15</b> — the earliest an S2 or S4 can, because that is the first moment a 15-minute EMA exists

An alert claiming Stage 4 before 10:15 would be lying about its own data.

━━━━━━━━━━━━━━━━━━━━
🔤 <b>THE TERMS</b>

<b>R</b> — the trade measured in units of what you risked. +0.261 R means the average trade returned about a quarter of the amount you put at risk.
<b>Win %</b> — how often it ended positive. A low win rate is normal here; the winners are bigger than the losers.
<b>OTM</b> — out of the money. One strike beyond the current price: cheaper, moves faster, expires worthless if you are wrong.
<b>ORB</b> — opening range breakout. The first 15 minutes set a high and a low; the trade is a break of one.

━━━━━━━━━━━━━━━━━━━━
🎯 <b>HOW TO READ AN ALERT</b>

The stage tells you <b>what confirmed</b>, not whether to trade. Options lose value every day they are held, so being right slowly is the same as being wrong.

<i>Everything here is automatic. Nothing needs a reply.</i>"""),

# ─────────────────────────── BREAKOUTS ───────────────────────────
("Breakouts", BR_TOK, "-1004405719798", """🚀 <b>TRUEFLOW BREAKOUTS</b>
<i>Equity swing trades. Stocks leaving a base.</i>

━━━━━━━━━━━━━━━━━━━━
<b>WHAT THIS GROUP IS</b>

Stocks held for days or weeks, not intraday. The idea: a stock runs hard, then goes quiet and tightens into a base. When it leaves that base on heavy volume, the next move often follows quickly.

Everything here is a stock that has just done that, or is about to.

━━━━━━━━━━━━━━━━━━━━
📥 <b>THE TWO KINDS OF MESSAGE</b>

🔔 <b>COIL BREAK — live, during the session</b>
A stock in a tightening base has just cleared its pivot. Sent the moment it happens.

📊 <b>BREAKOUT REPORT — after the close, 5:40 PM</b>
Two lists:
🚀 <b>Confirmed</b> — broke out today and held it
🎯 <b>Near breakout</b> — sitting right under the level, worth watching tomorrow

━━━━━━━━━━━━━━━━━━━━
🔤 <b>THE SETUPS BY NAME</b>

<b>VCP</b> — Volatility Contraction Pattern. Each pullback inside the base is shallower than the last. The range keeps narrowing. Sellers are running out.

<b>EP</b> — Episodic Pivot. A gap up on heavy volume, usually on news or results. Not a base at all — a sudden repricing.

<b>HTF</b> — High Tight Flag. An <b>80%+ run in under 40 sessions</b>, then a shallow pause holding within 25% of the high. Genuinely rare: roughly <b>one a day</b> across the whole universe.

<b>Reclaim</b> — lost the 9 EMA, spent a few days below, then closed back above while the bigger trend held. The shakeout that fails.

━━━━━━━━━━━━━━━━━━━━
🔤 <b>THE NUMBERS ON AN ALERT</b>

<b>Pivot</b> — the price the setup triggers at. Below it, nothing has happened.

<b>RVOL</b> — today's volume against its own 20-day average. <code>2.0×</code> is twice normal. <b>Volume is what separates a real break from a fake one</b> — a breakout on quiet volume usually comes back.

<b>ADR</b> — average daily range as a %. How far this stock moves on a normal day. A 6% ADR stock needs a wider stop than a 2% one; the same rupee stop is loose on one and suffocating on the other.

<b>Conviction ★</b>
★★★ — all timeframes up, sector strong, market not weak
★★ — daily and weekly up, sector strong on one
★ — weekly up, daily not down
✕ — avoid: counter-trend with a weak sector

<b>Base days</b> — how long it has been quiet. Longer bases tend to produce bigger moves.

⚠️ <b>XXL Candle</b> — moved too far too fast. Wait for a pullback rather than chasing.
📅 <b>Daily Trend: ALIGNED ✅</b> — the daily trend agrees with the break.

━━━━━━━━━━━━━━━━━━━━
📊 <b>WHAT THE TESTING FOUND</b>

Measured over a year of data:

• A breakout <b>with 1.5× volume</b> was the strongest single screen tested
• Stops tighter than <b>1.5 × ADR</b> lost money at every width — winners dug 1.5 ADR against the entry before working, at the 80th percentile
• In <b>weak market breadth</b>, the same setups lost about <b>four times as much per trade</b>

━━━━━━━━━━━━━━━━━━━━
🎯 <b>HOW TO USE IT</b>

These are <b>candidates, not instructions</b>. The alert says something happened. Whether it is worth taking depends on the market, the sector, and your own risk.

Check the breadth badge on the dashboard first. Under 50%, be far more selective.

<i>Everything here is automatic. Nothing needs a reply.</i>"""),

# ─────────────────────────── F&O ───────────────────────────
("F&O", AL_TOK, c.TF_FO, """⚡ <b>TRUEFLOW F&amp;O</b>
<i>Futures and options. Intraday alerts and positioning.</i>

━━━━━━━━━━━━━━━━━━━━
<b>WHAT THIS GROUP IS</b>

The F&amp;O stock universe — around 200 names with futures and options. Alerts here are intraday, and the reports tell you where the money moved.

━━━━━━━━━━━━━━━━━━━━
📥 <b>WHAT ARRIVES</b>

🔔 <b>ORB alerts</b> — live. A stock cleared its opening-range high or low.
📊 <b>F&amp;O OI Buildup</b> — after the close. Where positions were opened.
📊 <b>Index OI</b> — the same for the indices.
📋 <b>Tomorrow's watchlist</b> — evening. The names set up for the open.
📈 <b>Daily summary</b> — what the engine saw today.

━━━━━━━━━━━━━━━━━━━━
🔤 <b>ORB, PROPERLY EXPLAINED</b>

The first 15 minutes of the session set a <b>high</b> and a <b>low</b>. That box is the opening range — the market deciding where it wants to be.

A break above the high suggests buyers have won the argument; below the low, sellers.

<b>09:15–09:30 is the range forming. The rule is: no decisions in that window.</b> You are watching it build, not trading it.

━━━━━━━━━━━━━━━━━━━━
🔤 <b>OPEN INTEREST, PROPERLY EXPLAINED</b>

<b>OI</b> is how many contracts are currently live. Not volume — volume counts trades, OI counts positions still open.

Price and OI together tell you what kind of move it is:

📈 <b>Long buildup</b> — price up, OI up. New buyers arriving. Strongest.
📉 <b>Short buildup</b> — price down, OI up. New sellers arriving.
🔄 <b>Short covering</b> — price up, OI <b>down</b>. Sellers closing, not buyers arriving. Often fades.
💤 <b>Long unwinding</b> — price down, OI down. Holders leaving.

The distinction matters: a rally on short covering is people leaving, not people buying.

━━━━━━━━━━━━━━━━━━━━
🔤 <b>OTHER TERMS</b>

<b>RVOL</b> — volume against its own 20-day average.
<b>PDH / PDL</b> — previous day's high and low. Reference levels everyone watches.
<b>Delivery %</b> — the share actually taken to demat rather than squared off intraday. High delivery on a move suggests conviction.

━━━━━━━━━━━━━━━━━━━━
🎯 <b>HOW TO USE IT</b>

Fast and intraday. The evening watchlist is the part to read properly — it is the preparation that makes the next morning calm.

<i>Everything here is automatic. Nothing needs a reply.</i>"""),

# ─────────────────────────── CHART ALERTS ───────────────────────────
("Chart Alerts", AL_TOK, c.TF_CHART, """🔔 <b>TRUEFLOW CHART ALERTS</b>
<i>Only the price levels you drew yourself.</i>

━━━━━━━━━━━━━━━━━━━━
<b>WHAT THIS GROUP IS</b>

The only group with nothing automatic in it. No scans, no screeners, no reports.

Every message here is a level <b>you chose</b>, on a chart <b>you were looking at</b>, that price has now reached.

If this group buzzes, it is because something you were personally waiting for happened.

━━━━━━━━━━━━━━━━━━━━
⚙️ <b>HOW TO SET ONE</b>

1. Open the dashboard, click any stock
2. Hit <b>⤢ Expand</b> for the full chart
3. Click the <b>🔔</b> tool in the toolbar
4. Click the price you want to be told about

That is it. It watches from then on.

━━━━━━━━━━━━━━━━━━━━
⏱ <b>HOW IT ACTUALLY WORKS</b>

Checked <b>every 2 minutes</b> through the session, not continuously.

What that means in practice: a fast spike through your level that snaps straight back <b>can be missed</b>. For a level price sits at or crosses properly, it will fire.

Alerts fire <b>once</b>, then stop. They do not repeat every time price crosses.

━━━━━━━━━━━━━━━━━━━━
🎯 <b>HOW TO USE IT</b>

This is the most personal group you have. You set every one, so unlike a scan, <b>nothing here is noise</b> — each one deserves a look.

Which is exactly why it needs keeping clean. <b>Delete levels you no longer care about</b>, from the same chart. A forgotten alert on a stock you abandoned months ago is how a group like this starts getting ignored — and then a real one gets missed.

<i>Everything here is automatic. Nothing needs a reply.</i>"""),

# ─────────────────────────── US ───────────────────────────
("US", AL_TOK, c.TF_US, """🇺🇸 <b>TRUEFLOW US</b>
<i>The American market, in one place.</i>

━━━━━━━━━━━━━━━━━━━━
<b>WHAT THIS GROUP IS</b>

The same momentum method applied to US stocks, plus the things that only exist in that market — institutional filings, and what politicians disclosed trading.

Everything here lands <b>overnight IST</b>, because the US market closes at 1:30 AM or 2:30 AM our time. This is a morning read, not something to react to.

━━━━━━━━━━━━━━━━━━━━
📥 <b>WHAT ARRIVES</b>

📈 <b>Momentum scan</b> — around 3:30 AM IST, after the US close. The same EP / VCP / HTF setups.
💪 <b>Relative strength</b> — which stocks and sectors are leading.
🏦 <b>Smart money</b> — institutional buying, clusters, tiers.
📄 <b>SEC filings</b> — 13G and 13D stake disclosures. Who bought a big position.
📅 <b>Earnings</b> — results due and reported.
🏛 <b>Congressional trades</b> — US lawmakers must disclose their trades. This tracks them.

━━━━━━━━━━━━━━━━━━━━
🔤 <b>THE SETUPS</b>

Identical to the India side:

<b>EP</b> — gap up on heavy volume, usually news.
<b>VCP</b> — tightening base, each pullback shallower.
<b>HTF</b> — 80%+ run then a shallow pause. Rare.
<b>RVOL</b> — volume against its 20-day average.
<b>ADR</b> — average daily range %.

━━━━━━━━━━━━━━━━━━━━
🔤 <b>US-ONLY TERMS</b>

<b>13G / 13D</b> — filed when someone crosses 5% ownership. 13G is passive, 13D is activist and usually the more interesting of the two.
<b>Cluster buy</b> — several insiders or institutions buying the same name around the same time. Harder to dismiss than one.

━━━━━━━━━━━━━━━━━━━━
⏰ <b>TIMING</b>

US 9:30 AM = <b>7:00 PM IST</b>
US 4:00 PM close = <b>1:30 AM IST</b>
Scans run after that, landing around <b>3:30 AM IST</b>

So you read this at breakfast, with the US market shut. Nothing here is urgent.

<i>Everything here is automatic. Nothing needs a reply.</i>"""),

# ─────────────────────────── SYSTEM ───────────────────────────
("System", AL_TOK, c.TF_SYSTEM, """⚙️ <b>TRUEFLOW SYSTEM</b>
<i>Housekeeping. Nothing to trade here.</i>

━━━━━━━━━━━━━━━━━━━━
<b>WHAT THIS GROUP IS</b>

Every scheduled job on the server reports here when it finishes. No stocks, no alerts, no decisions.

This group exists for one reason: <b>so these messages stop sitting between your trade alerts</b>. Before it existed, a breakout notification would arrive sandwiched between two "backup complete" messages.

━━━━━━━━━━━━━━━━━━━━
📥 <b>WHAT ARRIVES, AND WHEN</b>

<b>5:35 PM</b> · 📊 EOD update — closing prices, EMAs, market strip
<b>6:30 PM</b> · ✅ Momentum scan — the setups computed for tomorrow
<b>8:30 PM</b> · 🧭 MTF scan — weekly and monthly trend states
<b>9:00 PM</b> · 📸 MTF snapshot — the day's trend data saved to history
<b>5:30 AM</b> · 📦 Archive — old sessions moved to compressed files
<b>6:00 AM</b> · 💾 Backup — full database copy, last 7 kept

Plus, whenever they happen: 🔧 engine reconnects, token expiry, crashes.

━━━━━━━━━━━━━━━━━━━━
🎯 <b>HOW TO USE IT — TWO THINGS ONLY</b>

⚠️ <b>Anything saying FAILED.</b> A scan or backup did not complete. Everything downstream of it is stale until fixed.

🔇 <b>Silence.</b> If an evening passes with nothing at all, a job did not run — and a job that fails to start cannot send you a failure message. <b>Missing messages matter more than the messages themselves.</b>

Otherwise, ignore it. Quiet means working.

━━━━━━━━━━━━━━━━━━━━
🔤 <b>THE TERMS</b>

<b>EOD</b> — end of day, after the 3:30 PM close.
<b>MTF</b> — multi-timeframe. Daily, weekly and monthly trend read together.
<b>Snapshot</b> — a dated copy kept so history accumulates. Without it, yesterday's trend data is overwritten and gone for good.
<b>Archive</b> — old rows moved out of the live database into compressed files. Nothing is deleted.

<i>Everything here is automatic. Nothing needs a reply.</i>"""),
]


def main():
    print("Sending detailed guides...\n")
    for name, tok, chat, text in GUIDES:
        r = requests.post("https://api.telegram.org/bot%s/sendMessage" % tok,
                          data={"chat_id": chat, "text": text,
                                "parse_mode": "HTML",
                                "disable_web_page_preview": True}).json()
        if r.get("ok"):
            print("  %-14s sent  (id %s)" % (name, r["result"]["message_id"]))
        else:
            print("  %-14s FAILED: %s" % (name, r.get("description")))
    print("\nUnpin the old guide, then pin this one.")


if __name__ == "__main__":
    main()
