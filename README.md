# Option Terminal — Phases 1–6

Live Indian index-options data collector, dashboard, signal engine, Telegram
alerts, Black-76 analytics, and a backtester for Angel One SmartAPI.

* **Phase 1** — authenticated session management, the database layer, and a live
  WebSocket collector that streams one index's option chain into storage.
* **Phase 2** — a **read-only Streamlit dashboard** (live option chain, spot,
  PCR, max pain, ATM), auto-refreshing.
* **Phase 3** — a **signal engine** turning PCR, max pain, OI build-up/flow, and
  support/resistance walls into scored signals with R-multiple trade frames.
* **Phase 4** — a **Telegram alert pipeline** (priority + cooldown) plus an
  interactive command bot.
* **Phase 5** — **Black-76 analytics**: per-strike IV and Greeks, IV smile,
  gamma exposure (GEX), IV rank/percentile.
* **Phase 6** — a **backtester** that replays stored history through the same
  signal engine, simulates trades in R-multiples, and reports win rate,
  expectancy, profit factor, drawdown, and plain-language insights.

> ⚠️ Everything here is heuristic/analytical tooling, **not trading advice**.
> Backtest results describe the past on your own captured data and do not
> predict future performance.

### A note on live order execution (intentionally not built yet)

Auto-trading is deliberately **deferred** and not included in this codebase. The
project's principle from day one has been that the order-execution module is the
one place a bug spends real money, so it must be built last — paper-mode first,
behind a hard kill-switch and risk limits — rather than appended at the end of a
build sprint. The backtester here is the safe way to evaluate the strategy until
that gated execution phase is designed properly.

## Why the collector is a standalone process (important)

Streamlit reruns its whole script on every interaction and cannot host a 24/7
WebSocket. So the design is:

```
  collector (this, on a VPS)  ──writes──▶  database  ◀──reads──  Streamlit dashboard (Phase 2)
```

`main.py` is the long-lived collector. The future dashboard will be a separate
process that only reads from the same database.

## Architecture

```
Angel One SmartAPI ──(REST login + WebSocketV2)──▶ AngelOneSession
                                                        │
                              InstrumentRepository ◀── scrip master
                                                        │
                                              MarketDataCollector
                                                        │ (batched writes)
                                                Database (SQLite/WAL → Postgres)
```

Every layer talks to the abstract `Database` interface, so migrating SQLite →
PostgreSQL later means adding one class, not touching callers.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill in your SmartAPI credentials
```

You need an Angel One SmartAPI app (API key) and your client code, PIN, and the
**base32 TOTP secret** from the authenticator QR you set up for SmartAPI.

### Optional: encrypt credentials at rest

```bash
python -m utils.crypto keygen                 # prints a Fernet key
python -m utils.crypto encrypt "your_pin"     # prompts for the key, prints ciphertext
```

Put the ciphertext values in `.env`, set `OT_CREDENTIALS_ENCRYPTED=true`, and
provide `OT_ENCRYPTION_KEY` (ideally via a secrets manager / systemd credential,
not the `.env` file itself).

## Run

### Phase 1 — the collector (writes data)

```bash
python main.py
```

This logs in, downloads the scrip master, resolves NIFTY ATM ± N strikes for the
nearest expiry, subscribes to the live feed, and writes ticks to the database.
Stop with Ctrl-C (it drains the write buffer and closes cleanly).

### Phase 2 — the dashboard (reads data)

In a **second terminal**, with the collector running:

```bash
streamlit run dashboard/app.py
```

The dashboard opens the same SQLite file in query-only mode and re-reads the
latest chain every few seconds (adjustable in the sidebar). It never writes, so
it cannot interfere with the collector. If you start it before the collector has
produced any ticks, it shows a "waiting for data" message and fills in once
ticks arrive.

> Why two processes? Streamlit reruns its script on every interaction and can't
> hold a 24/7 websocket. The collector owns the feed; the dashboard only reads.

### Phase 3 — the signal engine (writes signals)

In a **third terminal**, with the collector running:

```bash
python -m signals.runner
```

It reads the latest chain plus an earlier snapshot (for OI flow), evaluates the
indicators, and writes scored signals to the `signals` table on an interval. The
dashboard shows them live in its "Signals" panel. A short cooldown prevents
storing a near-identical signal every cycle.

When Telegram is configured (below), the same runner also **pushes alerts** for
each new signal that clears the priority gate.

### Phase 4 — Telegram alerts + command bot

Set up a bot once with [@BotFather](https://t.me/BotFather), then put the token
and your chat id in `.env`:

```
OT_TELEGRAM_BOT_TOKEN=123456:ABC-your-bot-token
OT_TELEGRAM_CHAT_ID=your_chat_or_channel_id
OT_ALERT_MIN_PRIORITY=MEDIUM      # LOW/MEDIUM/HIGH/CRITICAL gate for pushing
OT_ALERT_COOLDOWN_SEC=180         # per (index,direction,kind)
```

With those set, `python -m signals.runner` pushes alerts automatically (priority
derived from confidence/risk; duplicates suppressed by cooldown). Without them,
alerts still log to the console. For the interactive bot, run it as its own
process:

```bash
python -m alerts.telegram_bot
```

Commands: `/status`, `/signals [INDEX]`, `/pcr [INDEX]`, `/maxpain [INDEX]`,
`/settings`, `/help`. If `OT_TELEGRAM_CHAT_ID` is set, the bot only answers that
chat. (`/trades`, `/open`, `/history`, `/chart` reply "not available yet" — they
depend on later phases.)

### Phase 5 — Greeks & IV analytics

No extra processes. When the signal runner is running it also records an **ATM
IV snapshot** each cycle (into `iv_history`), which powers IV rank/percentile.
The dashboard gains a **Greeks tab** showing per-strike IV and Greeks, the IV
smile, gamma exposure (GEX), and ATM IV / IV rank / IV percentile.

The model is **Black-76** (options on the forward `F = S·e^{(r−q)T}`), since
Angel One does not provide Greeks. Two assumptions are configurable:

```
OT_RISK_FREE_RATE=0.065     # 6.5%
OT_DIVIDEND_YIELD=0.012     # 1.2%
```

Units: IV in percent; delta is spot delta; vega per 1% vol; theta and charm per
calendar day. First-order Greeks are closed-form; vanna/charm/speed are central
finite differences off them (the tests cross-check first-order Greeks against
finite differences of price, and verify put-call parity and IV round-trip).

### Phase 6 — Backtesting

Once you've captured some history (collector + signal runner running for a
while), replay it through the engine:

```bash
python -m backtest.runner            # all configured indices
python -m backtest.runner NIFTY      # one index
```

It reconstructs the chain at each point in time, runs the **same** SignalEngine
the live system uses, simulates each signal against the subsequent spot path
(stop/target in R-multiples), and prints + saves a JSON report
(`logs/backtest_<INDEX>.json`) with win rate, expectancy, profit factor,
drawdown, per-type/direction breakdowns, and insights. The dashboard also has a
"Backtest" panel that runs it on demand and charts the equity curve.

Fill model: spot tick samples (not OHLC), first-touch exit assumed to fill at
the level; outcomes in R where 1R = the trade's own entry-to-stop distance.
These assumptions are documented in `backtest/simulator.py` so the numbers are
interpretable.

## Test

```bash
pytest -q
```

The suite runs fully offline — no broker connection needed. It covers credential
encryption, SQLite persistence + WAL, scrip-master parsing / expiry resolution /
ATM strike selection, and WebSocket-packet normalisation (paise→rupees, OI,
bid/ask).

## Configuration (env vars, all prefixed `OT_`)

| Variable | Default | Notes |
|---|---|---|
| `OT_API_KEY` / `OT_CLIENT_CODE` / `OT_PIN` / `OT_TOTP_SECRET` | — | SmartAPI credentials |
| `OT_CREDENTIALS_ENCRYPTED` | `false` | If true, the four above are Fernet ciphertext |
| `OT_ENCRYPTION_KEY` | — | Required when credentials are encrypted |
| `OT_DB_BACKEND` | `sqlite` | `sqlite` now; `postgres` is the planned swap |
| `OT_DB_PATH` | `data/option_terminal.db` | SQLite file path |
| `OT_INDICES` | `NIFTY` | Comma-separated; Phase 1 collects the first one |
| `OT_STRIKES_AROUND_ATM` | `15` | Strikes selected on each side of ATM |
| `OT_LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |

## Project layout

```
option_terminal/
  config/settings.py        # typed env config (Pydantic)
  api/session.py            # SmartAPI login, token refresh, backoff
  database/
    interface.py            # abstract Database (the swap boundary)
    sqlite_db.py            # SQLite + WAL implementation
    factory.py              # backend selection
    models.py schema.py     # records + DDL
  collectors/
    instruments.py          # scrip master → spot token, expiry, ATM strikes
    market_data.py          # SmartWebSocketV2 collector + tick normalisation
  analytics/
    indicators.py           # shared math: chain, PCR, max pain, S/R, OI build-up
    greeks.py               # Black-76 pricing, Greeks, IV solver
    chain_analytics.py      # per-strike IV/Greeks, IV smile, GEX, IV rank
  signals/
    models.py               # Signal model + enums
    engine.py               # indicator votes → scored signals + R-multiple frame
    runner.py               # read snapshots → evaluate → persist → dispatch alerts
  alerts/
    models.py               # Priority + Alert model + signal→priority mapping
    formatting.py           # Telegram HTML message rendering
    channels.py             # NotificationChannel ABC + Telegram/console channels
    dispatcher.py           # priority gate + cooldown dedup + fan-out + persist
    factory.py              # build dispatcher from settings
    telegram_bot.py         # interactive command bot (python-telegram-bot)
  dashboard/
    data.py                 # dashboard summary (re-exports analytics)
    app.py                  # read-only chain + greeks + signals + backtest tabs
  backtest/
    models.py               # config, trade result, report
    simulator.py            # pure trade simulation to stop/target (R-multiples)
    metrics.py              # win rate, expectancy, profit factor, drawdown
    datasource.py           # data-source protocol + DB-backed implementation
    engine.py               # replay history → signals → simulated trades
    insights.py             # rule-based plain-language summary
    runner.py               # CLI: backtest + save JSON report
  .streamlit/config.toml    # dark theme
  utils/                    # crypto, logging
  tests/                    # offline unit tests (Phases 1–6)
  main.py                   # Phase 1 collector entrypoint
```

## Known Phase 1 boundaries (handled in later phases)

- **One index streamed** per process for now (NIFTY by default). Running all
  four is a matter of launching the collector per index or extending `main.py`.
- **Greeks/PCR/etc. are not computed yet** — that is Phase 3/5. Angel One does
  not provide Greeks; they will be computed locally (Black-76 + IV solver).
- **Strike window vs. subscription limits**: keep `OT_STRIKES_AROUND_ATM`
  sane (default 15 → 31 strikes × 2 = 62 option tokens + spot per index).
```
