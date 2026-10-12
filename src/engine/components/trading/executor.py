"""
Trade the scanner's signals, the part paper and live trading share.

When the scanner sees a pair with enough net edge, the executor sends one
limit order per leg. Each leg's limit is the deepest level that still
leaves the least edge the executor trades, see min_edge(), when both
ladders are walked together, so the order sweeps every level above the
floor, not just the top one. How an order reaches its venue and what comes
back is the one thing that differs: paper.py fills it against the book the
venue had when a live order would have reached it, live.py sends it to the
venue. Everything else is here.

An edge is taken only while each leg's book is current. Polymarket US
books reach us some 85 ms after the venue changes them, Kalshi's in 12, so
a price that just moved on one venue can sit next to the other's old one,
an edge that is gone by the time an order arrives. A leg on a venue in
config.CONFIRM_SECONDS must have a book newer, by the venues' own clocks,
than the other leg's last change, or wait until that change is old enough
that any reaction to it would have reached us. Live counts from the other
leg's change as of when the edge reached its least edge, from 2026-10-07,
so a change since that leaves the edge there does not start the wait
again, see opened(). The scanner offers the edge again at the next
change, or as soon as the wait ends, through recheck, so one that is real
is taken then. Once it is taken, both legs' orders go out at once, on a
game not yet started, on a future on paper, and with both legs on one
venue, see pricing.best_trade(). On a game under way, and on live's
futures, see LiveExecutor.lead(), Polymarket US's goes first, and Kalshi's
only once it has answered, for no more than it filled, see fill_legs().
Faster traders often take the Polymarket US quote before our order lands,
while Kalshi's fills, and a leg that misses first leaves nothing to sell
back. In the in-play test of 2026-10-04 live trades sent that way made
3.84$ on 69 trades, and those sending both at once lost 3.84$ on 78: they
matched about as many contracts, but 52 of them were left on one leg to
sell back, against 12. From 2026-10-12 live's Kalshi order on a game
under way may give up part of the edge left once Polymarket US has
filled, as config.LIVE_IN_PLAY_ALLOWANCE says, see follow_limit(): Kalshi's
price had often moved by the time its order came, and selling Polymarket
US's contracts back cost more than the edge the room gives up.

When the two legs fill unevenly the executor goes flat at once by selling
the excess back on its own venue, and records the result with fees. A
sale frees the money at once, where buying the missing amount on the
other venue, though it may cost less, would tie it up until the bet pays.
A sale goes no lower than config.MIN_SALE_SHARE of what the contracts
cost: below that it would give away most of what was paid, and the
contracts are kept, as a bet that may still pay out.
What it cannot sell stays on a list and is tried again on every tick,
against the books as they are then, until it is flat or the settler says
its contracts have resolved, however long the game outlasts the payout
time we expected: a market that has stopped trading sells nothing, see
flatten(). Until 2026-10-10 a trade was given up at that time: live trade
4135, a Dota 2 map, sold back 25 of the 50 contracts it held on one leg,
and the other 25 were never offered again while its market went on
trading. Only one config.FLATTEN_PAST_PAYOUT_HOURS past it is let go. A
sale that filled nothing is not tried again for config.SALE_RETRY_SECONDS,
since a book can show a bid an order never reaches. A sale its venue
turned away for lack of cash waits until the cash there has grown, since a
sale can need cash: each venue keeps one position per market, so selling
the No one trade holds where others hold more Yes is buying Yes. The list
is read back from the trades table when the process starts, so a restart
does not leave a trade exposed. The settler leaves alone a trade while an
order to flatten it is in flight.

Live trades a season's future, or a game before it starts, while the bet
pays out config.MIN_PAYOUT_HOURS or more away and the edge returns
config.MIN_ANNUAL_PCT a year or more until then, and with run.py
--live-in-play a game under way too, paying within config.MAX_PAYOUT_HOURS,
of which no return a year is asked, see live.py and pays_enough(). Live's
orders on a future sweep only the levels that return config.MIN_ANNUAL_PCT
a year. On a game under way live takes an edge of
config.LIVE_IN_PLAY_MIN_EDGE or more, and at most
config.LIVE_IN_PLAY_CONTRACTS a trade. Either only once the edge has
stayed at that least edge or more for config.LIVE_HOLD_SECONDS, as the
scanner's episode times it, the scanner offering it again then, through
recheck, see min_edge(), hold(), and most().
Paper trades an edge of config.MIN_EDGE or more on games before and while
they are played, paying within config.MAX_PAYOUT_HOURS, when first seen,
see paper.py. The legs are on two venues, or on a future two contracts of
one venue with the same rules, see pricing.best_trade(). Both venues
must be trading, outside the weekly maintenance each publishes, see
common/venues.py, and, as Kalshi last said, on the shard each market
trades on, see market/exchange.py: while one has stopped, its feed may
still show prices no order can trade at, see trading(). So must each
leg's market: one its venue says is paused, closed, or decided, or that
turned a live order away as not trading, is never offered, see
market/record.py, from 2026-10-07. A trade asks for config.FILL_SHARE of
what the books show at that edge, the share we expect to get, as far as
the cash free on each venue pays for, live as on paper. Every trade is
stored in the trades table as soon as it is sent and updated when it is
done, and every dollar moved goes through the cash the executor was
given. Live opens a trade with as little as config.LIVE_MIN_CONTRACTS,
sized in hundredths of a contract, which both venues trade in, and paper
in whole contracts, see fewest(). Settling what was bought is
money/settle.py's job.
"""

import asyncio
import dataclasses
import math
from dataclasses import dataclass
from api.orders import exact, size as order_size
from common.log import on_failure
from common.timeutil import epoch, hours_between, now_iso, shift
from common.venues import is_maintenance
from db import database
from db.models import Ledger, Leg, Trade
from engine.helper import config, fees, game
from engine.helper.pricing import annual_pct, changed_at, depth, fresh, in_steps, ladder, reach, sell_ladder, sweep, trade_words

CENT = 0.01     # The step a limit raised for the leg sent second is cut to, a price every market on both venues takes.


def shard(leg):
    """
    The exchange shard a leg's market trades on, whose cash is its own, as the catalog gave it, or None for a venue without shards.
    """
    return (leg.fee_info or {}).get("exchange_index")


def flattened_until(trade):
    """
    When an exposed trade that has not settled stops being flattened, config.FLATTEN_PAST_PAYOUT_HOURS past its payout
    time. That time is an estimate, which a game can outlast while its market trades.
    """
    return shift(trade.pays_at, hours=config.FLATTEN_PAST_PAYOUT_HOURS)


def order_cost(leg, quantity, limit=None):
    """
    The most quantity contracts of a leg can cost at its limit, or at limit when given, its venue's fee included, as the
    venue checks the cash for an order before taking it. A level below the limit costs less, fee and all, as the fee never
    changes faster than the price.
    """
    limit = leg.limit if limit is None else limit
    return quantity * limit + fees.fee(leg.venue, limit, quantity, leg.fee_info)


@dataclass
class Fill:
    """
    What came back for one order: contracts filled, dollars paid or received
    including fees, and the latency and time of the fill.
    """
    filled: float = 0           # Contracts, to the hundredth live, whole on paper.
    dollars: float = 0.0
    ms: int = 0
    ts: str | None = None
    note: str = ""
    unfunded: bool = False      # The venue turned the order away for lack of cash, so nothing traded.

    @property
    def average(self):
        return self.dollars / self.filled if self.filled else 0.0


class Executor:
    """
    Turns scanner signals into trades against the recorder's books. A
    subclass says how an order is filled, through fill() and sell_back().
    books is a function returning the newest Book of each contract, keyed by (venue, contract_id).
    cash is the money on each venue, with reserve(), release(), and apply().
    is_maintenance says whether a venue is in its weekly maintenance at a time, (venue, ISO 8601 UTC), see common/venues.py.
    """

    mode = None     # 'paper' or 'live', set by each subclass. It starts every log line.
    in_play = False     # Whether a game is traded once it has started: paper's, which trades games.
    step = 1        # The least part of a contract an order trades: whole contracts on paper, a hundredth live, see orders.STEP. A
                    # trade's size is cut to it.

    def __init__(self, conn, cash, books, log=print, clock=now_iso, is_maintenance=is_maintenance):
        if cash.mode != self.mode:
            raise ValueError(f"a {self.mode} executor cannot trade {cash.mode} money")
        self.conn = conn
        self.cash = cash
        self.books = books
        self.clock = clock          # The current time in ISO 8601 UTC, for fills and for how old a book is.
        self.log = log
        self.is_maintenance = is_maintenance    # Whether a venue is in its weekly maintenance at a time, see common/venues.py.
        self.games = {}             # Trade id maps to its pair's game date and members, for when its books go stale while it is flattened.
        self.tasks = set()          # Trades in flight, and the retry of exposed ones while it runs.
        self.done = []              # Trades finished since the last summary.
        self.exposed = {}           # Trade id maps to (Trade, [yes Leg, no Leg]) for trades holding more on one side than the other.
        self.flattening = set()     # Ids of exposed trades with an order in flight to flatten them, which the settler leaves alone.
        self.unfunded = {}          # Trade id maps to (venue, shard, cash) when a sale to flatten it was turned away for lack of cash: it is
                                    # tried again only once that cash has grown, by a payout, a deposit, or a sale.
        self.unsold = {}            # Trade id maps to when, in seconds since 1970, it may be flattened again after a sale that filled nothing.
        self.set_aside = {}         # Trade id maps to why no more orders are sent for it, which only live trading does.
        self.leads = {}             # Trade id maps to the venue whose leg goes first, for a trade whose legs are not sent at once.
        self.waiting = set()        # Pairs whose edge waited for a book to catch up since the last summary, see confirm_wait().
        self.held = set()           # Pairs whose edge was held until it had lasted since the last summary, see hold().
        self.recheck = None         # Called with (pair id, seconds) when an edge waits, to price the pair again once the wait ends.
        self.edge_since = None      # Called with a pair id, returns when its edge at live's least or more began, see Scanner.edge_since().
        self.edge_books = None      # Called with a pair id, returns when each member's book had changed then, see Scanner.edge_books().
        self.offer_again = None     # Called with (pair id, signal time, now) when a future's trade is done, so the scanner offers its
                                    # episode again, see again() and Scanner.offer_again().
        self.venue_trading = None   # Called with (venue, shard), says whether the venue's exchange takes orders there, see
                                    # market/exchange.py, or None to take its weekly maintenance as the only stop.
        self.market_closed = None   # Called with (venue, contract id, now) when a market turned an order away as not trading, so it is
                                    # left alone, see Recorder.refuse(), which only live trading does.
        self.retrying = None        # The task flattening exposed trades while one runs.
        self.totals = {"trades": 0, "profit": 0.0, "hedge": 0.0}
        self.reload_exposed()

    # ORDERS, which each subclass fills its own way.

    def book(self, key, when=None):
        """
        The newest book for a contract as this executor trades against it, or
        None when there is none. when, a time by our clock, asks for the one
        that had reached us by then, which only paper trading keeps, see
        paper.py: live trading acts on its books as they come.
        """
        return self.books().get(key)

    def fresh_book(self, key, aging=True, when=None):
        """
        The newest book for a contract, by when when given, or None when there is
        none or, when its books age, it is too old to trade, see pricing.fresh()
        and game.started().
        """
        book = self.book(key, when)
        return book if fresh(book, when or self.clock(), aging) else None

    def trading(self, leg, now):
        """
        Whether a leg's venue takes orders at now: outside its weekly
        maintenance, see common/venues.py, and, as the venue last said, on
        the shard the leg's market trades on, see venue_trading.
        """
        if self.is_maintenance(leg.venue, now):
            return False
        return self.venue_trading is None or self.venue_trading(leg.venue, shard(leg))

    def aging(self, trade):
        """
        Whether a trade's books go stale when they stop changing, as the
        scanner judges its pair's. One taken before this start is judged as a
        game's already begun, the stricter way.
        """
        kept = self.games.get(trade.id)
        return game.started(*kept, self.clock()) if kept else True

    async def fill(self, trade, leg, when=None):
        """
        Buy leg.quantity contracts of one leg of a trade at no more than
        leg.limit each, to open the trade, sent at when, by our clock, or
        now. Returns a Fill.
        """
        raise NotImplementedError

    async def sell_back(self, trade, leg, quantity, floor, when=None):
        """
        Sell back contracts held through a leg of a trade at no less than
        floor each, where the books said they would sell, sent at when, by our
        clock, or now. Returns a Fill whose dollars are the proceeds after fees.
        """
        raise NotImplementedError

    # TRADES

    def record_holdings(self, trade, legs):
        """
        Copy what the legs hold, and what that cost, onto the trade, and set its status from the matched count.
        """
        trade.yes_held, trade.no_held = legs[0].held, legs[1].held
        trade.yes_cost, trade.no_cost = legs[0].cost, legs[1].cost
        trade.status = "failed" if trade.matched == 0 else "partial" if trade.matched < trade.quantity else "filled"

    def sale_ladder(self, leg, excess, aging=True, least=0.0, when=None):
        """
        The ladder selling the excess back on the leg's own venue would sell
        into, from its fresh book, by when when given, its levels at least
        least each, or an empty one when there is no fresh book or nothing
        would sell.
        """
        book = self.fresh_book(leg.key, aging, when)
        if not book:
            return []
        selling = [(price, size) for price, size in sell_ladder(book, leg.polarity, leg.side) if price >= least - 1e-9]
        sold, _ = sweep(selling, excess, leg.venue, leg.fee_info, config.FILL_SHARE, selling=True, step=self.step)
        return selling if sold else []

    async def sell_excess(self, trade, leg, excess, average, selling, when=None):
        """
        Sell the excess back on its own venue, no lower than the book said
        it would sell, sent at when or now, and record what came back.
        Returns (contracts sold, note).
        """
        fill = await self.sell_back(trade, leg, excess, reach(selling, excess, config.FILL_SHARE, self.step), when)
        if fill.unfunded:
            if trade.id not in self.unfunded:
                self.log(f"{self.mode} {trade.label}: {leg.venue} lacked the cash to sell back {excess:g}, which waits until its cash grows")
            self.unfunded[trade.id] = (leg.venue, shard(leg), self.cash.available(leg.venue, shard(leg)))
            return 0, f"sold back 0 of {excess:g} on {leg.venue}, which lacked the cash"
        self.unfunded.pop(trade.id, None)
        if fill.filled:
            self.unsold.pop(trade.id, None)
            self.cash.apply(Ledger(fill.ts, leg.venue, fill.dollars, "sell", trade.id), shard(leg))
        else:
            self.unsold[trade.id] = epoch(self.clock()) + config.SALE_RETRY_SECONDS
        leg.held = exact(leg.held - fill.filled)
        leg.cost -= fill.filled * average
        trade.hedge_pnl += fill.dollars - fill.filled * average
        return fill.filled, f"sold back {fill.filled:g} of {excess:g} on {leg.venue}"

    async def flatten(self, trade, legs, when=None):
        """
        Sell the excess on the leg holding more back on its own venue, sent
        like any other order, no lower than config.MIN_SALE_SHARE of what it
        cost, at when, by our clock, on the books as they were then, or now.
        Returns what was done in words, or None when nothing would sell at
        that, its venue is not trading, or the trade has been set aside.
        """
        if trade.id in self.set_aside:
            return None
        long_leg, short_leg = sorted(legs, key=lambda leg: leg.held, reverse=True)
        if not self.trading(long_leg, when or self.clock()):
            return None
        excess = exact(long_leg.held - short_leg.held)
        average = long_leg.cost / long_leg.held         # What each contract the long leg holds cost.
        selling = self.sale_ladder(long_leg, excess, self.aging(trade), average * config.MIN_SALE_SHARE, when)
        if not selling:
            return None
        done, note = await self.sell_excess(trade, long_leg, excess, average, selling, when)
        if done < excess:
            note += f", {exact(excess - done):g} exposed"
        return note

    def record_fills(self, trade, fills):
        """
        Copy what each leg's order filled onto the trade, and the profit
        locked in on the contracts both legs hold, which pay a dollar a pair
        whichever way the game goes.
        """
        yes, no = fills
        trade.yes_filled, trade.yes_cost, trade.yes_latency_ms, trade.yes_fill_ts = yes.filled, yes.dollars, yes.ms, yes.ts
        trade.no_filled, trade.no_cost, trade.no_latency_ms, trade.no_fill_ts = no.filled, no.dollars, no.ms, no.ts
        trade.matched = min(yes.filled, no.filled)
        trade.profit = trade.matched * (1 - yes.average - no.average)

    def forget(self, trade_id):
        """
        Stop flattening a trade, and forget its game.
        """
        self.exposed.pop(trade_id, None)
        self.games.pop(trade_id, None)
        self.unfunded.pop(trade_id, None)
        self.unsold.pop(trade_id, None)

    async def fill_legs(self, trade, legs):
        """
        Send both legs' orders and return their Fills, in the legs' order.
        Both go out at once, unless the trade has a lead in leads: then the
        leg on that venue goes first, and the other only once it has
        answered, for no more than it filled, and not at all when it filled
        nothing, so a lead that misses leaves nothing to sell back. The
        other's limit may first be raised, see follow_limit().
        """
        lead = self.leads.pop(trade.id, None)
        if lead is None:
            return list(await asyncio.gather(*(self.fill(trade, leg) for leg in legs)))
        first = next(leg for leg in legs if leg.venue == lead)
        second = next(leg for leg in legs if leg is not first)
        led = await self.fill(trade, first)
        held = order_cost(second, second.quantity)      # What signal() held back for the second leg's order.
        second.quantity = order_size(min(second.quantity, led.filled))
        if second.quantity < self.step:
            follow = Fill(ts=led.ts, note=f"not sent, as {lead} filled nothing first")
        else:
            extra = self.follow_limit(trade, led, second, held)
            try:
                follow = await self.fill(trade, second, led.ts)
            finally:
                if extra:
                    self.cash.release(second.venue, extra, shard(second))
        return [led, follow] if legs[0] is first else [follow, led]

    def allowance(self, trade):
        """
        The share of the edge left once the leg sent first has filled that
        the other leg's order may give up to fill, see follow_limit(): none
        on paper, which sends its legs at the signal's limits, and live's
        own on a game under way, see LiveExecutor.allowance().
        """
        return 0.0

    def follow_limit(self, trade, led, follow, held):
        """
        Raise the limit of the leg sent second, follow, once the first has
        filled at led's average cost a contract, fees included, so it may
        give up allowance() of the edge left between that cost and its
        limit, fees included, to fill. The limit is cut to the cent below,
        is never past 0.99, and goes no further than the cash free on its
        venue, or shard, pays for beyond held, what was held back for the
        order at its old limit. Returns the dollars held back beyond held,
        which the caller gives back once the order has answered. The trade
        keeps the signal's limits, and the order its own, see Order.
        """
        share = self.allowance(trade)
        left = 1 - led.average - fees.cost_per_contract(follow.venue, follow.limit, follow.fee_info)
        if share <= 0 or left <= 0 or not led.filled:
            return 0.0
        most = 1 - led.average - (1 - share) * left        # The most each contract may cost, fee included.
        limit = min(math.floor(round(most * 100, 6)) / 100, 1 - CENT)
        while limit > follow.limit + 1e-9:
            extra = order_cost(follow, follow.quantity, limit) - held
            if (fees.cost_per_contract(follow.venue, limit, follow.fee_info) <= most + 1e-9
                    and extra <= self.cash.spendable(follow.venue, shard(follow)) + 1e-9):
                follow.limit = limit
                extra = max(extra, 0.0)
                self.cash.reserve(follow.venue, extra, shard(follow))
                return extra
            limit = round(limit - CENT, 2)
        return 0.0

    async def run_trade(self, trade, legs):
        """
        Fill both legs, see fill_legs(), flatten any mismatch as soon as both answers are in, and record the result.
        """
        reserved = [order_cost(leg, leg.quantity) for leg in legs]
        fills = await self.fill_legs(trade, legs)
        answered = max((fill.ts for fill in fills if fill.ts), default=None)     # When the later answer came, which paper works out after.
        for leg, fill, cost in zip(legs, fills, reserved):
            self.cash.release(leg.venue, cost, shard(leg))
            leg.held, leg.cost = fill.filled, fill.dollars
            if fill.filled:
                self.cash.apply(Ledger(fill.ts, leg.venue, -fill.dollars, "buy", trade.id), shard(leg))
        self.record_fills(trade, fills)
        trade.hedge_pnl = 0.0
        hedge = None                # How the mismatch was flattened, in words, when there was one.
        if trade.yes_filled != trade.no_filled:
            exposed = exact(abs(trade.yes_filled - trade.no_filled))
            hedge = (await self.flatten(trade, legs, answered)
                     or f"{exposed:g} exposed, {self.set_aside.get(trade.id, 'nothing to sell it into')}")
        self.record_holdings(trade, legs)
        notes = [f"{leg.side} leg {fill.note}" for leg, fill in zip(legs, fills) if fill.note]
        trade.hedge = ", ".join(([hedge] if hedge else []) + notes) or "none"
        database.update_trade(self.conn, trade)
        if legs[0].held != legs[1].held and trade.id not in self.set_aside:
            self.exposed[trade.id] = (trade, legs)
        else:
            self.forget(trade.id)
        self.totals["trades"] += 1
        self.totals["profit"] += trade.profit
        self.totals["hedge"] += trade.hedge_pnl
        self.done.append(trade)
        self.log(f"{self.mode} {trade.status}: {trade.label}, {trade.trade}, wanted {trade.quantity:g}, filled {trade.yes_filled:g}/{trade.no_filled:g}, "
                 f"locked in {trade.profit:.2f}$, hedge {trade.hedge} {trade.hedge_pnl:+.2f}$")

    def settled(self, trade_id):
        """
        Called by the settler when a trade has settled. Its contracts have resolved, so it is not flattened any more.
        """
        self.forget(trade_id)

    def reload_exposed(self):
        """
        Take back this mode's trades left holding more on one side than the
        other when the process last stopped, so they are flattened again, as
        retry() would have gone on doing: any not settled, though its payout
        time has passed, unless it is past flattened_until(). One with an
        order of unknown outcome is left to a human.
        """
        given_up = shift(self.clock(), hours=-config.FLATTEN_PAST_PAYOUT_HOURS)     # A payout time this or earlier is past flattened_until().
        for trade, *fee_infos in database.load_exposed_trades(self.conn, self.mode, given_up):
            self.exposed[trade.id] = (trade, [dataclasses.replace(leg, fee_info=fee_info) for leg, fee_info in zip(trade.legs(), fee_infos)])
        if self.exposed:
            self.log(f"{self.mode} trades left exposed before this start, flattening again: {', '.join(map(str, self.exposed))}")

    async def retry(self, now):
        """
        Try once more to flatten every exposed trade against the current books.
        A trade is tried until it is flat or settles, see settled(), past its
        payout time too, which is only an estimate: a market that has stopped
        trading sells nothing, see flatten(). One past flattened_until() is
        left to settle as it stands. One whose last sale filled nothing waits
        config.SALE_RETRY_SECONDS, and one whose last sale its venue turned
        away for lack of cash waits until the cash there has grown, rather
        than sending the same order every tick.
        Returns whether anything was sold, which is all that changes a result.
        """
        sold = False
        for trade_id, (trade, legs) in list(self.exposed.items()):
            if trade_id not in self.exposed:
                continue        # Settled while an earlier trade was being flattened.
            if now >= flattened_until(trade):
                self.forget(trade_id)
                continue
            if trade_id in self.set_aside:
                self.forget(trade_id)
                trade.hedge += f", then {self.set_aside[trade_id]}"
                database.update_trade(self.conn, trade)
                continue
            waiting = self.unfunded.get(trade_id)
            if waiting and self.cash.available(waiting[0], waiting[1]) <= waiting[2] + 1e-9:
                continue
            if epoch(now) < self.unsold.get(trade_id, 0):
                continue
            before, held = trade.hedge_pnl, [leg.held for leg in legs]
            self.flattening.add(trade_id)
            try:
                note = await self.flatten(trade, legs)
            finally:
                self.flattening.discard(trade_id)
            if not note or [leg.held for leg in legs] == held:
                continue        # Nothing sold, so nothing to record.
            sold = True
            self.record_holdings(trade, legs)
            left = exact(abs(legs[0].held - legs[1].held))
            trade.hedge += f", then {note} at {now[11:19]}"
            database.update_trade(self.conn, trade)
            self.totals["hedge"] += trade.hedge_pnl - before
            self.log(f"{self.mode} flattened {trade.label}: {note}, {left:g} still exposed, hedge {trade.hedge_pnl:+.2f}$")
            if not left:
                self.forget(trade_id)
        return sold

    def summary(self):
        """
        One line for the trades finished since the last summary and the running totals.
        """
        recent = self.done
        self.done = []
        counts = {s: sum(1 for t in recent if t.status == s) for s in ("filled", "partial", "failed")}
        waits = ""
        for pairs, why in ((self.held, "held until they lasted"), (self.waiting, "waited for a book to catch up")):
            if pairs:
                waits += f"; {len(pairs)} pairs' edges {why}"
                pairs.clear()
        return (f"{self.mode}: {len(recent)} trades ({counts['filled']} filled, {counts['partial']} partial, {counts['failed']} failed), "
                f"locked in {sum(t.profit for t in recent):.2f}$, hedges {sum(t.hedge_pnl for t in recent):+.2f}$; "
                f"total {self.totals['trades']} trades, {self.totals['profit'] + self.totals['hedge']:.2f}$; balances {self.cash.summary()}{waits}")

    # SIGNALS

    def spawn(self, coroutine):
        """
        Run a coroutine as a task this executor keeps until it is done, logging it if it fails.
        """
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(on_failure(self.log, f"{self.mode} trade task"))
        return task

    def under_way(self, pair, yes, no, now):
        """
        Whether the pair's game may have started by now, judged by every
        member of the pair the scanner gives, since a Kalshi contract gives no
        kickoff, or else by the legs' members yes and no. A future never starts.
        """
        return game.started(pair.get("game_date"), pair.get("members") or (yes, no), now)

    def plays(self, pair, yes, no, now):
        """
        Whether this executor trades the pair now, by whether its game may
        have started: live, a game not yet started or a future, which never
        starts; paper, whose in_play is set, a game under way too.
        """
        return self.in_play or not self.under_way(pair, yes, no, now)

    def pays_in_time(self, hours, pair):
        """
        Whether this executor trades a bet on the pair paying out hours from now: live, one config.MIN_PAYOUT_HOURS or more away.
        """
        return hours >= config.MIN_PAYOUT_HOURS

    def pays_enough(self, edge, now, pays_at, pair, under_way):
        """
        Whether an edge is worth the capital it ties up until the bet pays at
        pays_at: when this executor trades, see pays_in_time(), and returning
        config.MIN_ANNUAL_PCT a year or more until then, but on a game under
        way any edge, from 2026-10-05 at the user's asking: it pays within
        config.MAX_PAYOUT_HOURS, where five cents returns over 1,900% a year,
        so the rate never turned one down.
        """
        if not pays_at or not self.pays_in_time(hours_between(now, pays_at), pair):
            return False
        return under_way or annual_pct(edge, game.days_until(now, pays_at)) >= config.MIN_ANNUAL_PCT

    def lead(self, yes, no, under_way):
        """
        The venue whose leg goes first, see fill_legs(), or None to send
        both at once: Polymarket US, with a leg on each venue, on a game
        under way. Live sends it first on a future too, see
        LiveExecutor.lead().
        """
        both = yes["venue"] != no["venue"] and "polymarket_us" in (yes["venue"], no["venue"])
        return "polymarket_us" if both and under_way else None

    def min_edge(self, pair, yes, no, now):
        """
        The least edge this executor trades on the pair at now, and the floor for the deeper levels its orders sweep:
        config.MIN_EDGE, paper's, while live has its own, see LiveExecutor.min_edge().
        """
        return config.MIN_EDGE

    def most(self, pair, yes, no, now):
        """
        The most contracts this executor asks for on the pair at now, or None for as many as the books and the cash
        allow: live's on a game under way, see LiveExecutor.most().
        """
        return None

    def fewest(self):
        """
        The fewest contracts this executor opens a trade with: one whole contract on paper, whose fills are worked out
        in whole contracts, and live's own, see LiveExecutor.fewest().
        """
        return self.step

    def hold(self, pair, yes, no, now):
        """
        How many seconds the pair's edge must still last before this executor trades it, 0 when it may now: paper takes
        an edge the moment it is seen, live waits, see LiveExecutor.hold().
        """
        return 0.0

    def opened(self, pair):
        """
        When each member's book of the pair had last changed as the edge
        began, by (venue, contract id), for confirm_wait() to count from in
        place of the other leg's last change: nothing for paper, which counts
        from that, and live's, see LiveExecutor.opened().
        """
        return {}

    def confirm_wait(self, pair, yes, no, now):
        """
        How many seconds until each leg's book is current enough to trade on,
        0 when it is now, or None when a leg has no book. A leg on a venue in
        config.CONFIRM_SECONDS needs a book newer, by the venues' own clocks,
        than the other leg's last change, or else that change must be at
        least that many seconds old. The other leg's change is the one
        opened() gives, when it gives one, so a later change does not count.
        A book the venue gave no time for counts from when it reached us.
        """
        times = []
        for member in (yes, no):
            book = self.book((member["venue"], member["contract_id"]))
            if book is None:
                return None
            times.append(changed_at(book))
        (yes_at, no_at), clock, opened = times, epoch(now), self.opened(pair)
        wait = 0.0
        for member, own, other_member, other in ((yes, yes_at, no, no_at), (no, no_at, yes, yes_at)):
            other = opened.get((other_member["venue"], other_member["contract_id"]), other)
            if own < other:
                wait = max(wait, other + config.CONFIRM_SECONDS.get(member["venue"], 0) - clock)
        return wait

    def put_off(self, pair_id, seconds, pairs):
        """
        Turn a pair's edge down for now, adding the pair to pairs, held or
        waiting, which the summary counts, and, when seconds says how long
        until it may be taken, have the scanner price the pair again then.
        Returns False, as signal() does for an edge it does not take.
        """
        pairs.add(pair_id)
        if seconds and self.recheck:
            self.recheck(pair_id, seconds)
        return False

    def quantity_for(self, legs, least, most=None):
        """
        Set each leg's limit and return how many contracts to ask for. The
        two ladders are walked together through the levels that keep an
        edge of least, see min_edge(), each limit set at the deepest level
        reached. The quantity is config.FILL_SHARE of what those levels
        show, the share we expect to get, so an unchanged book fills in
        full, and no more than the cash free pays for, fees included, see
        order_cost(), both legs' at once where they share a venue's cash.
        Until 2026-10-10 the fees were left out, so a trade the cash sized
        was turned away for them: Kalshi refused trade 4003's 48.14
        contracts at 53 cents, 25.51$, on a shard holding about that, for
        the 0.84$ fee, and Polymarket US's leg, filled first, was sold back
        for 2.09$ less. Given most, it is no more than that, and the walk
        stops at the levels that hold it, so no limit goes deeper than
        those. It is cut to whole steps, see step: whole contracts on
        paper, hundredths live.
        """
        yes_leg, no_leg = legs
        yes_book, no_book = self.book(yes_leg.key), self.book(no_leg.key)
        if yes_book is None or no_book is None:
            return 0
        yes_ladder = ladder(yes_book, yes_leg.polarity, "yes")
        no_ladder = ladder(no_book, no_leg.polarity, "no")
        yes_leg.limit, no_leg.limit, available = depth(yes_ladder, no_ladder, (yes_leg.venue, yes_leg.fee_info),
                                                       (no_leg.venue, no_leg.fee_info), least,
                                                       None if most is None else most / config.FILL_SHARE)
        if not available:
            return 0
        paying = {}                 # The legs each venue's cash pays for, its shard's where it has them.
        for leg in legs:
            paying.setdefault((leg.venue, shard(leg)), []).append(leg)
        affordable = min(self.cash.spendable(*part) / sum(fees.cost_per_contract(leg.venue, leg.limit, leg.fee_info) for leg in group)
                         for part, group in paying.items())
        quantity = in_steps(min(available * config.FILL_SHARE, affordable, math.inf if most is None else most), self.step)
        # A fee rounded up, or Polymarket US's to the cent, can take the cost past the cash by a step or two.
        while quantity > 0 and any(sum(order_cost(leg, quantity) for leg in group) > self.cash.spendable(*part) + 1e-9
                                   for part, group in paying.items()):
            quantity = round(quantity - self.step, 2)
        return order_size(quantity)

    def again(self, trade, done):
        """
        Once a future's trade is done, its task done, have the scanner offer
        its episode again, see Scanner.offer_again(), when it matched
        something and left nothing to flatten, so an edge lasting past the
        trade is traded again, once it has lasted as live waits for any edge
        to, counted from now. From 2026-10-08, at the user's asking: of 134
        live futures trades from 2026-10-05, most edges ended with the
        trade, but 24 lasted a minute or more past it. A trade that matched
        nothing, its quote likely gone, or that left a leg to flatten ends
        the executor's trading of the episode, as every trade did before,
        and so does every trade on a game, whose edges last a fraction of a
        second and whose larger orders mostly fail.
        """
        if self.offer_again is None or done.cancelled() or done.exception() is not None:
            return
        if trade.matched > 0 and trade.id not in self.exposed and trade.id not in self.set_aside:
            self.offer_again(trade.pair_id, trade.signal_ts, self.clock())

    def signal(self, pair, yes, no, edge, size, fee_infos, now):
        """
        Called by the scanner when a pair shows an edge. Sends the two legs
        when the edge, see min_edge(), whether its game has started, see
        plays(), the time until the bet pays, its return a year, how long
        the edge has lasted, see hold(), both venues trading, and the
        balances allow, the leg on lead()'s venue first when it names one.
        Returns True when orders were sent, so the scanner sends no more for
        this episode, unless a future's trade gives it back once done, see
        again(). The scanner's size counts every level with a positive
        edge, while the legs are sized from the levels that keep the least
        edge, see quantity_for(). The cost is reserved here, before anything
        is awaited, so a second signal in the same moment sees what is left.
        """
        least = self.min_edge(pair, yes, no, now)
        if edge < least:
            return False
        if not self.plays(pair, yes, no, now):
            return False
        pays_at = game.pays_at((yes, no), pair["sport"])
        under_way = self.under_way(pair, yes, no, now)
        if not self.pays_enough(edge, now, pays_at, pair, under_way):
            return False
        hold = self.hold(pair, yes, no, now)
        if hold > 0:
            return self.put_off(pair["id"], hold, self.held)
        wait = self.confirm_wait(pair, yes, no, now)
        if wait is None or wait > 0:
            return self.put_off(pair["id"], wait, self.waiting)
        legs = [Leg(side, m["venue"], m["contract_id"], m["polarity"], fee_info=fee_infos[(m["venue"], m["contract_id"])])
                for side, m in (("yes", yes), ("no", no))]
        if not all(self.trading(leg, now) for leg in legs):
            return False
        quantity = self.quantity_for(legs, least, self.most(pair, yes, no, now))
        if quantity < self.fewest() - 1e-9:
            return False
        for leg in legs:
            leg.quantity = quantity
            self.cash.reserve(leg.venue, order_cost(leg, quantity), shard(leg))
        yes_leg, no_leg = legs
        trade = Trade(mode=self.mode, pair_id=pair["id"], label=pair["label"], trade=trade_words(yes, no), signal_ts=now, edge=edge,
                      quantity=quantity, pays_at=pays_at, in_play=int(under_way),
                      yes_venue=yes["venue"], yes_contract=yes["contract_id"], yes_polarity=yes["polarity"], yes_limit=yes_leg.limit,
                      no_venue=no["venue"], no_contract=no["contract_id"], no_polarity=no["polarity"], no_limit=no_leg.limit)
        database.insert_trade(self.conn, trade)
        self.games[trade.id] = (pair.get("game_date"), (yes, no))
        lead = self.lead(yes, no, under_way)
        if lead:
            self.leads[trade.id] = lead
        task = self.spawn(self.run_trade(trade, legs))
        if pair.get("game_date") is None:
            task.add_done_callback(lambda done: self.again(trade, done))
        return True

    def tick(self, now):
        """
        Once a second from the session. Tries again to flatten what is still exposed.
        """
        if self.exposed and (self.retrying is None or self.retrying.done()):
            self.retrying = self.spawn(self.retry(now))
