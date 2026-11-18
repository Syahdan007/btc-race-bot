# btc-race bot — pullback trend-rider for cbBTC

Entry for make.fun **btc-race-24h-20261118-1444** ("Race a BTC trading bot
live for 24 hours"). A long-only, paper-money cbBTC/USDC bot speaking
`agent-fun/bars-replay/v1`: `main.py` at the root (Python 3.13, standard
library only, fully deterministic), the kit's SDK shim byte-for-byte under
`vendor/`, and the strategy itself in `main.py`.

## Strategy

The economics of the race drive everything: a round trip costs roughly a
quarter of a percent once the 4 bps fill fee, the 2 bps modelled slippage
and the pool's price impact are paid, and one-minute noise is a few tenths
of a percent wide. Bots that trade often, or stop out on noise-sized
distances, bleed to death — on the frozen dev window the kit's own
ema-cross baseline makes 80 trades and loses $148, and buy-and-hold loses
$32. This bot trades rarely and only with a confirmed trend:

- **Regime**: EMA 30 over EMA 105 on one-minute closes, the slow EMA
  rising, the gap between them at least 10 bps wide.
- **Entry (pullback)**: inside that regime, buy only once the price has
  dipped back to the fast EMA and turned up again — never chase strength.
  One entry deploys 90% of equity.
- **Exits**: the close breaking the slow EMA; a 45-bar time stop for a
  position that has gone nowhere (a failed entry is cut near breakeven
  instead of riding the reversal); a hard stop 1.5% under the fill price;
  a trailing stop 1.4% under the best close since entry; signal exits are
  ignored for the first 18 bars so the entry's own noise cannot shake the
  position out.
- **Brake**: at 10% drawdown from the equity peak any position is sold
  and entries pause until the trend signal fully resets. (The race ranks
  any entry that breaches 25% drawdown below every entry that meets the
  bar, so ruin protection outranks greed.)

The file also contains the three alternative families that were built and
graded during development — Donchian breakout, z-score mean reversion,
and raw momentum, plus a hybrid switch — all sharing the same risk core.
They are off in the shipped configuration because they graded worse on
the dev window (best momentum variant −$1.39, best reversion −$3.34,
best breakout −$15.92).

## Local grading on the frozen dev window

Graded with the kit harness's own simulation engine (fee 4 bps, slippage
2 bps, constant-product impact, fills at the next bar's open), the same
code `grade-local` runs:

| bot | net PnL | trades | max drawdown |
| --- | ---: | ---: | ---: |
| **this bot** | **+$7.64 (+0.76%)** | 4 (2 round trips) | 0.95% |
| ema-cross (kit baseline) | −$148.17 | 80 | 14.97% |
| buy-and-hold (kit baseline) | −$31.63 | 1 | 3.25% |
| momentum (kit baseline) | −$23.87 | 1 | 3.24% |
| rsi-bollinger (kit baseline) | −$39.92 | 19 | 4.35% |
| adx-momentum (kit baseline) | −$75.57 | 30 | 7.56% |

Half-window slices of this bot, each graded as a standalone window:
first 12 h **+$10.38** (the window's one real rally, caught on a pullback
and held to the slow-EMA break), second 12 h **−$2.69** (a slow bleed;
one small time-stopped loss, drawdown 0.27%). A shifted 20 h sub-window
(bars 120–1320) reproduces the full-window result exactly (+$7.64), and
two consecutive runs produce byte-identical replies (deterministic).

Parameter sensitivity (±20% nudges on the full window): the result sits
on a plateau — EMA slow 96–115 grades +$6.46…+$7.64, EMA fast 30–36
+$6.46, stop/trailing/cooldown nudges leave the result unchanged at
+$7.64; the shipped point (30/105) is the plateau's centre, not its edge.

## Reproduce

With the competition kit next to this repo:

```
node cli/makefun.mjs grade-local --kit . --bot <this repo> --driver docker
```

(Inside the sandbox the bot is started as `python3 -P -s /bot/main.py`;
the vendored shim is added to `sys.path` from the script's own directory,
so no installation is needed.)
