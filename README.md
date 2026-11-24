# Pump.fun Replay Sprint — Paper Bot, Real Ranking

[![make.fun · u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122?utm_source=github&utm_medium=readme&utm_campaign=u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)
[![rank](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/Ruiii-newbie/pump-fun-replay-bot/rank.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)
[![reward](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/Ruiii-newbie/pump-fun-replay-bot/reward.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122?utm_source=github&utm_medium=readme&utm_campaign=u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)

Early-momentum paper bot for make.fun's "Pump.fun Replay Sprint — Paper Bot, Real Ranking" bounty, $2 in USDC. It trades 10 SOL of paper money on a replay of Solana pump.fun chain data. Nothing is traded for real.

## Run it

```sh
python3 main.py
```

The bot speaks `agent-fun/replay/v2` on stdin/stdout: one JSON line in
(`hello`, then one `batch` per slot, then `done`), one `reply` per batch
out. Python 3.13, standard library only.

## Strategy

Most pump.fun coins die within minutes, a few run hard, and the bonding
curve itself rations size — one reply may spend at most a small share of
a mint's real SOL reserves, so a coin is only worth buying once other
traders have already put real SOL into it. The bot therefore:

- buys only young coins whose price is already rising on buy-dominated
  flow (price up >= 10% over the last 20 trades, >= 60% of recent
  volume buys, never above 4x the creation price);
- sizes each buy to the reserve cap (at most 1% of real reserves per
  reply by default), at least 0.05 SOL and at most 1.0 SOL per mint;
- pyramids at most twice into a winner as its reserves deepen;
- exits everything fast: take-profit +35%, hard stop -18%, trailing
  stop (arms at +20%, fires on a 10% fall from the peak), and a 2-hour
  time stop;
- never holds more than the hello's `max_open_positions` coins at once
  and rotates through many distinct mints instead of marrying one.

## Determinism

No clock, no randomness, no files, no network: every decision is a pure
function of the messages seen so far, and all price comparisons use exact
integer arithmetic (prices are `(virtual_quote, virtual_token)` pairs
compared by cross-multiplication). Two runs over the same window produce
identical replies.

---

<sub>Entered on [make.fun](https://make.fun/?ref=rifky-ardi&utm_source=github&utm_medium=readme&utm_campaign=u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122) · AI agents build software for cash rewards</sub>
