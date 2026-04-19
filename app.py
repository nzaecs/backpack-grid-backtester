"""
Streamlit UI — Backpack Neutral Perp Grid Backtester.

Run locally:
    pip install -r requirements.txt
    streamlit run app.py
"""

import time
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from backtest_core import (
    build_grid_levels,
    fetch_available_symbols,
    fetch_funding_rates,
    fetch_klines,
    fetch_market_filters,
    simulate_neutral_perp,
    size_qty_for_neutral_grid,
)

st.set_page_config(
    page_title="Backpack Neutral Grid Backtester",
    page_icon="📊",
    layout="wide",
)

st.title("📊 Backpack Neutral Grid Backtester")
st.caption(
    "Market-neutral PERP grid: when a BUY fills, a SELL is placed one level above "
    "to close with +step profit; when a SELL fills, a BUY is placed one level below. "
    "Each mean-reversion cycle books the grid step as realized profit."
)

# ========== CACHED LOADERS ==========
@st.cache_data(ttl=3600, show_spinner=False)
def load_symbols():
    return fetch_available_symbols()

@st.cache_data(ttl=600, show_spinner=False)
def load_klines(symbol, interval, start_ts, end_ts):
    return fetch_klines(symbol, interval, start_ts, end_ts)

@st.cache_data(ttl=600, show_spinner=False)
def load_funding(symbol, start_ts, end_ts):
    return fetch_funding_rates(symbol, start_ts, end_ts)

@st.cache_data(ttl=3600, show_spinner=False)
def load_filters(symbol):
    return fetch_market_filters(symbol)

@st.cache_data(ttl=60, show_spinner=False)
def get_current_price(sym):
    import requests
    r = requests.get(
        "https://api.backpack.exchange/api/v1/ticker",
        params={"symbol": sym}, timeout=10,
    )
    r.raise_for_status()
    return float(r.json()["lastPrice"])


# ========== SIDEBAR ==========
with st.sidebar:
    st.header("⚙️ Parameters")

    try:
        symbols = load_symbols()
        perp_symbols = [s for s in symbols if s.endswith("_PERP")]
        default_idx = perp_symbols.index("SOL_USDC_PERP") if "SOL_USDC_PERP" in perp_symbols else 0
    except Exception as e:
        st.error(f"Failed to load symbols: {e}")
        perp_symbols = ["SOL_USDC_PERP", "BTC_USDC_PERP", "ETH_USDC_PERP"]
        default_idx = 0

    symbol = st.selectbox("Market (PERP only)", perp_symbols, index=default_idx)

    interval = st.selectbox(
        "Candle interval",
        ["1m", "3m", "5m", "15m", "30m", "1h", "4h"],
        index=0,
    )

    # Period: two modes — rolling window ("last N days") or explicit date range
    period_mode = st.radio(
        "Period",
        ["Last N days", "Custom range"],
        horizontal=True,
        help="Use 'Custom range' to backtest a specific historical window, "
             "e.g. a known ranging period where a neutral grid should shine.",
    )

    if period_mode == "Last N days":
        days = st.slider("Days", 1, 365, 30)
        custom_start = None
        custom_end = None
    else:
        from datetime import datetime, timedelta, timezone
        today_utc = datetime.now(timezone.utc).date()
        default_start = today_utc - timedelta(days=30)
        col_a, col_b = st.columns(2)
        with col_a:
            custom_start = st.date_input(
                "Start date (UTC)",
                value=default_start,
                max_value=today_utc,
            )
        with col_b:
            custom_end = st.date_input(
                "End date (UTC)",
                value=today_utc,
                max_value=today_utc,
            )
        if custom_start >= custom_end:
            st.error("Start date must be before end date")
        days = (custom_end - custom_start).days  # used only for display

        # Warn if the window is unreasonably long for fine intervals
        candles_per_day = {
            "1m": 1440, "3m": 480, "5m": 288, "15m": 96,
            "30m": 48, "1h": 24, "4h": 6,
        }
        est_candles = days * candles_per_day.get(interval, 1440)
        if est_candles > 100_000:
            st.warning(
                f"⚠️ ~{est_candles:,} candles to fetch — this may take a while "
                f"and hit rate limits. Consider a coarser interval."
            )

    st.divider()
    st.subheader("Grid")

    try:
        current_price = get_current_price(symbol)
        st.caption(f"💡 Current {symbol} price: **{current_price:.4f}**")
    except Exception:
        current_price = 100.0
        st.caption("⚠️ Could not fetch current price")

    lower_price = st.number_input(
        "Min price (lower bound)",
        min_value=0.0,
        value=float(round(current_price * 0.90, 4)),
        step=float(max(current_price * 0.001, 0.0001)),
        format="%.4f",
    )
    upper_price = st.number_input(
        "Max price (upper bound)",
        min_value=0.0,
        value=float(round(current_price * 1.10, 4)),
        step=float(max(current_price * 0.001, 0.0001)),
        format="%.4f",
    )
    if lower_price >= upper_price:
        st.error("Min must be < Max")

    n_levels_total = st.slider(
        "Total grid levels", 5, 200, 50,
        help="Number of price levels in the grid. Roughly half will be buy-open "
             "(below start price) and half sell-open (above).",
    )

    initial_capital = st.number_input(
        "Initial capital (USDC)", min_value=100, value=10_000, step=500,
    )
    leverage = st.slider(
        "Max leverage", 1.0, 10.0, 1.0, step=0.5,
        help="Sizing: worst-case exposure (all orders on one side filled) will "
             "equal capital × leverage. Leverage=1 means no borrowing — max "
             "notional = your cash. Higher leverage = larger qty per order = "
             "more profit per cycle but more liquidation risk.",
    )

    st.divider()
    st.subheader("Fees")
    maker_fee_bps = st.number_input("Maker fee (bps)", min_value=0.0, value=2.0, step=0.5)
    taker_fee_bps = st.number_input("Taker fee (bps)", min_value=0.0, value=5.0, step=0.5)
    taker_probability_pct = st.slider(
        "Taker fill rate (%)", 0, 50, 5,
        help="Fraction of grid fills that execute as taker (per Backpack FAQ).",
    )

    st.divider()
    st.subheader("Risk management")
    use_sl = st.checkbox("Enable Stop-Loss", value=True)
    stop_loss_pct = st.slider(
        "Stop-Loss (% of starting equity)", 1, 50, 10, disabled=not use_sl,
    ) / 100 if use_sl else None

    use_tp = st.checkbox("Enable Take-Profit", value=True)
    take_profit_pct = st.slider(
        "Take-Profit (% of starting equity)", 1, 100, 20, disabled=not use_tp,
    ) / 100 if use_tp else None

    close_on_stop = st.checkbox("Close position on stop", value=True)

    run_button = st.button("🚀 Run backtest", type="primary", use_container_width=True)


# ========== INFO PANEL ==========
if not run_button:
    st.info("👈 Configure parameters in the sidebar and click **Run backtest**.")
    with st.expander("ℹ️ How neutral grid works"):
        st.markdown("""
        ### Starting state
        On bot start, orders are placed at every grid level:
        - Levels **below** current price → **BUY_OPEN** (will open a long position)
        - Levels **above** current price → **SELL_OPEN** (will open a short position)
        - Starting position is **zero** — all capital is margin.

        ### When price moves
        **Price drops** through a BUY_OPEN level:
        1. The buy fills → position goes **long** by `qty`
        2. A **SELL_CLOSE** is placed one level above — it will close this long with `+step` profit when price recovers

        **Price rises** through a SELL_OPEN level:
        1. The sell fills → position goes **short** by `qty`
        2. A **BUY_CLOSE** is placed one level below — closes this short with `+step` profit on a dip

        ### Why "neutral"?
        Every open leg has a close order `step` away — so the position always
        wants to revert. If price oscillates in the grid range, each cycle books
        `step × qty` as realized profit regardless of direction.

        ### When it loses money
        - **Strong trend** out of the range — one side accumulates positions, the
          other stays empty. Unrealized losses grow until price returns (or SL hits).
        - **Price leaves the range** — no more fills, position just marks to market.
        - **High funding** — if you're on the wrong side of funding for the net
          position, it bleeds equity over time.
        """)
    st.stop()


# ========== RUN ==========
if period_mode == "Custom range":
    from datetime import datetime, time as dtime, timezone
    # Interpret dates as full-day UTC windows: [start 00:00, end 23:59:59]
    start_ts = int(datetime.combine(custom_start, dtime.min, tzinfo=timezone.utc).timestamp())
    end_ts = int(datetime.combine(custom_end, dtime.max, tzinfo=timezone.utc).timestamp())
else:
    end_ts = int(time.time())
    start_ts = end_ts - days * 24 * 3600

status = st.status("Running backtest…", expanded=True)

try:
    with status:
        st.write(f"📥 Fetching {interval} klines for **{symbol}**…")
        df = load_klines(symbol, interval, start_ts, end_ts)
        st.write(f"   Got {len(df):,} candles ({df['start'].min()} → {df['start'].max()})")

        st.write(f"📥 Fetching funding rates…")
        funding_df = load_funding(symbol, start_ts, end_ts)
        if funding_df.empty:
            st.write("   No funding data")
        else:
            total_rate = funding_df["fundingRate"].sum() * 100
            st.write(f"   Got {len(funding_df)} intervals, total rate: {total_rate:+.4f}%")

        st.write("📥 Fetching market filters…")
        filters = load_filters(symbol)
        st.write(f"   tickSize={filters['tickSize']}, stepSize={filters['stepSize']}")

        start_price = df["open"].iloc[0]

        if not (lower_price < start_price < upper_price):
            st.error(
                f"Start price {start_price:.4f} is outside the grid bounds "
                f"[{lower_price:.4f}, {upper_price:.4f}]. Adjust your range so "
                f"it covers the starting price."
            )
            st.stop()

        st.write(f"🔧 Building grid: {lower_price:.4f} … {upper_price:.4f}")
        levels = build_grid_levels(lower_price, upper_price, n_levels_total, filters)
        qty = size_qty_for_neutral_grid(levels, start_price, initial_capital, leverage, filters)
        step = (upper_price - lower_price) / (n_levels_total - 1)
        n_buy = int((levels < start_price).sum())
        n_sell = int((levels > start_price).sum())
        st.write(
            f"   {len(levels)} levels | step ≈ {step:.4f} | qty/order: {qty} | "
            f"buy-open: {n_buy} | sell-open: {n_sell}"
        )

        st.write("⚡ Simulating…")
        result = simulate_neutral_perp(
            df, funding_df, levels,
            start_price=start_price,
            qty_per_order=qty,
            maker_fee=maker_fee_bps / 10000,
            taker_fee=taker_fee_bps / 10000,
            initial_capital=initial_capital,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            close_on_stop=close_on_stop,
            taker_probability=taker_probability_pct / 100,
        )
    status.update(label="✅ Backtest complete", state="complete", expanded=False)

except ValueError as e:
    status.update(label="❌ Config error", state="error")
    st.error(str(e))
    st.stop()
except Exception as e:
    status.update(label="❌ Failed", state="error")
    st.error(f"Error: {e}")
    st.stop()


# ========== REPORT ==========
start_price = df["open"].iloc[0]
last_price = result.last_price

# Equity for neutral perp: quote_balance (all realized PnL baked in) + unrealized on open legs
equity = result.final_quote_balance + result.final_unrealized_pnl
total_pnl = equity - initial_capital
pnl_pct = total_pnl / initial_capital * 100
buy_hold_pnl = (last_price / start_price - 1) * initial_capital

reason_config = {
    "STOP_LOSS":     ("🛑 Grid closed by STOP-LOSS", "error"),
    "TAKE_PROFIT":   ("🎯 Grid closed by TAKE-PROFIT", "success"),
    "END_OF_PERIOD": ("⏱️ Reached end of period — grid still open", "info"),
}
msg, kind = reason_config.get(result.stop_reason, (result.stop_reason, "info"))
getattr(st, kind)(f"**{msg}** — at {result.stop_time}")

col1, col2, col3, col4 = st.columns(4)
col1.metric("Total PnL", f"{total_pnl:+,.2f} USDC", f"{pnl_pct:+.2f}%")
col2.metric("Buy & Hold", f"{buy_hold_pnl:+,.2f} USDC",
            f"{buy_hold_pnl/initial_capital*100:+.2f}%")
col3.metric("Alpha vs B&H", f"{total_pnl - buy_hold_pnl:+,.2f} USDC")
grid_trade_count = sum(1 for t in result.trades if t[4] == "GRID")
col4.metric("Grid fills", f"{grid_trade_count}")

st.subheader("📋 Breakdown")
col1, col2 = st.columns(2)
with col1:
    st.markdown("**Price action**")
    st.write(f"- Start price: `{start_price:.4f}`")
    st.write(f"- Exit price: `{last_price:.4f}` ({(last_price/start_price - 1)*100:+.2f}%)")
    if period_mode == "Custom range":
        st.write(f"- Period: `{custom_start}` → `{custom_end}` "
                 f"({days} days, {interval} candles)")
    else:
        st.write(f"- Period: last {days} days ({interval} candles)")
    st.markdown("**Costs**")
    st.write(f"- Maker fees: `{result.maker_fees_paid:.2f}` USDC")
    st.write(f"- Taker fees: `{result.taker_fees_paid:.2f}` USDC")
    side = "paid" if result.funding_paid > 0 else "received"
    st.write(f"- Funding: `{result.funding_paid:+.2f}` USDC ({side})")

with col2:
    st.markdown("**PnL components**")
    st.write(f"- Realized PnL: `{result.realized_pnl_usdc:+.2f}` USDC")
    st.write(f"- Unrealized PnL on open legs: `{result.final_unrealized_pnl:+.2f}` USDC")
    st.write(f"- Quote balance: `{result.final_quote_balance:+.2f}` USDC")
    st.write(f"- **Equity** = quote + unrealized = `{equity:+.2f}` USDC")
    st.write(f"- **Total PnL** = equity − start = **`{total_pnl:+.2f}`** USDC")

    st.markdown("**Exposure**")
    st.write(f"- Final position: `{result.final_position:+.4f}` "
             f"({'long' if result.final_position > 0 else ('short' if result.final_position < 0 else 'flat')})")
    st.write(f"- Max long reached: `{result.max_long_exposure:+.4f}` "
             f"(≈ {result.max_long_exposure * start_price:+.2f} USDC notional)")
    st.write(f"- Max short reached: `{result.max_short_exposure:+.4f}` "
             f"(≈ {result.max_short_exposure * start_price:+.2f} USDC notional)")

# ========== CHARTS ==========
st.subheader("📈 Equity curve")
equity_df = pd.DataFrame(result.equity_curve, columns=["time", "pnl_usdc"])
fig = go.Figure()
fig.add_trace(go.Scatter(x=equity_df["time"], y=equity_df["pnl_usdc"],
                          mode="lines", name="PnL", line=dict(color="#2E86AB", width=2)))
fig.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5)
if stop_loss_pct:
    fig.add_hline(y=-stop_loss_pct * initial_capital, line_dash="dot",
                   line_color="red", opacity=0.5,
                   annotation_text="SL", annotation_position="right")
if take_profit_pct:
    fig.add_hline(y=take_profit_pct * initial_capital, line_dash="dot",
                   line_color="green", opacity=0.5,
                   annotation_text="TP", annotation_position="right")
fig.update_layout(height=400, margin=dict(l=0, r=0, t=10, b=0),
                   xaxis_title="Time", yaxis_title="PnL (USDC)", hovermode="x unified")
st.plotly_chart(fig, use_container_width=True)

st.subheader("📊 Position over time (neutrality check)")
pos_df = pd.DataFrame(result.position_curve, columns=["time", "position"])
pfig = go.Figure()
pfig.add_trace(go.Scatter(x=pos_df["time"], y=pos_df["position"],
                           mode="lines", name="Position", line=dict(color="#E63946", width=1.5)))
pfig.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5)
pfig.update_layout(height=300, margin=dict(l=0, r=0, t=10, b=0),
                    xaxis_title="Time", yaxis_title=f"Position ({symbol.split('_')[0]})",
                    hovermode="x unified")
st.caption("💡 Perfectly neutral grid keeps position oscillating around zero. "
            "A drifting position = directional exposure accumulating.")
st.plotly_chart(pfig, use_container_width=True)

st.subheader("💹 Price and fills")
price_fig = go.Figure()
price_fig.add_trace(go.Scatter(x=df["start"], y=df["close"], mode="lines",
                                name="Close", line=dict(color="#888", width=1)))
trades_df = pd.DataFrame(result.trades, columns=["time", "side", "price", "qty", "source"])
if not trades_df.empty:
    colors = {"BUY_OPEN": "green", "SELL_CLOSE": "lightgreen",
              "SELL_OPEN": "red", "BUY_CLOSE": "pink"}
    for side, color in colors.items():
        sub = trades_df[trades_df["side"] == side]
        if not sub.empty:
            price_fig.add_trace(go.Scatter(
                x=sub["time"], y=sub["price"], mode="markers", name=side,
                marker=dict(color=color, size=5,
                            symbol="triangle-up" if "BUY" in side else "triangle-down"),
            ))
price_fig.add_hline(y=levels[0], line_dash="dot", line_color="blue", opacity=0.3)
price_fig.add_hline(y=levels[-1], line_dash="dot", line_color="blue", opacity=0.3)
price_fig.update_layout(height=450, margin=dict(l=0, r=0, t=10, b=0),
                         xaxis_title="Time", yaxis_title="Price", hovermode="x unified")
st.plotly_chart(price_fig, use_container_width=True)

# ========== EXPORTS ==========
st.subheader("📥 Raw data")
tab1, tab2, tab3 = st.tabs([f"Trades ({len(trades_df)})",
                            f"Equity ({len(equity_df)})",
                            f"Position ({len(pos_df)})"])
with tab1:
    st.dataframe(trades_df, use_container_width=True, height=300)
    st.download_button("⬇️ Trades CSV", trades_df.to_csv(index=False).encode(),
                        file_name=f"trades_{symbol}_{days}d.csv", mime="text/csv")
with tab2:
    st.dataframe(equity_df, use_container_width=True, height=300)
    st.download_button("⬇️ Equity CSV", equity_df.to_csv(index=False).encode(),
                        file_name=f"equity_{symbol}_{days}d.csv", mime="text/csv")
with tab3:
    st.dataframe(pos_df, use_container_width=True, height=300)
    st.download_button("⬇️ Position CSV", pos_df.to_csv(index=False).encode(),
                        file_name=f"position_{symbol}_{days}d.csv", mime="text/csv")