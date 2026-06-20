# Zerodha Kite Connect Integration

Replaces yfinance's ~15-minute delayed price feed with Zerodha's real-time WebSocket stream.
yfinance is kept as a silent fallback — if any Zerodha credential is absent or the feed
fails, the bot continues polling via yfinance with no alert loss.

---

## Files

| File | Role |
|------|------|
| `alert_bot/kite_auth.py` | Daily TOTP authentication, token persistence |
| `alert_bot/kite_feed.py` | `KitePriceFeed` — WebSocket price cache |
| `data/kite_token.json` | Persisted access token (gitignored) |

The only change to existing code is in `alert_bot/main.py`: `_fetch_one()` tries
`_kite_feed.get_price()` first before falling through to yfinance.

---

## Setup

### 1. Get a Kite Connect subscription

Subscribe at https://kite.trade/ (~₹2000/month). Create an app and note:
- **API key** and **API secret**
- Set the app's **redirect URL** to any URL — `http://127.0.0.1` works fine, it does not
  need to be a live server.

### 2. Get your TOTP secret

The `ZERODHA_TOTP_SECRET` is the **base32 seed** Zerodha shows when you set up TOTP
two-factor authentication — it is the long alphanumeric string (e.g. `JBSWY3DPEHPK3PXP`),
**not** the 6-digit rotating code you type daily.

If you set up 2FA with an authenticator app (Google Authenticator, Authy, etc.), look for
the option to export or show the secret key when adding the account.

### 3. Add credentials to `.env`

```bash
ZERODHA_API_KEY=your_api_key
ZERODHA_API_SECRET=your_api_secret
ZERODHA_USER_ID=AB1234          # your Zerodha client ID
ZERODHA_PASSWORD=your_password
ZERODHA_TOTP_SECRET=JBSWY3DPEHPK3PXP   # base32 seed, NOT the 6-digit code
```

Optionally override poll intervals (these are the defaults):
```bash
# POLL_INTERVAL_SECONDS=10           # used when Kite WebSocket is connected (default)
# POLL_INTERVAL_FALLBACK_SECONDS=180 # used when falling back to yfinance (default)
```

### 4. Install dependencies

```bash
pip install kiteconnect pyotp
```

Both are listed in `requirements.txt` — a fresh `pip install -r requirements.txt` handles
this automatically.

---

## How It Works

### Authentication (`kite_auth.py`)

Zerodha access tokens expire daily at **6 AM IST (00:30 UTC)**. The auth module handles
renewal automatically:

1. On bot startup and at every **market-open daily reload**, `get_kite_client()` is called.
2. It checks `data/kite_token.json` — if the cached token is still valid, no network call
   is made.
3. If the token is expired or absent, `_login_with_totp()` runs a headless 3-step login:
   - POST `/api/login` with user ID + password → get `request_id`
   - Generate TOTP via `pyotp.TOTP(ZERODHA_TOTP_SECRET).now()`
   - POST `/api/twofa` with the TOTP code → session is authenticated
   - GET the OAuth connect URL → capture `request_token` from the redirect `Location` header
   - Exchange `request_token` + API secret for the `access_token` via `generate_session()`
4. The new token is written to `data/kite_token.json` with its expiry timestamp.

The same `requests.Session` is reused across all three steps so Zerodha's session cookies
persist — this is required for the TOTP step to work.

### Price Feed (`kite_feed.py`)

`KitePriceFeed` maintains a live `{NSE:SYMBOL → last_price}` dict updated by the WebSocket:

**Startup:**
1. `_resolve_tokens()` calls `kite.ltp(all_symbols)` — this returns both initial prices and
   integer `instrument_token` values for each symbol (Kite's WebSocket subscribes by token,
   not by name). The initial prices also seed the cache.
2. `_start_websocket()` opens a `KiteTicker` connection in **MODE_LTP** (lightest mode —
   just last traded price). The connection runs in a daemon thread with `reconnect=True`.

**During market hours:**
- Each incoming tick calls `_handle_ticks()`, which updates `_price_cache` and
  `_last_tick_time` under a lock.
- `get_price(yf_symbol)` reads from cache if the entry is fresh (< 60 seconds old).
- If stale, it falls back to a REST `kite.ltp()` call.
- If that also fails, it returns `None` — `_fetch_one()` in `main.py` then tries yfinance.

**Symbol mapping:** yfinance uses `STLTECH.NS`; Kite uses `NSE:STLTECH`. The helper
`_to_kite_symbol()` handles this with `removesuffix(".NS")`.

**Daily re-auth:** At market open each day, `main.py` calls `get_kite_client()` to get a
fresh token, then calls `_kite_feed.refresh_subscriptions([...], kite=new_kite)`. This
swaps in the new `KiteConnect` object with the fresh token, stops the old WebSocket, and
reconnects — all transparently.

### Fallback Chain in `_fetch_one()`

```
1. _kite_feed.get_price(yf_symbol)
   ├── cache hit (< 60s)  → return price immediately
   ├── stale              → kite.ltp() REST call → return price
   └── REST failure       → return None
2. yf.Ticker(yf_symbol).fast_info.last_price  (yfinance, ~15 min delay)
3. yf.Ticker(yf_symbol).history(...)          (last 1-min candle)
4. return None                                (logged as warning)
```

Steps 1 and 2 are independent — a `fast_info` failure does not skip the `history()`
attempt.

---

## Poll Interval Behaviour

| State | Interval | Why |
|-------|----------|-----|
| Kite WebSocket connected | `POLL_INTERVAL_SECONDS` (default 10s) | Cache is live; frequent checks are free |
| Kite unavailable / yfinance fallback | `POLL_INTERVAL_FALLBACK_SECONDS` (default 180s) | yfinance free tier rate-limits on high frequency |

The interval is selected dynamically on every loop iteration based on whether `_kite_feed`
is set.

---

## Graceful Degradation

The integration is designed so that **no alert is ever lost** due to Kite being unavailable:

- If all `ZERODHA_*` vars are absent from `.env`, the bot starts in yfinance-only mode.
  Log line: `Kite credentials not configured — running in yfinance-only mode`
- If login fails at startup (wrong credentials, network error), `_kite_feed` is set to
  `None` and the bot falls back to yfinance. A warning is logged but no crash.
- If the WebSocket drops mid-session, `KiteTicker` auto-reconnects (`reconnect=True`).
  During the gap, `get_price()` falls back to REST `ltp()`, and then to yfinance on REST
  failure.
- If daily re-auth fails at market open, the bot continues on yfinance for that day and
  retries the next morning.

---

## Known Gaps / Follow-up Work

- **Hot-reload subscription sync:** When `stocks/*.md` changes trigger a hot-reload
  mid-day, `_kite_feed` is not told about new/removed tickers. They'll be fetched via
  yfinance until the next market-open re-auth. Fix: call `_kite_feed.refresh_subscriptions()`
  inside the hot-reload block in `main.py`.

- **BSE tickers:** `_to_kite_symbol()` assumes NSE (`.NS` suffix). `STLTECH.BO` would
  produce `NSE:STLTECH.BO` — an invalid Kite symbol. No BSE tickers are currently in the
  watchlist, but guard if that changes.

- **Feed health logging:** If `refresh_subscriptions()` partially fails (e.g., `_resolve_tokens()`
  network error after `_kite` is already swapped), the feed is left in a degraded state
  with a new client but stale tokens. Prices silently fall through to yfinance. A health-check
  log line on every poll would surface this faster.

---

## Tests

All tests live in `tests/kite/`:

| File | What it tests |
|------|---------------|
| `test_kite_auth.py` | Token persistence, expiry math, TOTP login flow, `get_kite_client()` |
| `test_kite_feed.py` | Symbol mapping, token resolution, tick handling, staleness, WebSocket teardown, `on_connect` callback |
| `test_main_kite.py` | `_fetch_one()` fallback chain — Kite hit, Kite miss, Kite unconfigured |

Run with: `pytest tests/kite/ -v`
