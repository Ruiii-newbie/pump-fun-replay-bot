#!/usr/bin/env python3
"""Early-momentum paper bot for agent-fun/replay/v2 (pump.fun replay).

Reads newline-delimited JSON on stdin (hello, then one batch per slot,
then done) and answers every batch with exactly one reply carrying buy /
sell orders. Python 3.13, standard library only. No network, no wall
clock, no files, no randomness: every decision is a pure function of the
messages seen so far, so two runs over the same window produce identical
replies.

Idea in one paragraph: most pump.fun coins die within minutes, a few run
hard, and the bonding curve itself rations size -- one reply may spend at
most a small share of a mint's real SOL reserves, so a coin is only worth
buying once other traders have already put real SOL into it. The bot
therefore buys only young coins whose price is already rising on
buy-dominated flow, sizes each buy to the reserve cap, pyramids at most
twice into a winner as its reserves deepen, and exits everything fast:
take-profit, hard stop, trailing stop, and a time stop. It never holds
more than the hello's max_open_positions coins at once, and it rotates
through many distinct mints instead of marrying one.

Prices are compared with exact integer arithmetic. A price is a
(num, den) pair -- lamports of quote per base unit of token -- taken from
a mint's virtual reserves (price = virtual_quote / virtual_token), and
"up x%" is tested by cross-multiplication, never by floats.
"""

import json
import sys
from collections import deque

# --- strategy knobs -------------------------------------------------------
MIN_BUY_LAMPORTS = 50_000_000       # 0.05 SOL: smallest buy worth placing
MAX_POSITION_LAMPORTS = 1_000_000_000  # 1.0 SOL: most we ever put in one mint
MAX_BUYS_PER_MINT = 3               # initial buy + at most 2 pyramid add-ons
MIN_REAL_RESERVE_LAMPORTS = 5_000_000_000  # 5 SOL of real reserves before we touch a mint
MAX_AGE_SLOTS = 14_400              # ~96 min: never enter an old coin
MIN_TRADES_TO_ENTER = 8             # trades we must have seen before an entry
MOM_WINDOW = 20                     # trades behind the momentum read
MOM_MIN_BPS = 1_000                 # price up >= 10% over the momentum window
BOUNCE_WINDOW = 3                   # last few trades must not be falling
MAX_FROM_CREATE_BPS = 30_000        # never buy above 4x the creation price
BUY_SHARE_MIN_BPS = 6_000           # >= 60% of recent volume must be buys
ADDON_MIN_SLOTS = 150               # ~1 min between buys on the same mint
ADDON_MIN_GAIN_BPS = 500            # pyramid only while up >= 5% on the position
TAKE_PROFIT_BPS = 3_500             # sell all at +35% over average entry
STOP_LOSS_BPS = 1_800               # sell all at -18% under average entry
TRAIL_TRIGGER_BPS = 2_000           # trailing stop arms at +20% over entry
TRAIL_DROP_BPS = 1_000              # ... and fires on a 10% fall from the peak
MAX_HOLD_SLOTS = 18_000             # ~2 h: time stop, sell whatever is left
REENTRY_COOLDOWN_SLOTS = 600        # ~4 min after an exit before re-entering
BUY_SLIPPAGE_BPS = 300              # 3% adverse-move tolerance on buys
SELL_SLIPPAGE_BPS = 500             # 5% on sells (a rejected sell is retried)
HIST_LEN = 48                       # trades of per-mint history kept
CASH_BUFFER_LAMPORTS = 10_000_000   # 0.01 SOL kept back for tx fees


def price_up(now, then, min_bps):
    """True when price `now` beats price `then` by at least min_bps.

    Prices are (num, den) pairs; the test is exact integer arithmetic:
    now/then - 1 >= min_bps / 10000.
    """
    n_num, n_den = now
    t_num, t_den = then
    return n_num * t_den * 10_000 >= t_num * n_den * (10_000 + min_bps)


def price_down(now, then, min_bps):
    """True when price `now` is at least min_bps below price `then`."""
    n_num, n_den = now
    t_num, t_den = then
    return n_num * t_den * 10_000 <= t_num * n_den * (10_000 - min_bps)


def price_ratio_bps(now, then):
    """(now / then - 1) in bps, floored, as an int (may be negative)."""
    n_num, n_den = now
    t_num, t_den = then
    return (n_num * t_den - t_num * n_den) * 10_000 // (t_num * n_den)


class MintState:
    """Everything the bot remembers about one mint."""

    __slots__ = (
        "mint", "created_slot", "supply", "complete",
        "vq", "vt", "rq", "rt", "last_slot",
        "hist", "first_price", "entry", "entry_slot", "peak", "buys",
        "last_exit_slot", "seen_trades",
    )

    def __init__(self, mint, created_slot, supply, vq, vt, rt):
        self.mint = mint
        self.created_slot = created_slot
        self.supply = supply
        self.complete = False
        self.vq = vq
        self.vt = vt
        self.rq = 0
        self.rt = rt
        self.last_slot = created_slot
        self.hist = deque(maxlen=HIST_LEN)  # (slot, vq, vt, is_buy, quote)
        self.first_price = (vq, vt)
        self.entry = None     # (paid_lamports, tokens) of the open position
        self.entry_slot = None  # slot the position opened (first buy's fill)
        self.peak = None      # best (vq, vt) since entry
        self.buys = 0         # filled buys on the open position
        self.last_exit_slot = -10 ** 12
        self.seen_trades = 0

    def price(self):
        return (self.vq, self.vt)

    def record_trade(self, slot, vq, vt, rq, rt, is_buy, quote):
        self.vq = vq
        self.vt = vt
        self.rq = rq
        self.rt = rt
        self.last_slot = slot
        self.seen_trades += 1
        self.hist.append((slot, vq, vt, is_buy, quote))


class Bot:
    def __init__(self, hello):
        self.capital = int(hello["capital"]["lamports"])
        self.cash = self.capital
        self.max_open = int(hello["caps"]["max_open_positions"])
        self.max_order_bps = int(hello["caps"]["max_order_bps_of_real_reserve"])
        self.max_orders = int(hello["caps"]["max_orders_per_batch"])
        self.latency = int(hello.get("latency_slots") or 0)
        self.tx_fee = int(hello["fees"]["base_fee_lamports"]) + int(
            hello["fees"]["priority_fee_lamports"]
        )
        self.mints = {}          # mint -> MintState
        self.positions = {}      # mint -> token_amount (from account)
        self.seq = -1
        self.slot = -1
        self.next_order = 1
        self.last_buy_slot = {}  # mint -> slot of the last buy we decided
        self.pending_sells = set()   # mints with a sell decided, unresolved
        self.pending_buys = {}   # client_id -> lamports reserved (size + fee)

    # -- helpers ------------------------------------------------------------
    def _cid(self, prefix):
        cid = "%s%d" % (prefix, self.next_order)
        self.next_order += 1
        return cid

    def _reserve_cap(self, state):
        """Most lamports one reply may spend on this mint (the size cap)."""
        return state.rq * self.max_order_bps // 10_000

    def _buy_size(self, state):
        """Lamports to spend on this mint now, or 0 when no buy is sized."""
        if state.rq < MIN_REAL_RESERVE_LAMPORTS:
            return 0
        cap = self._reserve_cap(state)
        if cap < MIN_BUY_LAMPORTS:
            return 0
        spent = state.entry[0] if state.entry is not None else 0
        room = MAX_POSITION_LAMPORTS - spent
        if room < MIN_BUY_LAMPORTS:
            return 0
        # Cash the account shows minus buys already decided but not yet
        # resolved (their fills land two slots later): the account lags.
        reserved = 0
        for amount in self.pending_buys.values():
            reserved += amount
        cash_room = self.cash - CASH_BUFFER_LAMPORTS - reserved
        if cash_room < MIN_BUY_LAMPORTS:
            return 0
        return min(cap, room, cash_room)

    def _momentum_ok(self, state):
        """Rising price on buy-dominated flow, not already run away."""
        hist = state.hist
        if len(hist) < MIN_TRADES_TO_ENTER:
            return False
        now = state.price()
        # Momentum: price up >= MOM_MIN_BPS over the window.
        then = (hist[0][1], hist[0][2])
        if len(hist) >= MOM_WINDOW:
            then = (hist[-MOM_WINDOW][1], hist[-MOM_WINDOW][2])
        if not price_up(now, then, MOM_MIN_BPS):
            return False
        # No fresh dump: price must not be falling over the last few trades.
        if len(hist) > BOUNCE_WINDOW:
            bounce_then = (hist[-1 - BOUNCE_WINDOW][1], hist[-1 - BOUNCE_WINDOW][2])
            if price_down(now, bounce_then, 200):
                return False
        # Not too far above the creation price: never chase a top.
        if price_up(now, state.first_price, MAX_FROM_CREATE_BPS):
            return False
        # Flow: most of the recent volume must be buys.
        buy_vol = 0
        total_vol = 0
        for _, _, _, is_buy, quote in hist:
            total_vol += quote
            if is_buy:
                buy_vol += quote
        if total_vol <= 0:
            return False
        return buy_vol * 10_000 >= total_vol * BUY_SHARE_MIN_BPS

    # -- per-batch ----------------------------------------------------------
    def on_batch(self, batch):
        self.seq = int(batch["seq"])
        self.slot = int(batch["slot"])
        events = batch.get("events") or []
        marks = batch.get("marks") or []
        fills = batch.get("fills") or []
        account = batch.get("account") or {}

        # Authoritative account state first: cash and positions.
        self.cash = int(account.get("cash_lamports", self.cash))
        self.positions = {}
        for p in account.get("positions") or []:
            self.positions[p["mint"]] = int(p["token_amount"])

        # Fold the batch's events into per-mint state.
        touched = set()
        for e in events:
            mint = e.get("mint")
            kind = e.get("event")
            if kind == "CreateEvent":
                if mint not in self.mints:
                    vq = int(e["virtual_quote_reserves"])
                    vt = int(e["virtual_token_reserves"])
                    rt = int(e["real_token_reserves"])
                    supply = int(e["token_total_supply"])
                    self.mints[mint] = MintState(
                        mint, self.slot, supply, vq, vt, rt
                    )
                touched.add(mint)
            elif kind == "TradeEvent":
                st = self.mints.get(mint)
                if st is None:
                    # A trade for a mint whose creation we never saw: seed
                    # state from the trade's own post-trade reserves.
                    st = MintState(
                        mint, self.slot, 0,
                        int(e["virtual_quote_reserves"]),
                        int(e["virtual_token_reserves"]),
                        int(e["real_token_reserves"]),
                    )
                    self.mints[mint] = st
                st.record_trade(
                    self.slot,
                    int(e["virtual_quote_reserves"]),
                    int(e["virtual_token_reserves"]),
                    int(e["real_quote_reserves"]),
                    int(e["real_token_reserves"]),
                    bool(e["is_buy"]),
                    int(e["quote_amount"]),
                )
                touched.add(mint)
            elif kind == "CompleteEvent":
                st = self.mints.get(mint)
                if st is not None:
                    st.complete = True
                touched.add(mint)

        # Marks carry the reserves the bot's own orders would meet (its own
        # impact included) for held / pending mints: use them for exits.
        mark_price = {}
        for m in marks:
            mint = m["mint"]
            st = self.mints.get(mint)
            if st is None:
                continue
            vq = int(m["virtual_quote_reserves"])
            vt = int(m["virtual_token_reserves"])
            mark_price[mint] = (vq, vt)
            st.rq = int(m["real_quote_reserves"])
            st.rt = int(m["real_token_reserves"])

        # Fills: learn the true execution price of our buys, and note exits.
        # Any resolution frees its reservation (buy spend / pending sell).
        for f in fills:
            mint = f["mint"]
            self.pending_buys.pop(f.get("client_id", ""), None)
            if f.get("side") == "sell":
                self.pending_sells.discard(mint)
            st = self.mints.get(mint)
            if st is None:
                continue
            if f.get("status") != "filled":
                continue
            if f.get("side") == "buy":
                paid = int(f["quote_lamports"])
                tokens = int(f["token_amount"])
                if tokens > 0 and paid > 0:
                    if st.entry is None:
                        st.entry = (paid, tokens)
                        st.entry_slot = int(f["slot"])
                        st.peak = st.price()
                        st.buys = 1
                    else:
                        old_paid, old_tokens = st.entry
                        st.entry = (old_paid + paid, old_tokens + tokens)
                        st.buys += 1
                    self.last_buy_slot[mint] = int(f["slot"])
            elif f.get("side") == "sell":
                if mint not in self.positions:
                    # Position fully closed: reset the entry note.
                    st.entry = None
                    st.entry_slot = None
                    st.peak = None
                    st.buys = 0
                    st.last_exit_slot = self.slot

        # A held mint whose position vanished without a sell fill in this
        # batch was force-closed (complete / flatten): reset its entry.
        for mint, st in self.mints.items():
            if st.entry is not None and mint not in self.positions:
                st.entry = None
                st.entry_slot = None
                st.peak = None
                st.buys = 0
                st.last_exit_slot = self.slot

        orders = []
        # Exits first: they free slots and cash for entries.
        orders.extend(self._exits(touched, mark_price))
        # Entries, strongest momentum first, within the order cap.
        orders.extend(self._entries(touched, len(orders)))
        return orders[: self.max_orders] if self.max_orders > 0 else []

    def _exits(self, touched, mark_price):
        orders = []
        for mint in sorted(self.positions):
            st = self.mints.get(mint)
            if st is None or st.complete:
                continue  # complete mints are force-closed by the harness
            if mint in self.pending_sells:
                continue  # a sell is already in flight for this mint
            tokens = self.positions[mint]
            if tokens <= 0:
                continue
            now = mark_price.get(mint)
            if now is None:
                # No fresh mark: only re-evaluate on a batch that touched it.
                if mint not in touched:
                    continue
                now = st.price()
            if st.entry is None:
                # Position with no entry note (e.g. a fill we missed): use
                # the current price as the baseline so exits still work.
                st.entry = (now[0], now[1])
                st.peak = now
                st.buys = 1
            entry = (st.entry[0], st.entry[1])
            # Entry price as (num, den) = paid lamports per token.
            entry_price = (entry[0], entry[1])
            if st.peak is None or price_up(now, st.peak, 1):
                st.peak = now
            reason = None
            if price_up(now, entry_price, TAKE_PROFIT_BPS):
                reason = "take"
            elif price_down(now, entry_price, STOP_LOSS_BPS):
                reason = "stop"
            elif (
                st.peak is not None
                and price_up(st.peak, entry_price, TRAIL_TRIGGER_BPS)
                and price_down(now, st.peak, TRAIL_DROP_BPS)
            ):
                reason = "trail"
            elif self.slot - (st.entry_slot if st.entry_slot is not None else self.slot) >= MAX_HOLD_SLOTS:
                reason = "time"
            if reason is None:
                continue
            cid = self._cid("s")
            self.pending_sells.add(mint)
            orders.append({
                "side": "sell",
                "client_id": cid,
                "mint": mint,
                "token_amount": str(tokens),
                "max_slippage_bps": SELL_SLIPPAGE_BPS,
            })
        return orders

    def _entries(self, touched, already):
        if len(self.positions) >= self.max_open:
            return []
        room = self.max_orders - already
        if room <= 0:
            return []
        candidates = []
        for mint in touched:
            st = self.mints.get(mint)
            if st is None or st.complete:
                continue
            if mint in self.positions:
                # Pyramid add-on only: winning, spaced out, buys left.
                if st.buys >= MAX_BUYS_PER_MINT:
                    continue
                if st.entry is None:
                    continue
                if self.slot - self.last_buy_slot.get(mint, -10 ** 12) < ADDON_MIN_SLOTS:
                    continue
                if not price_up(st.price(), (st.entry[0], st.entry[1]), ADDON_MIN_GAIN_BPS):
                    continue
                if not self._momentum_ok(st):
                    continue
                score = price_ratio_bps(st.price(), (st.entry[0], st.entry[1]))
                candidates.append((score, mint))
                continue
            # Fresh entry.
            if self.slot - st.created_slot > MAX_AGE_SLOTS:
                continue
            if self.slot - st.last_exit_slot < REENTRY_COOLDOWN_SLOTS:
                continue
            if not self._momentum_ok(st):
                continue
            score = price_ratio_bps(st.price(), st.first_price)
            candidates.append((score, mint))
        # Strongest momentum first; mint breaks ties deterministically.
        candidates.sort(key=lambda c: (-c[0], c[1]))
        orders = []
        for _, mint in candidates:
            st = self.mints[mint]
            size = self._buy_size(st)
            if size <= 0:
                continue
            # A fresh entry opens a slot; an add-on does not. Enforce the cap.
            if mint not in self.positions:
                opened = len({
                    o["mint"] for o in orders if o["side"] == "buy"
                } - set(self.positions))
                if len(self.positions) + opened >= self.max_open:
                    continue
            orders.append({
                "side": "buy",
                "client_id": self._cid("b"),
                "mint": mint,
                "quote_lamports": str(size),
                "max_slippage_bps": BUY_SLIPPAGE_BPS,
            })
            # Reserve the spend so a second buy this reply, or the next
            # reply before this fill lands, sizes against what is left.
            self.pending_buys[orders[-1]["client_id"]] = size + self.tx_fee
            if len(orders) >= room:
                break
        return orders


def main():
    bot = None
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            return 1
        kind = msg.get("type")
        if kind == "hello":
            try:
                bot = Bot(msg)
            except (KeyError, TypeError, ValueError):
                return 1
        elif kind == "batch":
            if bot is None:
                return 1
            try:
                orders = bot.on_batch(msg)
            except Exception:
                # Never crash the run on one bad batch: answer empty.
                orders = []
            reply = {"type": "reply", "seq": int(msg["seq"]), "orders": orders}
            out.write(json.dumps(reply, separators=(",", ":")) + "\n")
            out.flush()
        elif kind == "done":
            return 0
        else:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
