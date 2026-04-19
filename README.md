## ⚠️ Disclaimer

This tool is for **educational and research purposes only**. It is not financial advice, not a trading recommendation, and not a prediction of future results.

Backtest results are based on historical data and simplifying assumptions (kline-level fills, probabilistic taker rate, no real order book depth). **Past performance does not guarantee future performance.** Live trading outcomes will differ — often significantly — from backtest results due to slippage, partial fills, API latency, funding rate fluctuations, and market conditions that did not occur in the historical sample.

Cryptocurrency trading, especially with leverage on perpetual futures, carries a substantial risk of loss, including loss of your entire invested capital. Do your own research, understand the mechanics of grid bots, and never risk funds you can't afford to lose.

The author assumes no responsibility for any losses or damages resulting from the use of this software. Use at your own risk.

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


## Project structure

```
app.py              # Streamlit UI
backtest_core.py    # Pure backtest logic, importable
requirements.txt    # pip dependencies
```

## Notes on accuracy

The backtest uses a **kline-based fill model**: if a grid level falls within `[low, high]` of a candle, the order is considered filled at the level price. This tends to overstate PnL by 5–20% vs tick-level simulation in fast markets. Treat results as an **upper bound**, not a guarantee.

Position is marked-to-market using the candle `close` — close enough for daily attribution, may under/overstate tick-level equity swings.

## Limitations

- Only PERP markets are supported (the neutral logic requires shorting).
- Arithmetic grid only — geometric spacing not implemented.
- No slippage model for the principal part of the trade — fills happen at the level price exactly. Only fees account for spread.
- Historical depth is bounded by when each market was listed on Backpack (SOL/BTC/ETH PERP: roughly mid-2024 onwards).

## License

MIT. No affiliation with Backpack Exchange.
