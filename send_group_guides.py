#!/usr/bin/env python3
# ==============================================================
#  Send one guide message to each TrueFlow group, for pinning.
#
#  Run:  cd /root/trueflow && ./bin/python send_group_guides.py
#
#  Safe to re-run - it just sends again. Delete the old one if you do.
# ==============================================================

import re, sys, requests
sys.path.insert(0, "/root/trueflow")
import tf_config as c

eng = open("/root/trueflow/alert_engine.py").read()
BN_TOK = re.search(r"8664203514:[A-Za-z0-9_-]{30,}", eng).group(0)
BR_TOK = re.search(r"8665254687:[A-Za-z0-9_-]{30,}", eng).group(0)
AL_TOK = c.TF_ALERTS_TOKEN

FOOT = ("\n\n<i>Everything here is automatic. Nothing in this group needs a reply.</i>")

GUIDES = [

("Breakouts", BR_TOK, "-5463362876", """🚀 <b>TRUEFLOW BREAKOUTS</b>
<i>Stocks breaking out, right now.</i>

━━━━━━━━━━━━━━━━━
📥 <b>WHAT ARRIVES HERE</b>

🔔 <b>Coil breaks</b> — live, during market hours. A stock that has been tightening finally moves.
📊 <b>Breakout Report</b> — after the close. Two lists: what confirmed today, and what is close.

━━━━━━━━━━━━━━━━━
🔤 <b>WHAT THE WORDS MEAN</b>

<b>Coil</b> — price squeezing into a tighter and tighter range. Stored energy.
<b>Pivot</b> — the level the setup triggers on. Above it, the trade is live.
<b>RVOL</b> — today's volume against its own 20-day average. <code>2.0×</code> means twice normal. Volume is what separates a real break from a fake one.
<b>ADR</b> — how much this stock moves on an average day, as a %. A 6% ADR stock needs more room than a 2% one.
<b>Conviction ★</b> — how many conditions lined up. More stars, more of the checklist met.

⚠️ <b>XXL Candle</b> — it moved too far too fast. Wait for a pullback rather than chasing.
📅 <b>Daily Trend: ALIGNED ✅</b> — the daily trend agrees with the break.

━━━━━━━━━━━━━━━━━
🎯 <b>HOW TO USE IT</b>

These are <b>candidates, not instructions</b>. The alert says something happened — you decide whether to take it.

Check the market breadth badge on the dashboard first. When fewer than half the market is above its 50 EMA, these setups have historically lost roughly four times as much per trade.""" + FOOT),

("F&O", AL_TOK, c.TF_FO, """⚡ <b>TRUEFLOW F&amp;O</b>
<i>Futures and options — intraday alerts and positioning.</i>

━━━━━━━━━━━━━━━━━
📥 <b>WHAT ARRIVES HERE</b>

🔔 <b>ORB alerts</b> — live, during the session.
📊 <b>F&amp;O OI Buildup</b> — where the open interest went.
📊 <b>Index OI</b> — the same for the indices.
📋 <b>Tomorrow's watchlist</b> — after the close, the names to watch at the open.
📈 <b>Daily summary</b> — what the engine saw today.

━━━━━━━━━━━━━━━━━
🔤 <b>WHAT THE WORDS MEAN</b>

<b>ORB</b> — Opening Range Breakout. The first 15 minutes set a high and a low; the trade is a break of one of them.
<b>OI</b> — Open Interest. How many contracts are live. Rising OI with rising price means new money coming in, not just squaring off.
<b>Long/Short buildup</b> — price and OI rising together is long buildup; price falling with OI rising is short buildup.
<b>RVOL</b> — volume against its own 20-day average.
<b>Stage 1–4</b> — where the move is in its life. 3 and 4 run with the daily trend; 1 and 2 run against it.

━━━━━━━━━━━━━━━━━
🎯 <b>HOW TO USE IT</b>

Fast-moving and intraday. Worth knowing: 9:15–9:30 is the opening range forming — the rule is <b>no decisions</b> in that window.""" + FOOT),

("Chart Alerts", AL_TOK, c.TF_CHART, """🔔 <b>TRUEFLOW CHART ALERTS</b>
<i>Only the levels you drew yourself.</i>

━━━━━━━━━━━━━━━━━
📥 <b>WHAT ARRIVES HERE</b>

🔔 <b>Price alert</b> — a level you set on a dashboard chart has been touched.

Nothing else. No scans, no reports, no system messages. If this group buzzes, <b>a level you chose was reached</b>.

━━━━━━━━━━━━━━━━━
⚙️ <b>HOW IT WORKS</b>

Open any chart in the dashboard → 🔔 tool → click a price. It is checked every 2 minutes through the session.

<b>Every 2 minutes</b>, not continuously — a fast spike through a level and straight back can be missed.

━━━━━━━━━━━━━━━━━
🎯 <b>HOW TO USE IT</b>

This is the most personal group you have. You set every one of these, so each is worth a look — unlike a scan, nothing here is noise.

Delete levels you no longer care about, from the same chart. A forgotten alert on a stock you abandoned months ago is how this group starts getting ignored.""" + FOOT),

("US", AL_TOK, c.TF_US, """🇺🇸 <b>TRUEFLOW US</b>
<i>Everything US, in one place.</i>

━━━━━━━━━━━━━━━━━
📥 <b>WHAT ARRIVES HERE</b>

📈 <b>Momentum scan</b> — runs after the US close, around 3:30 AM IST.
💪 <b>Relative strength</b> — what is leading.
🏦 <b>Smart money</b> — institutional activity.
📄 <b>SEC filings</b> — stake changes and disclosures.
📅 <b>Earnings tracker</b> — results due and reported.
🏛 <b>Congressional trades</b> — what US politicians disclosed buying and selling.

━━━━━━━━━━━━━━━━━
🔤 <b>WHAT THE WORDS MEAN</b>

<b>EP</b> — Episodic Pivot. A gap up on heavy volume, usually on news.
<b>VCP</b> — Volatility Contraction. Tightening range before a move.
<b>HTF</b> — High Tight Flag. An 80%+ run, then a shallow pause. Rare — roughly one a day across the whole universe.
<b>RVOL / ADR</b> — same meanings as the India groups.

━━━━━━━━━━━━━━━━━
⏰ <b>TIMING</b>

These land overnight IST, so they are a <b>morning read</b>, not something to act on the moment it arrives.""" + FOOT),

("System", AL_TOK, c.TF_SYSTEM, """⚙️ <b>TRUEFLOW SYSTEM</b>
<i>Housekeeping. Nothing to trade here.</i>

━━━━━━━━━━━━━━━━━
📥 <b>WHAT ARRIVES HERE</b>

✅ <b>Scan finished</b> — momentum, MTF, EOD.
💾 <b>Database backup</b> — nightly, keeps the last 7.
📦 <b>Archive</b> — old sessions moved to compressed files on the server.
📸 <b>MTF snapshot</b> — the day's trend data saved so history builds up.
🔧 <b>Engine messages</b> — reconnects, token expiry, crashes.

━━━━━━━━━━━━━━━━━
🎯 <b>HOW TO USE IT</b>

<b>Ignore it when it is quiet.</b> That is the point of this group — it exists so these messages stop sitting between your trade alerts.

Two things worth reacting to:

⚠️ Anything saying <b>FAILED</b> — a backup or scan did not complete.
🔇 <b>Silence.</b> If a whole evening passes with nothing at all, something did not run. Missing messages matter more here than the messages themselves.

━━━━━━━━━━━━━━━━━
⏰ <b>WHEN TO EXPECT IT</b>

Most land between <b>6:00 PM and 10:00 PM IST</b>, after the close.""" + FOOT),

("BankNifty", BN_TOK, "-5419812169", """🏦 <b>TRUEFLOW BANKNIFTY</b>
<i>Index alerts and stage tracking.</i>

━━━━━━━━━━━━━━━━━
📥 <b>WHAT ARRIVES HERE</b>

🎯 <b>Stage 1–4 alerts</b> — live, during the session.
📊 <b>Session tracking</b> — how the index is behaving.
💬 <b>Live commentary</b> — running notes through the day.

━━━━━━━━━━━━━━━━━
🔤 <b>WHAT THE STAGES MEAN</b>

The stages describe <b>where a move sits relative to the daily trend</b>:

<b>Stage 3 & 4</b> — running <i>with</i> the daily trend. These are the aligned ones.
<b>Stage 1 & 2</b> — running <i>against</i> it. Counter-trend, and they behave differently.

What separates 2 from 4, and 1 from 3, is <b>15-minute confirmation</b> — whether the shorter timeframe has agreed yet.

━━━━━━━━━━━━━━━━━
🎯 <b>HOW TO USE IT</b>

Options decay, so time matters more here than on a swing. The methodology is 15-minute ORB with 5/15-minute EMA alignment, one step OTM.

A stage alert says the setup formed. It does not say the trade is good — the market context still decides that.""" + FOOT),
]


def main():
    print("Sending guides...\n")
    for name, tok, chat, text in GUIDES:
        r = requests.post("https://api.telegram.org/bot%s/sendMessage" % tok,
                          data={"chat_id": chat, "text": text,
                                "parse_mode": "HTML",
                                "disable_web_page_preview": True}).json()
        if r.get("ok"):
            print("  %-14s sent  (message id %s - pin this one)"
                  % (name, r["result"]["message_id"]))
        else:
            print("  %-14s FAILED: %s" % (name, r.get("description")))
    print("\nOpen each group, long-press the message, choose Pin.")


if __name__ == "__main__":
    main()
