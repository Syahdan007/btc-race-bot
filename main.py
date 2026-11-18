"""cbBTC long-only bot for agent-fun/bars-replay/v1 (make.fun btc-race).

The wire protocol is spoken by the vendored kit shim under vendor/; this
file is the strategy, written for this competition.

One lesson from the dev window drives the design: a round trip costs
roughly a quarter of a percent once the 4 bps fill fee, the 2 bps modelled
slippage and the pool's price impact are paid, and one-minute noise is a
few tenths of a percent wide. Bots that trade often, or stop out on
noise-sized distances, bleed to death (the kit's own ema-cross baseline
makes 80 trades and loses $148 on the dev window). So every family below
trades rarely, enters only on a confirmed signal, and keeps its stops
several noise-widths away from the price.

Four entry families share one risk core (FAMILY selects). The shipped
configuration, picked by grading every family on the frozen dev window
and keeping the most robust result, is the trend family with pullback
entries: EMA 30/105, entry on a dip back to the fast EMA inside a
confirmed uptrend, slow-EMA-break exit, and the 45-bar time stop. On the
dev window it makes +$7.64 (+0.76%) with 0.95% max drawdown, where the
kit's ema-cross baseline loses $148 and buy-and-hold loses $32. The other
families remain in the file as the tested alternatives; they graded
worse (momentum best -$1.39, reversion best -$3.34, breakout best
-$15.92) and are switched off by the constants below.

- "trend":     the fast EMA (25) above the slow EMA (120), the slow EMA
               rising, and the gap between them at least SEP_MIN_BPS wide.
               Exit when the close breaks the slow EMA or the gap closes.
- "breakout":  the close breaks the highest close of the last BREAK_N
               bars with the fast EMA above the slow one. Exit under the
               lowest close of the last BREAK_M bars (Donchian channel).
- "reversion": only in a range regime (slow EMA nearly flat): buy a dip
               Z_IN ATRs under the mid EMA, sell when the price recovers
               to Z_OUT ATRs under it, or after MAX_HOLD_REV bars.
- "hybrid":    trend entries in a trend regime, reversion entries in a
               range regime, never both at once.

The shared risk core, whatever the family: a hard stop STOP_PCT under the
entry fill price, a trailing stop TRAIL_PCT under the best close since
entry, a minimum hold before signal exits apply (stops are exempt), a
cooldown after every exit, entries skipped in extreme per-bar volatility,
and an equity brake: at BRAKE_DD drawdown from the equity peak any
position is sold and entries pause until the trend signal fully resets.

Everything is a deterministic function of the bars seen: no clock, no
randomness, no files, no network. Orders decided on one batch fill at the
next bar's open, so signals use closes up to the current bar only.
"""

import os
import sys
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# --- parameters (bars are one minute; the local sweep rewrites these) ---
FAMILY = "trend"        # trend | breakout | reversion | hybrid | momentum
EMA_FAST = 30           # fast EMA period
EMA_SLOW = 105          # slow EMA period
REGIME_EMA = 240        # long regime EMA period (the gate's yardstick)
GATE_MODE = 0           # entries: 0 = ungated, 1 = close above REGIME_EMA,
                        # 2 = REGIME_EMA rising, 3 = both
USE_REGIME_GATE = 0     # legacy alias: 1 acts as GATE_MODE 1 when GATE_MODE is 0
TIME_STOP = 45          # bars after which a non-profitable position is cut (0 = off)
EXIT_MODE = 0           # trend exit: 0 = slow break, 1 = fast cross, 2 = both confirm
ENTRY_MODE = 1          # trend entry: 0 = strength, 1 = pullback to the fast EMA
MOM_BARS = 30           # momentum family: lookback bars
MOM_IN_BPS = 35.0       # momentum entry: lookback return at least this
MOM_OUT_BPS = -10.0     # momentum exit: lookback return falls to this
MOM_BARS2 = 0           # second, longer momentum lookback (0 = off)
MOM_IN2_BPS = 0.0       # its entry threshold when enabled
SLOPE_MIN_ATR = -999.0  # entries need the slow EMA's regime slope at least
                        # this many ATRs (over REGIME_BARS); -999 = off
SLOPE_BARS = 30         # bars over which the slow EMA's slope is measured
REGIME_BARS = 60        # bars over which the range regime is judged
ATR_PERIOD = 30         # Wilder ATR period (true range of the bars)
MID_PERIOD = 60         # mid EMA period for the reversion z-score
SEP_MIN_BPS = 10.0      # trend entry needs the EMA gap at least this wide
EXIT_GAP_BPS = 0.0      # trend exit when the EMA gap closes to this
BREAK_N = 90            # breakout: bars in the entry channel
BREAK_M = 45            # breakout: bars in the exit channel
BREAK_AS_TREND = 0      # 1: breakout positions exit on the trend rules instead
Z_IN = 2.0              # reversion entry: dip this many ATRs under the mid EMA
Z_OUT = 0.2             # reversion exit: recovered to this many ATRs under it
RANGE_SLOPE_MAX = 0.25  # range regime: |slow slope| under this many ATRs
STOP_PCT = 0.015        # hard stop under the entry fill price
TP_PCT = 0.0            # take-profit above the entry fill price (0 = off)
TRAIL_PCT = 0.014       # trailing stop under the best close since entry
TRAIL_ARM = 0.0         # trailing arms only once the peak is this far above entry
MIN_HOLD = 18           # bars a signal exit is ignored after entry
COOLDOWN = 30           # bars after an exit before a new entry
MAX_HOLD_REV = 240      # reversion positions give up after this many bars
SIZE_FRAC = 0.9         # share of equity a trend/breakout entry deploys
SIZE_REV_FRAC = 0.6     # share of equity a reversion entry deploys
BRAKE_DD = 0.10         # equity drawdown from peak that flattens and pauses
VOL_CAP = 0.004         # skip entries when ATR/price is above this
MAX_SLIP_BPS = 200      # max_slippage_bps sent with every order
MIN_NOTIONAL = 2_000_000  # never send a buy under 2 USDC

HISTORY = 400           # closes retained for channels


class Bot:
    """The strategy's whole state, advanced one batch at a time."""

    def __init__(self):
        self.bars_seen = 0
        self.prev_close = None
        self.ema_fast = None
        self.ema_slow = None
        self.ema_regime = None
        self.ema_mid = None
        self.atr = None
        self.slow_hist = deque(maxlen=REGIME_BARS + 1)
        self.regime_hist = deque(maxlen=REGIME_BARS + 1)
        self.closes = deque(maxlen=HISTORY)
        self.holding = False
        self.mode = None             # family that opened the position
        self.entry_price = None      # fill price (scaled int) of the position
        self.peak_close = None       # best close since the position opened
        self.entry_seq = -1
        self.last_exit_seq = -10 ** 9
        self.equity_peak = None
        self.need_fresh = False      # set by the brake: wait for a full reset

    # -- indicators ---------------------------------------------------------
    def _feed_bar(self, bar):
        close = int(bar["c"])
        high = int(bar["h"])
        low = int(bar["l"])
        if self.ema_fast is None:
            self.ema_fast = float(close)
            self.ema_slow = float(close)
            self.ema_regime = float(close)
            self.ema_mid = float(close)
            self.atr = float(high - low)
        else:
            self.ema_fast += (2.0 / (EMA_FAST + 1)) * (close - self.ema_fast)
            self.ema_slow += (2.0 / (EMA_SLOW + 1)) * (close - self.ema_slow)
            self.ema_regime += (2.0 / (REGIME_EMA + 1)) * (close - self.ema_regime)
            self.ema_mid += (2.0 / (MID_PERIOD + 1)) * (close - self.ema_mid)
            tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))
            self.atr += (tr - self.atr) / ATR_PERIOD
        self.prev_close = close
        self.slow_hist.append(self.ema_slow)
        self.regime_hist.append(self.ema_regime)
        self.closes.append(close)
        self.bars_seen += 1
        return close

    # -- orders ---------------------------------------------------------------
    def _buy(self, state, frac):
        notional = min(int(state.equity * frac), state.cash, state.weight_room(SYMBOL))
        if notional < MIN_NOTIONAL:
            return []
        return [{
            "side": "buy",
            "symbol": SYMBOL,
            "notional_quote_units": notional,
            "max_slippage_bps": MAX_SLIP_BPS,
        }]

    def _sell_all(self, state):
        held = state.positions.get(SYMBOL)
        if held is None or held.qty <= 0:
            return []
        return [{
            "side": "sell",
            "symbol": SYMBOL,
            "qty": held.qty,
            "max_slippage_bps": MAX_SLIP_BPS,
        }]

    # -- per-batch decision -----------------------------------------------------
    def on_batch(self, state, batch):
        bar = None
        for b in batch["bars"]:
            if b["symbol"] == SYMBOL:
                bar = b
        held = state.positions.get(SYMBOL)
        now_holding = held is not None and held.qty > 0

        # Learn the entry price (and which mode is running) from the fills.
        if now_holding and not self.holding:
            self.entry_price = None
            for f in batch["fills"]:
                if f["symbol"] == SYMBOL and f["side"] == "buy" and f["status"] == "filled":
                    self.entry_price = int(f["price"])
            if self.entry_price is None and bar is not None:
                self.entry_price = int(bar["o"])
            self.entry_seq = state.seq
            self.peak_close = self.entry_price
        if self.holding and not now_holding:
            self.last_exit_seq = state.seq
            self.entry_price = None
            self.peak_close = None
            self.mode = None
        self.holding = now_holding

        if bar is None:
            return []
        close = self._feed_bar(bar)

        if self.equity_peak is None or state.equity > self.equity_peak:
            self.equity_peak = state.equity
        if state.warming_up:
            return []
        warm = EMA_SLOW + REGIME_BARS
        if GATE_MODE or USE_REGIME_GATE:
            warm = max(warm, REGIME_EMA + REGIME_BARS)
        if self.bars_seen <= warm:
            return []
        atr = self.atr
        if not atr or atr <= 0 or close <= 0:
            return []

        gap_bps = (self.ema_fast - self.ema_slow) / close * 10000.0
        slope = (self.slow_hist[-1] - self.slow_hist[-1 - SLOPE_BARS]) / atr \
            if len(self.slow_hist) > SLOPE_BARS else 0.0
        regime_slope = abs(self.slow_hist[-1] - self.slow_hist[0]) / atr \
            if len(self.slow_hist) > REGIME_BARS else 9.9
        slope_regime = (self.slow_hist[-1] - self.slow_hist[0]) / atr \
            if len(self.slow_hist) > REGIME_BARS else 0.0
        z = (close - self.ema_mid) / atr
        mom = None
        if len(self.closes) > MOM_BARS:
            past = list(self.closes)[-1 - MOM_BARS]
            if past > 0:
                mom = (close / past - 1.0) * 10000.0
        mom2 = None
        if MOM_BARS2 and len(self.closes) > MOM_BARS2:
            past2 = list(self.closes)[-1 - MOM_BARS2]
            if past2 > 0:
                mom2 = (close / past2 - 1.0) * 10000.0
        gate = GATE_MODE if GATE_MODE else (1 if USE_REGIME_GATE else 0)
        regime_ok = True
        if gate in (1, 3) and close <= self.ema_regime:
            regime_ok = False
        if gate in (2, 3):
            if len(self.regime_hist) > REGIME_BARS and self.regime_hist[-1] <= self.regime_hist[0]:
                regime_ok = False

        if self.need_fresh and gap_bps < 0.0:
            self.need_fresh = False

        # Equity brake: flatten, then wait for a full signal reset.
        if self.equity_peak and state.equity <= self.equity_peak * (1.0 - BRAKE_DD):
            self.need_fresh = True
            if self.holding:
                return self._sell_all(state)
            return []

        if self.holding:
            if close > self.peak_close:
                self.peak_close = close
            age = state.seq - self.entry_seq
            # Stops first: they apply from the first bar.
            if TP_PCT and self.entry_price is not None and close >= self.entry_price * (1.0 + TP_PCT):
                return self._sell_all(state)
            if self.entry_price is not None and close <= self.entry_price * (1.0 - STOP_PCT):
                return self._sell_all(state)
            if self.entry_price is not None and self.peak_close >= self.entry_price * (1.0 + TRAIL_ARM):
                if close <= self.peak_close * (1.0 - TRAIL_PCT):
                    return self._sell_all(state)
            if age < MIN_HOLD:
                return []
            # Time stop: a position that has gone nowhere is a failed entry.
            if TIME_STOP and age >= TIME_STOP and self.entry_price is not None \
                    and close <= self.entry_price:
                return self._sell_all(state)
            # Family signal exits.
            if self.mode == "reversion":
                if z >= -Z_OUT or age >= MAX_HOLD_REV:
                    return self._sell_all(state)
            elif self.mode == "breakout":
                window = list(self.closes)[-BREAK_M - 1:-1]
                if window and close < min(window):
                    return self._sell_all(state)
            elif self.mode == "momentum":
                if mom is not None and mom <= MOM_OUT_BPS:
                    return self._sell_all(state)
            elif EXIT_MODE == 1:  # trend, fast-cross exit
                if self.ema_fast < self.ema_slow:
                    return self._sell_all(state)
            elif EXIT_MODE == 2:  # trend, both must confirm
                if self.ema_fast < self.ema_slow and close < self.ema_slow:
                    return self._sell_all(state)
            else:  # trend, slow-break exit
                if close < self.ema_slow or gap_bps <= EXIT_GAP_BPS:
                    return self._sell_all(state)
            return []

        # Flat: entries.
        if self.need_fresh:
            return []
        if state.seq - self.last_exit_seq < COOLDOWN:
            return []
        if atr / close > VOL_CAP:
            return []
        if slope_regime < SLOPE_MIN_ATR:
            return []
        trend_ok = self.ema_fast > self.ema_slow and slope > 0.0 and gap_bps >= SEP_MIN_BPS
        if ENTRY_MODE == 1 and trend_ok:
            # Pullback entry: same trend, but only once the price has dipped
            # back to the fast EMA and turned up again — no chasing strength.
            prev = list(self.closes)[-2] if len(self.closes) >= 2 else close
            trend_ok = close <= self.ema_fast + 0.3 * atr and close > prev
        if FAMILY in ("trend", "hybrid") and trend_ok and regime_ok:
            self.mode = "trend"
            return self._buy(state, SIZE_FRAC)
        if FAMILY == "momentum" and regime_ok:
            if mom is not None and mom >= MOM_IN_BPS and close > self.ema_slow:
                if MOM_BARS2 and (mom2 is None or mom2 < MOM_IN2_BPS):
                    return []
                self.mode = "momentum"
                return self._buy(state, SIZE_FRAC)
        if FAMILY == "breakout" and regime_ok:
            window = list(self.closes)[-BREAK_N - 1:-1]
            if window and close > max(window) and self.ema_fast > self.ema_slow:
                self.mode = "trend" if BREAK_AS_TREND else "breakout"
                return self._buy(state, SIZE_FRAC)
        if FAMILY in ("reversion", "hybrid") and regime_slope <= RANGE_SLOPE_MAX and regime_ok:
            if z <= -Z_IN:
                self.mode = "reversion"
                return self._buy(state, SIZE_REV_FRAC)
        return []


_BOT = Bot()


def on_batch(state, batch):
    return _BOT.on_batch(state, batch)


if __name__ == "__main__":
    makefun_bars.run(on_batch)
