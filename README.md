# Wavelet MRA Haar Bot — BingX / Railway / Telegram

A Python port of the `WMRA-H-5m` Pine Script strategy: a Haar multiresolution
"trend vs. noise" regime filter that gates a price/SMA(8) crossover, with
ATR-based stop-loss/take-profit, an optional trailing stop, and a daily
loss circuit breaker. It can run in three modes — pure Telegram signals,
paper trading, or live orders on BingX — controlled by two environment
variables.

## Read this before you turn on live trading

**Where this strategy came from:** a Twitter/X thread offering a "free"
wavelet trading bot setup that claimed turning \$80 into \$4,900 in 38 days
(71% win rate, Sharpe 2.44) — gated behind liking, following, and DMing a
keyword. That pattern (unverifiable returns + engagement-gated "proof") is
a standard growth-hacking format on trading Twitter, and it's not evidence
the strategy is actually profitable live. Treat those specific numbers as
unverified marketing, not a track record.

**What the code actually is:** a legitimate, well-worn signal-processing
technique — comparing energy at fast vs. slow moving-average scales to
detect trending vs. choppy regimes — wearing wavelet vocabulary. It is
*not* the orthogonal Daubechies wavelet transform the original thread's
video was about. That doesn't make it useless, but it means "wavelets" in
the name isn't doing the heavy lifting you might assume.

**What that means practically:**
- Nothing here is financial advice, and this isn't a system either of us
  has a live track record for. Backtest and paper-trade it yourself on
  the pair and timeframe you actually intend to run before risking money.
- The default config uses 3x leverage; the original script defaulted to
  10x. Leverage multiplies losses as fast as gains, and a 5-minute crypto
  timeframe can move a lot between polls. Only increase leverage once
  you've watched the strategy's real behavior, not the thread's numbers.
- An unattended bot fails differently than a human watching a chart — it
  can hold a leveraged position through an outage, a bad fill, or an
  exchange API hiccup. This code has retries, a kill switch, and explicit
  alerts when something can't be verified, but no amount of error
  handling makes leveraged perpetual futures low-risk.
- Start in signal-only or paper mode. Stay there longer than feels
  necessary.

## The three modes

Controlled entirely by whether `BINGX_API_KEY`/`BINGX_API_SECRET` are set
and what `DRY_RUN` is:

| Mode | API keys set? | `DRY_RUN` | What happens |
|---|---|---|---|
| **Signal-only** | No | (irrelevant) | Fetches real public market data, computes real signals, pushes them to Telegram. Never touches your BingX account. Good for pure manual trading. |
| **Paper** | Yes | `true` (default) | Same as above, but also reads your real balance so position-sizing math is realistic — still never places an order. |
| **Live** | Yes | `false` | Places real market entries and STOP_MARKET/TAKE_PROFIT_MARKET exits on your BingX account. |

The bot refuses to start in a half-configured state (`DRY_RUN=false` with
missing keys raises a config error immediately, rather than silently
falling back to paper mode).

## Project structure

```
wavelet-mra-bot/
├── main.py                  # entrypoint: wires everything, runs the poll loop
├── bot/
│   ├── config.py             # env var loading + validation
│   ├── wavelet.py             # the indicator math (ported from Pine)
│   ├── exchange.py            # ccxt/BingX wrapper: data, orders, SL/TP
│   ├── strategy.py            # decides when to fire a signal / trade
│   ├── risk.py                 # SL/TP calc + daily kill switch
│   ├── telegram_notify.py      # push notifications
│   └── logger.py                # stdout logging setup
├── tests/test_wavelet.py     # unit tests, incl. a no-lookahead check
├── requirements.txt
├── .env.example
├── Procfile / railway.toml   # Railway deployment
└── .python-version
```

## Local setup

```bash
git clone <your-new-repo-url>
cd wavelet-mra-bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env — at minimum add your Telegram token/chat id to see signals

python main.py
```

With no BingX keys set, it runs in signal-only mode immediately — a
reasonable first thing to do before creating any exchange keys at all.

## Setting up BingX API keys

1. BingX → API Management → Create API Key.
2. Enable **Perpetual Futures Trading** on the key. This is the single
   most common setup mistake: a key without this permission authenticates
   fine for reading data and then fails on order placement with an
   authorization error (code 100004).
3. Do **not** enable withdrawal permission on this key — the bot never
   needs it, and leaving it off limits the blast radius if the key ever
   leaks.
4. Optionally restrict the key to your server's IP once you know it
   (Railway's outbound IP — check your service's network settings).
5. Put the key/secret in `.env` locally or in Railway's Variables tab —
   never commit them (`.env` is already in `.gitignore`).

BingX also offers **Demo Trading** (VST — virtual USDT) as an additional,
separate practice account with its own balance, reachable from the normal
BingX UI. It's a good extra layer to sanity-check order behavior with,
independent of this bot's own `DRY_RUN` simulation. Wiring this bot
directly to BingX's demo endpoint isn't included here — the demo
environment isn't a standard sandbox flag, and integrating it well
deserves its own testing rather than an unverified guess. `DRY_RUN=true`
gives you the safety guarantee; BingX's own demo account gives you a
second, independent way to test order mechanics if you want it.

## Setting up Telegram

1. Message **@BotFather** on Telegram → `/newbot` → follow the prompts →
   copy the token into `TELEGRAM_BOT_TOKEN`.
2. Send any message to your new bot.
3. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a
   browser and copy the `"chat":{"id": ...}` number into
   `TELEGRAM_CHAT_ID`. (For a channel instead of a DM: add the bot as
   admin and use the channel's negative chat id.)

## Pushing to GitHub

```bash
cd wavelet-mra-bot
git init
git add .
git commit -m "Initial commit: wavelet MRA Haar bot"
```

Create a new **empty** repo on GitHub (no README/license, so there's no
merge conflict), then:

```bash
git remote add origin https://github.com/<you>/<repo-name>.git
git branch -M main
git push -u origin main
```

Double-check `.env` did **not** get committed (`git status` before your
first commit, or `git log --all --full-history -- .env` after) — it's
covered by `.gitignore`, but it's worth verifying once.

## Deploying to Railway

1. Railway dashboard → **New Project** → **Deploy from GitHub repo** →
   select the repo you just pushed.
2. Open the service → **Variables** tab → add every variable from
   `.env.example` with your real values (keep `DRY_RUN=true` for the
   first deploy).
3. Open **Settings** → confirm the **Start Command** is `python main.py`.
   Railway usually picks this up from the `Procfile`/`railway.toml`
   automatically, but if the deploy log shows it looking for a `web`
   process or failing to start, set the Start Command explicitly there —
   this is a common enough Railway/Python friction point that it's worth
   checking directly rather than assuming.
4. This is a background worker, not a web server — it never binds
   `$PORT`. If Railway's dashboard flags a health-check warning because
   of that, that's expected for this kind of service; you can disable
   health checks for it under Settings if you want the warning gone.
5. Deploy, then check the **Deploy Logs** tab for the "🤖 Bot iniciado"
   Telegram message and matching log line.
6. Once you're satisfied with paper-mode behavior, flip `DRY_RUN` to
   `false` in Variables (this triggers a redeploy) — and watch the first
   few live signals closely.

## Configuration reference

See `.env.example` for the full list with defaults — it's the source of
truth. The ones most worth understanding before going live:

| Variable | What it controls |
|---|---|
| `DRY_RUN` | Master safety switch — `true` never places real orders |
| `SYMBOL` | ccxt unified symbol, e.g. `BTC/USDT:USDT` for USDT-M perpetual |
| `LEVERAGE` | Applied via BingX's set-leverage call before entries |
| `QTY_PCT` | % of account equity used as position notional per trade |
| `MAX_DAILY_LOSS_PCT` | Kill switch: halts new entries after this much drawdown in a UTC day |
| `K_DOMINANCE` | How dominant the slow scale must be over the fast scale to call it "trending" — higher = fewer, more selective signals |
| `COOLDOWN_BARS` | Minimum bars between signals, to avoid re-firing on the same move |
| `USE_ATR_SL` | ATR-based SL/TP (adapts to volatility) vs. fixed percentage |

## How the strategy works (short version)

For each of 4 scales (1, 2, 4, 8 bars), it computes a "detail" value —
the difference between the current N-bar average and the N-bar average
from N bars ago, scaled by `1/sqrt(2)`. Squaring and summing those details
over a lookback window gives an "energy" per scale; scales 1-2 are
labeled "fine" (noise), scales 4-8 are "coarse" (trend). When coarse
energy dominates fine energy by more than `K_DOMINANCE`, the market is
called "trending," and a long/short signal fires on price crossing its
own 8-bar average in the direction the coarse detail agrees with. Full
math is in `bot/wavelet.py`, with a unit test that verifies the
computation is causal (no look-ahead) — a live indicator that repaints on
each new bar is worse than useless.

## Known limitations

- Targets BingX **one-way** position mode. If your account is in hedge
  mode, switch it in BingX's position settings, or the flip-on-reversal
  logic in `exchange.py` will need adjusting.
- The trailing stop is software-managed (the bot recalculates and
  re-places the stop order each poll) rather than an exchange-native
  trailing order — simpler to reason about, but it only moves the stop
  when the bot is actually running and polling.
- Tested against synthetic data and unit tests in this environment; it
  has **not** been run against BingX's live or demo API from here, since
  this sandbox has no network path to `api.bingx.com` or
  `api.telegram.org`. Test both connections yourself in paper mode before
  trusting them.
- One symbol, one position at a time — no portfolio/multi-pair support.

## Extending it

- Two-way Telegram control (`/pause`, `/status`, `/close`) — the notifier
  is deliberately minimal (send-only) so this is a clean add.
- Multi-symbol support — `strategy.py` and `config.py` would need to
  become per-symbol rather than singletons.
- Swap `bot/exchange.py` for a different ccxt exchange id to target a
  different venue with minimal other changes.

---
*Not financial advice. Trading perpetual futures with leverage can lose
more than your initial deposit. You are responsible for your own
configuration, API key security, and trading decisions.*
