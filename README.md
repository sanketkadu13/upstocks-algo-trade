# NIFTY Options Engine (Upstox)

A single-operator trading platform for NIFTY options on Upstox: multi-strategy
short strangles with live monitoring, an IPO breakout watcher, Telegram
alerting, and a kill switch that nothing in the app can route around.

Dashboard: **http://127.0.0.1:8000/**

## Quick start

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt   # Windows
.venv\Scripts\python.exe -m playwright install chromium       # only for auto-login

copy .env.example .env        # fill in UPSTOX_API_KEY / SECRET / REDIRECT_URI
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Log in from the dashboard banner each morning — Upstox access tokens expire
around **03:30 IST** and there is no silent refresh. The token is saved to
`data/upstox_token.json`; you never need to edit `.env` for it.

Automatic daily login is available (Settings → Automatic daily login) and
optional. **Deployed to a server, the redirect URI must change** — see
[deploy/LOGIN.md](deploy/LOGIN.md), which covers both the manual and
automatic flows and the mistakes that break them.

## Safety model

Everything that can move money is funnelled through one place.

- **`OrderGateway` is the only path to an order.** Strategies, manual buttons
  and any future module call it, never the broker. That makes the kill switch
  unbypassable and gives one complete audit trail.
- **Kill switch** — arm it and every order is refused and logged, including
  ones a strategy tries to place automatically.
- **Active view** — "if something can place an order, it must appear here",
  with real-money rows flagged.
- **Paper mode per strategy** — simulated fills priced at the realistic exit
  side of the book, not at LTP.
- **Partial fills are never silent.** If one leg of a strangle fills and the
  other fails, the position is recorded, kept tracked so it can be squared
  off, logged `CRITICAL`, and pushed to Telegram.

## Price reliability

- Primary: Upstox v3 WebSocket feed, auto-reconnecting with backoff and
  resubscribing every key on reconnect.
- Fallback: any key that hasn't ticked in >5s is polled over REST.
- Every price carries its age; anything older than 10s is marked stale, and
  **no exit trigger can fire on an incomplete quote set** — a missing leg
  would otherwise read as a large profit.
- `scripts/feed_smoke_test.py` checks the feed alone in 30 seconds.

## Dual-basis P&L

Every position is marked two ways:

| basis | meaning |
|---|---|
| `ltp` | last traded price — optimistic, you can't transact there |
| `exit` | what you'd really get: a long hits the bid, a short lifts the ask |

Triggers default to the **exit** basis, and the gap is shown as *slippage*.
This is not a rounding detail — on a cheap OTM option the spread is routinely
a double-digit percentage of the premium, so an LTP-based target can book a
loss you thought was a win.

## Strategies

Each strategy has its own config: lots, target, stop, trigger basis, trailing
stop, profit lock, auto-entry time and expiry, VIX limit and square-off time.

- **Strikes**: `CE = ceil(prevHigh/50)*50`, `PE = floor(prevLow/50)*50`
- **Trailing stop** arms at a threshold then follows the peak down
- **Profit lock** is a one-way floor — once pinned it never lowers
- **Exit ladder**: marketable limit, chased wider (0.5% → 1.5% → 3%), then a
  market fallback, rather than firing a naked market order into a thin book
- **Skip rules**: expiry day (0-DTE) and India VIX above a limit
- Auto-entry claims the day *before* attempting, so a crash mid-entry costs a
  missed trade rather than a duplicate one

## IPO Watch

Watches recently listed stocks and fires when price breaks the listing-day
high (stop = listing-day low, target = listing high × 1.30, anchored to the
high so a gap-up entry doesn't shrink the target). **Alert only — it never
places an order.**

Listing data is derived from Upstox's own daily candles: a stock's first-ever
candle *is* its listing day. No third-party scraping to maintain.

Guards: a stock whose history predates the lookback window is refused as "not
a recent listing", and one that already broke its listing high is refused
unless forced.

## Alerts and checks

- **Telegram** on entry, exit, partial fill, kill-switch change, IPO breakout,
  external square-off, day-rollover-with-open-position, plus the pre-market
  check and EOD summary. Configure in Settings.
- **Pre-market check** (auto at 09:00, or on demand) verifies token expiry,
  account, funds, feed freshness, instrument master age, previous-day range,
  what's armed to fire, kill-switch state and alerting.

## Layout

```
app/
  broker.py            swappable broker interface
  upstox_broker.py     Upstox REST (auth, quotes, orders, positions, historical, funds)
  upstox_feed.py       v3 WebSocket feed incl. market depth
  price_cache.py       single source of truth for prices + freshness
  price_poller.py      REST fallback for stale keys
  order_gateway.py     the only path to an order; kill switch; audit
  mtm.py               pure P&L and trigger maths (fully unit-tested)
  strategy_engine.py   entry, monitoring, exit orchestration
  strategies_store.py  multi-strategy config persistence
  market_calendar.py   trading days, holidays, market hours
  health_check.py      pre-market readiness
  schedules.py         daily jobs (health check, EOD summary)
  notify.py            Telegram
  audit.py, storage.py durable append-only log + atomic writes
  ipo/                 listings, watchlist, scanner, monitor
  api/                 FastAPI routes incl. /api/stream (SSE)
static/dashboard/      the UI (vanilla HTML/CSS/JS, no build step)
deploy/                systemd, nginx, install.sh, backup.sh, DEPLOY.md
scripts/               smoke tests + backtest
tests/                 31 tests
```

## Scripts

```bash
.venv\Scripts\python.exe scripts\feed_smoke_test.py       # is the feed ticking?
.venv\Scripts\python.exe scripts\backtest.py --from 2026-06-01 --to 2026-08-31
.venv\Scripts\python.exe scripts\smoke_test.py --i-understand-this-places-real-orders
.venv\Scripts\python.exe -m pytest tests\ -q
```

`smoke_test.py` places one real 1-lot order and squares it off — run it once
before trusting LIVE mode.

## Deployment

See [deploy/DEPLOY.md](deploy/DEPLOY.md). systemd + nginx with TLS and basic
auth; `sudo ./deploy/install.sh` is re-runnable and never overwrites `.env`
or `data/`.

**The service runs exactly one worker, deliberately.** Live positions, the
price cache and the kill switch are in-process state; a second worker would
be a second trading engine.

## Known limits

- **Backtest is an approximation.** Upstox doesn't retain premiums for expired
  contracts, so legs are priced with Black-Scholes off real spot history at a
  flat IV. Directional, not a P&L promise.
- **Holiday calendar is hand-maintained.** `reference/nse_holidays_2026.json`
  has several lunar dates marked `tentative` — verify against NSE before
  relying on it. A missing year degrades to weekends-only with a warning.
- **Auto-login is built but disabled** (`AUTO_LOGIN_ENABLED=false`). It drives
  the real OAuth page headlessly with a TOTP secret; it needs a correct
  `UPSTOX_TOTP_SECRET` and puts your PIN in `.env`.
- **Margin estimate in the health check is rough** (~₹80k/leg) — it exists to
  catch "nowhere near enough", not to be exact.
