# Binance Telegram Auto-Trading Bot PRO

Python 3.11+ Telegram bot for manual premium subscriptions, Binance USD-M Futures
market scanning, deterministic SMC signals, Testnet-first account controls, and
optional automated execution.

## Railway deployment

1. Extract this ZIP and deploy the project root from a GitHub repository or with
   the Railway CLI (`railway up`).
2. In the Railway service, add only these user-supplied variables:
   - `BOT_TOKEN` — token created with Telegram's BotFather.
   - `ADMIN_CHAT_ID` — your positive numeric Telegram user/private-chat ID.
     The admin panel intentionally works only in that user's private chat.
3. Attach a Railway **Volume** to the service and set its mount path to `/data`.
   Railway provides `RAILWAY_VOLUME_MOUNT_PATH` automatically. No third
   user-defined variable is needed.
4. Deploy and open a private chat with the bot. Use `/admin` to set plan prices,
   payment instructions, scanner filters, and risk limits before inviting users.
5. Users select a plan, submit a transaction reference and proof, and remain
   locked until you approve the payment in the private admin panel. Verify the
   transaction independently; a screenshot by itself is not payment verification.
6. Start with a Binance Futures **Testnet** API key. API credentials are entered
   privately in the bot, verified before storage, encrypted with Fernet, and
   stored separately from the SQLite database. Keep Binance withdrawal
   permission disabled.
7. Auto-trading starts **off per user**. Testnet is the default. Real trading
   additionally requires admin permission, a verified Real API key, two
   separate user confirmations, and a final per-user auto-trading opt-in.

### Persistent data and encryption

The bot stores `database.db`, the separate `credential_master.key`, and rotating
logs in Railway's mounted `/data` volume. The encryption key is generated
automatically with restrictive file permissions; it is never written to the
database or source code. The volume must remain attached across deploys:

- Without a Railway volume, subscription/payment records may disappear on
  redeploy and encrypted Binance credentials are not enabled.
- Losing the volume or its key file makes stored credentials unreadable. Back up
  the database and key together in a protected location; never commit or email
  the encryption key.

Locally, state files are kept beside the Python modules. The included
`database.db` is an empty initialized database with default plans/settings; it
contains no user data or credentials.

## Initial configuration

Plan durations default to Daily (1 day), Weekly (7 days), Monthly (30 days), and
Lifetime (no expiry). Prices start at zero, which keeps payment disabled until
the admin sets a price. Use the admin panel to set payment instructions before
accepting payment.

Risk defaults: 0.5% equity per trade, 3x maximum leverage, 3 concurrent
positions, minimum 1.5:1 reward/risk, and 2% daily loss limit. Orders are skipped
if a risk-sized quantity is below an exchange minimum; quantity is never
increased to force a fill. The admin panel requires a second confirmation for
risk-increasing changes.

## Implemented controls

- Strict unpaid-user menu and premium authorization checks on commands and
  callbacks.
- Manual payment queue with proof copy, one-time approval/rejection, duplicate
  reference prevention, expiry checks, and admin audit events.
- Private admin center for users, payments, plans, payment instructions,
  scanner settings, trading permissions, emergency stop, risk, and recent logs.
- User-isolated encrypted Testnet/Real Binance credentials.
- Public USDT-M perpetual market discovery, volume/exclusion filters, 4H/1H/15M/5M
  completed-candle scanner, request pacing, candle cache, per-symbol recovery,
  and stored scan outcomes.
- Deterministic SMC setup checks for 4H structure, 1H sweep/reclaim and
  displacement BOS, then a later 15M/5M OB/FVG retest. Signals are not fabricated.
- Exchange-filter-aware position sizing, per-user/per-environment trade records,
  Binance USDⓈ-M Futures conditional protections through the Algo Order API
  (`STOP_MARKET` and `TAKE_PROFIT_MARKET`), actual open-order verification,
  reduce-only partial exits, replacement stop verification, break-even/trailing
  management, and a verified emergency reduce-only close if protection cannot
  be installed.
- Signal age starts when the complete setup is saved; `sweep_time` is retained
  as structure metadata and never controls expiry. The default signal lifetime
  is 90 minutes. A fresh Futures mark, wallet, current positions, symbol
  filters, entry zone, stop/targets, and risk are checked immediately before an
  order is submitted.
- Rejections are persisted with explicit reason codes. The Telegram workspace
  includes dashboard, unexpired signals, live exchange positions, trade
  history, verified-fill performance, management settings, and user audit logs.
- The position worker polls Binance REST every 20 seconds, reconciles persistent
  active trades after process restart, checks Algo orders and actual fills,
  repairs missing protections without duplicating live orders, uses Binance
  unrealized/realized PnL, and edits each trade's Telegram manager. Each manager
  has close confirmation, manual TP tranches, move-stop-to-breakeven, and refresh
  controls, with user/admin authorization checked per callback. WebSocket
  market/user streams are not configured; the dashboard reports REST polling.
- Railway worker start command and SQLite persistence.

## Offline regression checks

From the project root, run:

```bash
python -m unittest discover -s tests -v
```

These tests use a mocked Binance client and temporary SQLite files. They verify
Algo-service SL/TP creation and open-order verification, order ID persistence,
long/short trigger directions, missing-stop recovery, live PnL, partial and
manual TP actions, breakeven replacement, stop fills, TP3 completion, and
manual close. They do not connect to Telegram or place real/testnet orders.

## Important limitations and safety notes

- Futures trading can cause rapid losses. This software is not financial advice,
  and its signals do not promise a profit.
- Binance API endpoints, credentials, filters, rates, and account permissions
  may change. Test every release with small Testnet orders before enabling Real.
- Binance Futures API access cannot prove a key's withdrawal permission from
  the trade endpoints used here. Verify in Binance key settings that withdrawals
  are disabled.
- If the emergency close after stop-placement failure cannot be confirmed, the
  bot alerts the user/admin and marks the trade for manual review. Check the
  Binance account immediately.
- Keep one running bot instance per database/volume. Multiple polling workers
  can conflict; order client IDs reduce but do not replace single-instance
  operation.
- Configure Telegram privacy and restrict who can access the admin account.

## Run locally

```bash
python -m pip install -r requirements.txt
export BOT_TOKEN="..."
export ADMIN_CHAT_ID="123456789"
python main.py
```

Do not put real API keys or bot tokens in source files, screenshots, logs, or
committed configuration. For production, use Railway's Variables interface.

