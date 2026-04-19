"""
Backpack Exchange — Grid Bot Backtester (core module)
=====================================================

Two grid modes:

1. SPOT-STYLE (long-biased)
   Classic grid: buys below start price, sells above. Pre-buys base inventory
   at start to back the sell orders. Can only hold long or zero — never short.
   Good for markets you expect to range or rise.

2. NEUTRAL-PERP (market-neutral)
   PERP grid with signed positions. No pre-buy — all capital is margin.
   When a BUY fills → position goes long → a SELL order is placed one step
   ABOVE it (to close with +step profit). When a SELL fills → position goes
   short → a BUY is placed one step BELOW. This keeps exposure small and
   symmetric: profits are booked on every mean-reversion, regardless of
   direction.

Both modes share the fee/funding/SL/TP/data-fetching infrastructure.
"""

import time
import random
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

BASE_URL = "https://api.backpack.exchange"


# ========== DATA FETCHING ==========
def fetch_klines(symbol: str, interval: str, start_ts: int, end_ts: int) -> pd.DataFrame:
    # Size chunks so ~1000 candles per request — stays under Backpack's cap.
    interval_seconds = {
        "1s":   1,     "1m":   60,    "3m":   180,   "5m":   300,
        "15m":  900,   "30m":  1800,  "1h":   3600,  "2h":   7200,
        "4h":   14400, "6h":   21600, "8h":   28800, "12h":  43200,
        "1d":   86400, "3d":   259200, "1w":   604800, "1month": 2592000,
    }.get(interval, 60)
    chunk_seconds = interval_seconds * 1000

    url = f"{BASE_URL}/api/v1/klines"
    all_rows: List[dict] = []
    cursor = start_ts

    while cursor < end_ts:
        chunk_end = min(cursor + chunk_seconds, end_ts)
        params = {"symbol": symbol, "interval": interval,
                  "startTime": cursor, "endTime": chunk_end}
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        if not data:
            cursor = chunk_end
            continue
        all_rows.extend(data)
        cursor = chunk_end
        time.sleep(0.05)

    if not all_rows:
        raise RuntimeError("Empty response from /klines.")

    df = pd.DataFrame(all_rows)
    for col in ("open", "high", "low", "close", "volume", "quoteVolume"):
        df[col] = pd.to_numeric(df[col])
    df["start"] = pd.to_datetime(df["start"])
    df = df.sort_values("start").drop_duplicates("start").reset_index(drop=True)
    return df


def fetch_market_filters(symbol: str) -> dict:
    r = requests.get(f"{BASE_URL}/api/v1/markets", timeout=30)
    r.raise_for_status()
    for m in r.json():
        if m["symbol"] == symbol:
            return {
                "tickSize": float(m["filters"]["price"]["tickSize"]),
                "stepSize": float(m["filters"]["quantity"]["stepSize"]),
                "minQuantity": float(m["filters"]["quantity"]["minQuantity"]),
            }
    raise RuntimeError(f"Symbol {symbol} not found in /markets")


def fetch_available_symbols() -> List[str]:
    r = requests.get(f"{BASE_URL}/api/v1/markets", timeout=30)
    r.raise_for_status()
    symbols = [m["symbol"] for m in r.json() if m.get("orderBookState") == "Open"]
    return sorted(symbols)


def fetch_funding_rates(symbol: str, start_ts: int, end_ts: int) -> pd.DataFrame:
    url = f"{BASE_URL}/api/v1/fundingRates"
    all_rows: List[dict] = []
    offset, limit = 0, 1000
    while True:
        params = {"symbol": symbol, "limit": limit, "offset": offset}
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
        except requests.HTTPError:
            return pd.DataFrame(columns=["timestamp", "fundingRate"])
        batch = r.json()
        if not batch:
            break
        all_rows.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
        time.sleep(0.05)

    if not all_rows:
        return pd.DataFrame(columns=["timestamp", "fundingRate"])

    df = pd.DataFrame(all_rows)
    df["timestamp"] = pd.to_datetime(df["intervalEndTimestamp"])
    df["fundingRate"] = pd.to_numeric(df["fundingRate"])
    df = df[["timestamp", "fundingRate"]].sort_values("timestamp").reset_index(drop=True)
    start_dt = pd.to_datetime(start_ts, unit="s", utc=True).tz_localize(None)
    end_dt = pd.to_datetime(end_ts, unit="s", utc=True).tz_localize(None)
    df = df[(df["timestamp"] >= start_dt) & (df["timestamp"] <= end_dt)].reset_index(drop=True)
    return df


def round_to_step(value: float, step: float) -> float:
    return round(round(value / step) * step, 10)


# ========== RESULT CONTAINER ==========
@dataclass
class SimResult:
    trades: List[Tuple[pd.Timestamp, str, float, float, str]] = field(default_factory=list)
    realized_pnl_usdc: float = 0.0
    maker_fees_paid: float = 0.0
    taker_fees_paid: float = 0.0
    funding_paid: float = 0.0
    final_position: float = 0.0           # signed for perp-neutral, positive for spot
    final_quote_balance: float = 0.0
    final_unrealized_pnl: float = 0.0     # mark-to-market PnL on open legs
    last_price: float = 0.0
    stop_reason: str = "END_OF_PERIOD"
    stop_time: Optional[pd.Timestamp] = None
    equity_curve: List[Tuple[pd.Timestamp, float]] = field(default_factory=list)
    position_curve: List[Tuple[pd.Timestamp, float]] = field(default_factory=list)
    # Diagnostics
    max_long_exposure: float = 0.0        # max positive position reached
    max_short_exposure: float = 0.0       # max negative position reached


# ========== NEUTRAL PERP GRID ==========
def build_grid_levels(
    lower_price: float,
    upper_price: float,
    n_levels_total: int,
    filters: dict,
) -> np.ndarray:
    if lower_price >= upper_price:
        raise ValueError(f"lower_price ({lower_price}) must be < upper_price ({upper_price})")
    if n_levels_total < 2:
        raise ValueError("n_levels_total must be >= 2")
    raw = np.linspace(lower_price, upper_price, n_levels_total)
    levels = np.unique([round_to_step(p, filters["tickSize"]) for p in raw])
    return levels


def simulate_neutral_perp(
    df: pd.DataFrame,
    funding_df: pd.DataFrame,
    levels: np.ndarray,
    start_price: float,
    qty_per_order: float,
    maker_fee: float,
    taker_fee: float,
    initial_capital: float,
    stop_loss_pct: Optional[float],
    take_profit_pct: Optional[float],
    close_on_stop: bool = True,
    taker_probability: float = 0.05,
) -> SimResult:
    """
    Market-neutral PERP grid.

    State per level:
      - 'buy_open'   : buy-limit is resting (will open a long leg)
      - 'sell_open'  : sell-limit is resting (will open a short leg)
      - 'buy_close'  : buy-limit is resting (will close a short leg)
      - 'sell_close' : sell-limit is resting (will close a long leg)
      - 'inactive'   : nothing resting

    Start state:
      - Levels below start_price: buy_open (ready to open long on dip)
      - Levels above start_price: sell_open (ready to open short on rally)

    When a buy_open fires at level L:
      - Position += qty (goes longer)
      - Record buy price as cost basis at level L
      - Level L becomes 'inactive' (nothing immediately above it in buy_open)
      - Level L+1 gets a sell_close order (will close this long with +step profit)

    When a sell_close at L+1 fires:
      - Position -= qty (closes the long leg)
      - Realized profit = (L+1 - L) * qty
      - Level L+1 becomes 'inactive'
      - Level L returns to buy_open (ready for next cycle)

    Symmetric logic for shorts via sell_open / buy_close.
    """
    rng = random.Random(42)

    n = len(levels)
    # Level state: 0=inactive, 1=buy_open, 2=sell_open, 3=buy_close, 4=sell_close
    STATE_INACTIVE = 0
    STATE_BUY_OPEN = 1
    STATE_SELL_OPEN = 2
    STATE_BUY_CLOSE = 3
    STATE_SELL_CLOSE = 4

    level_state = np.full(n, STATE_INACTIVE, dtype=np.int8)
    # Per-level cost basis — entry price of the open leg the close-order will match
    entry_price_at_level: dict = {}

    # Initialize: below start → buy_open; above start → sell_open
    for i, lp in enumerate(levels):
        if lp < start_price:
            level_state[i] = STATE_BUY_OPEN
        elif lp > start_price:
            level_state[i] = STATE_SELL_OPEN
        # equal to start_price → skip (would fill immediately as taker)

    result = SimResult()
    position = 0.0           # signed; + = long, - = short
    quote_balance = float(initial_capital)  # all capital starts as USDC margin

    funding_times = funding_df["timestamp"].values if not funding_df.empty else np.array([])
    funding_rates = funding_df["fundingRate"].values if not funding_df.empty else np.array([])
    funding_idx = 0

    sl_threshold = -stop_loss_pct * initial_capital if stop_loss_pct else None
    tp_threshold = take_profit_pct * initial_capital if take_profit_pct else None

    def apply_fee(price, qty) -> Tuple[float, bool]:
        """Return (fee_amount, is_taker)."""
        is_taker = rng.random() < taker_probability
        rate = taker_fee if is_taker else maker_fee
        fee = price * qty * rate
        return fee, is_taker

    def track_position(pos):
        if pos > result.max_long_exposure:
            result.max_long_exposure = pos
        if pos < result.max_short_exposure:
            result.max_short_exposure = pos

    for _, row in df.iterrows():
        low, high, close, ts = row["low"], row["high"], row["close"], row["start"]

        # Snapshot state at candle open — no level that flips this candle can
        # also fire again on the same candle.
        state_snapshot = level_state.copy()
        in_range = (levels >= low) & (levels <= high)

        # Process BUY-side fills (buy_open = opens long; buy_close = closes short)
        buy_open_hits = np.where((state_snapshot == STATE_BUY_OPEN) & in_range)[0]
        buy_close_hits = np.where((state_snapshot == STATE_BUY_CLOSE) & in_range)[0]
        sell_open_hits = np.where((state_snapshot == STATE_SELL_OPEN) & in_range)[0]
        sell_close_hits = np.where((state_snapshot == STATE_SELL_CLOSE) & in_range)[0]

        # When price drops through the grid, process buy_open from highest to
        # lowest (price touches high levels first). When price rises, process
        # sell_open from lowest to highest. This ordering matters for chain
        # reactions within a single candle.
        for idx in sorted(buy_open_hits, reverse=True):
            price = levels[idx]
            fee, is_taker = apply_fee(price, qty_per_order)
            quote_balance -= fee  # only fee; perp doesn't charge notional upfront
            position += qty_per_order
            if is_taker:
                result.taker_fees_paid += fee
            else:
                result.maker_fees_paid += fee
            entry_price_at_level[int(idx)] = price
            level_state[idx] = STATE_INACTIVE
            # Place a sell_close one level up to close this long with +step profit
            if idx + 1 < n:
                level_state[idx + 1] = STATE_SELL_CLOSE
                entry_price_at_level[int(idx + 1)] = price  # remember entry
            result.trades.append((ts, "BUY_OPEN", price, qty_per_order, "GRID"))

        for idx in sorted(sell_close_hits):
            price = levels[idx]
            if position < qty_per_order - 1e-12:
                # Not enough long to close — order stays resting.
                # This happens if a chain of sell_close would take us negative.
                continue
            fee, is_taker = apply_fee(price, qty_per_order)
            quote_balance -= fee
            position -= qty_per_order
            if is_taker:
                result.taker_fees_paid += fee
            else:
                result.maker_fees_paid += fee
            entry = entry_price_at_level.pop(int(idx), price - (levels[1] - levels[0]))
            pnl = (price - entry) * qty_per_order
            result.realized_pnl_usdc += pnl
            quote_balance += pnl  # realize PnL into quote
            level_state[idx] = STATE_INACTIVE
            # Return level below to buy_open (ready for next cycle)
            if idx - 1 >= 0:
                level_state[idx - 1] = STATE_BUY_OPEN
            result.trades.append((ts, "SELL_CLOSE", price, qty_per_order, "GRID"))

        for idx in sorted(sell_open_hits):
            price = levels[idx]
            fee, is_taker = apply_fee(price, qty_per_order)
            quote_balance -= fee
            position -= qty_per_order  # going short
            if is_taker:
                result.taker_fees_paid += fee
            else:
                result.maker_fees_paid += fee
            entry_price_at_level[int(idx)] = price
            level_state[idx] = STATE_INACTIVE
            # Place a buy_close one level down
            if idx - 1 >= 0:
                level_state[idx - 1] = STATE_BUY_CLOSE
                entry_price_at_level[int(idx - 1)] = price
            result.trades.append((ts, "SELL_OPEN", price, qty_per_order, "GRID"))

        for idx in sorted(buy_close_hits, reverse=True):
            price = levels[idx]
            if position > -qty_per_order + 1e-12:
                continue  # not enough short to close
            fee, is_taker = apply_fee(price, qty_per_order)
            quote_balance -= fee
            position += qty_per_order
            if is_taker:
                result.taker_fees_paid += fee
            else:
                result.maker_fees_paid += fee
            entry = entry_price_at_level.pop(int(idx), price + (levels[1] - levels[0]))
            pnl = (entry - price) * qty_per_order
            result.realized_pnl_usdc += pnl
            quote_balance += pnl
            level_state[idx] = STATE_INACTIVE
            # Return level above to sell_open (ready for next cycle)
            if idx + 1 < n:
                level_state[idx + 1] = STATE_SELL_OPEN
            result.trades.append((ts, "BUY_CLOSE", price, qty_per_order, "GRID"))

        track_position(position)

        # Funding: applied to notional of open position
        while funding_idx < len(funding_times) and funding_times[funding_idx] <= ts.to_datetime64():
            rate = funding_rates[funding_idx]
            notional = position * close  # signed
            funding_payment = notional * rate
            quote_balance -= funding_payment
            result.funding_paid += funding_payment
            funding_idx += 1

        # Equity = quote + position_unrealized_pnl
        # For PERP: equity = quote_balance + position * close (if we mark-to-mkt)
        # But quote_balance already contains all realized PnL and fees paid.
        # Unrealized = position * (close - avg_entry_of_open_legs)
        # Simpler: current_equity = quote_balance + sum_of_open_legs_unrealized
        unrealized = 0.0
        for idx, entry in entry_price_at_level.items():
            st = level_state[idx]
            # sell_close means open long leg (entry was a buy) → unrealized = (close - entry)*qty
            # buy_close means open short leg (entry was a sell) → unrealized = (entry - close)*qty
            if st == STATE_SELL_CLOSE:
                unrealized += (close - entry) * qty_per_order
            elif st == STATE_BUY_CLOSE:
                unrealized += (entry - close) * qty_per_order
        current_equity = quote_balance + unrealized
        current_pnl = current_equity - initial_capital
        result.equity_curve.append((ts, current_pnl))
        result.position_curve.append((ts, position))

        stop_triggered = None
        if sl_threshold is not None and current_pnl <= sl_threshold:
            stop_triggered = "STOP_LOSS"
        elif tp_threshold is not None and current_pnl >= tp_threshold:
            stop_triggered = "TAKE_PROFIT"

        if stop_triggered:
            if close_on_stop and abs(position) > 1e-12:
                # Flatten position at close with taker fee
                exit_fee = abs(position) * close * taker_fee
                result.taker_fees_paid += exit_fee
                # Realize PnL from all open legs
                for idx, entry in list(entry_price_at_level.items()):
                    st = level_state[idx]
                    if st == STATE_SELL_CLOSE:
                        pnl = (close - entry) * qty_per_order
                    elif st == STATE_BUY_CLOSE:
                        pnl = (entry - close) * qty_per_order
                    else:
                        continue
                    result.realized_pnl_usdc += pnl
                    quote_balance += pnl
                quote_balance -= exit_fee
                side = "SELL_CLOSE" if position > 0 else "BUY_CLOSE"
                result.trades.append((ts, side, close, abs(position), stop_triggered))
                position = 0.0
                entry_price_at_level.clear()
            result.stop_reason = stop_triggered
            result.stop_time = ts
            result.last_price = close
            result.final_position = position
            result.final_quote_balance = quote_balance
            return result

    result.stop_reason = "END_OF_PERIOD"
    result.stop_time = df["start"].iloc[-1]
    last_close = df["close"].iloc[-1]
    result.last_price = last_close
    result.final_position = position
    result.final_quote_balance = quote_balance
    # Compute unrealized PnL of remaining open legs at last close
    unrealized = 0.0
    for idx, entry in entry_price_at_level.items():
        st = level_state[idx]
        if st == STATE_SELL_CLOSE:
            unrealized += (last_close - entry) * qty_per_order
        elif st == STATE_BUY_CLOSE:
            unrealized += (entry - last_close) * qty_per_order
    result.final_unrealized_pnl = unrealized
    return result


def size_qty_for_neutral_grid(
    levels: np.ndarray,
    start_price: float,
    capital: float,
    leverage: float,
    filters: dict,
) -> float:
    """
    Sizing for neutral-perp grid.

    Worst case exposure: price moves to one extreme and fills every open-order
    on that side. max_exposure = n_buy_levels * qty * start_price (for long).
    We size so that worst_case_notional = capital * leverage.
    """
    n_buy = int((levels < start_price).sum())
    n_sell = int((levels > start_price).sum())
    worst_n = max(n_buy, n_sell)
    if worst_n == 0:
        raise ValueError("No buy or sell levels — start_price outside grid bounds")
    max_notional = capital * leverage
    qty = max_notional / (worst_n * start_price)
    qty = round_to_step(qty, filters["stepSize"])
    if qty < filters["minQuantity"]:
        raise ValueError(
            f"qty_per_order={qty} < minQuantity={filters['minQuantity']}. "
            f"Increase capital or reduce levels/leverage."
        )
    return qty