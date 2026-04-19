# Backpack Neutral Grid Backtester

Web UI for backtesting a **market-neutral grid bot** strategy on Backpack Exchange PERP markets.

Mirrors the mechanics of Backpack's native grid bot: when a BUY fills, a SELL is placed one level above to close with `+step` profit; when a SELL fills, a BUY is placed one level below. Each mean-reversion cycle books the grid step as realized profit, regardless of direction.

## Features

- **Market-neutral PERP grid** with signed positions (long or short)
- Automatic re-order placement after each fill (mirror logic)
- Fetches real historical klines, funding rates, and market filters via public Backpack API — **no API keys needed**
- Configurable min/max price, level count, leverage
- Separate maker / taker fees with probabilistic taker fills (per Backpack FAQ)
- Funding P&L on open position
- Stop-Loss / Take-Profit as % of starting equity (matches Backpack's bot semantics)
- Optional "Close position on stop" toggle
- Custom date range OR rolling window ("last N days")
- Interactive charts: equity curve, position over time (neutrality check), price + fills
- CSV export for trades, equity, and position history

## Quickstart

```bash
pip install -r requirements.txt
streamlit run app.py
```

Opens at `http://localhost:8501`.

## Deploy on Streamlit Community Cloud (free)

1. Push these files to a public GitHub repo:
   - `app.py`
   - `backtest_core.py`
   - `requirements.txt`
2. Go to <https://share.streamlit.io> and sign in with GitHub.
3. Click **New app**, pick your repo, set main file to `app.py`, click **Deploy**.
4. You'll get a public URL like `your-app-name.streamlit.app` within a minute.

**Free tier limits:**
- 1 GB RAM — fine for up to ~200k candles
- App sleeps after ~7 days of inactivity (wakes up in ~30 sec)
- Built-in 10-minute cache handles shared-IP rate limits

## Deploy on Railway / Render (paid, no sleep)

Same repo works. On Railway, add a start command:

```
streamlit run app.py --server.port $PORT --server.address 0.0.0.0
```

## Project structure

```
app.py              # Streamlit UI
backtest_core.py    # Pure backtest logic, importable
requirements.txt    # pip dependencies
```

## How the neutral grid works

**Starting state** (position = 0, all capital is margin):
- Levels **below** current price → `BUY_OPEN` resting orders (will open long on a dip)
- Levels **above** current price → `SELL_OPEN` resting orders (will open short on a rally)

**Price drops through a BUY_OPEN level:**
1. Buy fills → position goes **long** by `qty`
2. A `SELL_CLOSE` order is placed **one level above** at the entry — will close this long with `+step × qty` profit when price recovers

**Price rises through a SELL_OPEN level:**
1. Sell fills → position goes **short** by `qty`
2. A `BUY_CLOSE` is placed **one level below** at the entry — closes this short with `+step × qty` on a dip

**Why "neutral":** every open leg always has a pending close order `step` away. If price oscillates in the grid range, each cycle books realized profit regardless of direction. The position drifts only when price trends strongly, accumulating inventory on the side the market is moving away from.

## When the strategy loses money

- **Strong trend out of the range** — one side accumulates positions, the other stays empty. Unrealized losses grow until price returns (or SL triggers).
- **Price leaves the range entirely** — no more fills, position just marks to market.
- **High funding on the wrong side** — if net position is on the losing side of funding, it bleeds equity over time.
- **Tight grid + high fees** — if `step × qty < 2 × fee × price × qty`, each cycle loses money.

## Notes on accuracy

The backtest uses a **kline-based fill model**: if a grid level falls within `[low, high]` of a candle, the order is considered filled at the level price. This tends to overstate PnL by 5–20% vs tick-level simulation in fast markets. Treat results as an **upper bound**, not a guarantee.

Taker fills are modelled probabilistically — per Backpack's FAQ, resting limit orders may execute as taker during rapid price moves or tight spacing. Default 5% taker rate is a reasonable estimate for moderately liquid markets; adjust in the sidebar.

Position is marked-to-market using the candle `close` — close enough for daily attribution, may under/overstate tick-level equity swings.

## Limitations

- Only PERP markets are supported (the neutral logic requires shorting).
- Arithmetic grid only — geometric spacing not implemented.
- No slippage model for the principal part of the trade — fills happen at the level price exactly. Only fees account for spread.
- Historical depth is bounded by when each market was listed on Backpack (SOL/BTC/ETH PERP: roughly mid-2024 onwards).

## License

MIT. No affiliation with Backpack Exchange.