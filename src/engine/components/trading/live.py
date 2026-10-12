"""
Trade the scanner's signals with real orders on the venues.

The live executor makes the same decisions as the paper one, from the
shared Executor: which signals to take, how many contracts, the limit of
each leg, and how to flatten a leg that filled short. Only the orders
differ. Each is a real immediate or cancel limit order, sent through the
venue client's place_order() on a thread of its own, so an order never
waits behind a settlement lookup or a catalog refresh. A buy's limit is
the leg's limit, and an order that flattens sells no lower than the
deepest price the books said it would reach, so a book that moved leaves
the rest exposed for the next tick rather than filling far from where it
was priced. Every order is stored in the orders table before it is sent,
with the times of the book it went out on, the venue's for its last
change and ours for when that reached us, and updated with the venue's
answer, and the money is the venues' own, through LiveBalances from
money/live.py.

Both venues fill orders in hundredths of a contract, so fills, holdings,
and the orders that flatten are all counted to the hundredth, and from
2026-10-07 a trade opens in hundredths too, with as little as
config.LIVE_MIN_CONTRACTS, where before it opened whole contracts, at
least one. Every config.LIVE_POSITION_SECONDS the live executor reads each
venue's positions and logs any contract the venue holds more or less of
than the live trades say, which would mean the records are wrong, see
check_positions().

Live trades the futures, see executor.py, and with run.py --live-in-play
also the games, matches, and races under way that pay within
config.MAX_PAYOUT_HOURS, by its own rules below. With its legs on two
venues, Polymarket US's order goes first and Kalshi's only once that has
answered, for what it filled, see Executor.fill_legs(), on a game under way
at a limit raised by up to config.LIVE_IN_PLAY_ALLOWANCE of the edge left,
from 2026-10-12, see allowance(). From 2026-10-04 to 10-05 the in-play test
traded 200 of them at no more than 5 contracts, each beside a paper twin on
the same signal, see tools/in_play_test.py.
When paper trades the same games, each live order leaves a footprint of
what it took, which paper adds back, see footprints.py.

Live takes a future's edge when it returns config.MIN_ANNUAL_PCT a year
until it pays, its orders sweeping only the levels that return that, not
every level down to config.MIN_EDGE, see min_edge(). On a game under way
it takes an edge of config.LIVE_IN_PLAY_MIN_EDGE or more, two cents, and
asks for no more than config.LIVE_IN_PLAY_CONTRACTS a trade, twenty,
with no limit on how many such trades. Either only once the edge has
stayed at that least edge or more for config.LIVE_HOLD_SECONDS, a tenth of
a second, by the scanner's episode: one that ends sooner is never traded,
and one still there when the time is up is priced again then and traded,
see hold(). A Polymarket US leg's book must still be current, counted
from the Kalshi leg's change as of when the edge began, see opened(). All
at the user's asking: the games' rules from 2026-10-06, their wait half a
second until 2026-10-07, when futures, taken at once until then, began
waiting too, and the book's wait stopped starting again on a change that
left the edge. From 2026-10-05 04:07 UTC to that day's 15:14 UTC build
every edge, futures' too, waited half a second; then a game's was taken
at five cents, five contracts a trade, for 200 trades, only on the signal
a change of the Polymarket US leg's book brought, which in seven hours let
one episode through.

An order its venue turned away because the market was not trading,
paused, closed, or decided, or because the exchange had stopped, is
closed, see orders.closed(). Nothing traded, and the market is left alone
for config.CLOSED_MARKET_SECONDS, or until its venue says it trades, see
Recorder.refuse(), so no more orders go to it, and from 2026-10-07 it is
no refusal to the brakes. On 2026-10-07 a paused CS2 match's Kalshi leg
was turned away so after its Polymarket US leg had filled.

An order whose outcome cannot be known, because no answer came, the venue
failed on its side, or its answer cannot be read, leaves what its trade
holds unknown. That trade is set aside: no more orders are sent for it,
and the log says which order to look up on the venue. No email goes out
for one, but enough of them halt live trading, and the email about the
halt names them all. The rest of live trading goes on. When live trading
halts, whether flattening goes on then, and what counts as a refusal, is
in brakes.py.
"""

import asyncio
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from api import kalshi, orders, polymarket_us
from common import jsonutil
from common.paths import NAME
from common.periodic import Periodic
from common.timeutil import at_seconds, now_iso
from common.venues import VENUES, is_maintenance
from db import database
from db.models import Order
from engine.components.trading.brakes import Brakes
from engine.components.trading.executor import Executor, Fill
from engine.helper import config, game
from engine.helper.pricing import live_hold, live_min_edge

PLACE = {"kalshi": kalshi.place_order, "polymarket_us": polymarket_us.place_order}   # How each venue takes an order.
POSITIONS = {"kalshi": kalshi.positions, "polymarket_us": polymarket_us.positions}    # How each venue reports what the account holds.
ORDER_THREADS = 8       # Orders in flight at once. Two per trade, so a burst of signals is not held back.


class LiveExecutor(Executor):
    """
    Sends real orders for the trades the shared Executor decides on.
    place maps a venue to its place_order function. notifier is the Notifier
    from notify.py, which emails a human when live trading halts and when a
    venue's cash, or one of its shards' in config.LIVE_SHARDS, runs low.
    positions maps a venue to how it reports what the account holds, by
    default POSITIONS, which check_positions() compares with the trades.
    """

    mode = "live"
    step = orders.STEP      # The venues fill, and take orders, in hundredths of a contract.

    def __init__(self, conn, cash, books, log=print, clock=now_iso, place=None, notifier=None, positions=None,
                 is_maintenance=is_maintenance, in_play=False, footprints=None):
        super().__init__(conn, cash, books, log, clock, is_maintenance)
        self.in_play = in_play      # Whether live trades games under way too, see plays().
        self.footprints = footprints    # Where each order leaves what it took, for paper to give back, or None.
        self.place = place or PLACE
        self.threads = ThreadPoolExecutor(ORDER_THREADS, thread_name_prefix="orders")
        self.brakes = Brakes(conn, cash, log, notifier, clock)
        self.notifier = notifier
        self.low = set()            # (venue, shard or None) whose cash is under config.LIVE_LOW_CASH, once a human has been told.
        self.positions = POSITIONS if positions is None else positions
        self.checks = Periodic(lambda: config.LIVE_POSITION_SECONDS, log, "live position check")
        self.told = {}              # (venue, contract) maps to the mismatch last logged on it, so it is logged once.

    @property
    def halted(self):
        return self.brakes.halted

    def watch_cash(self, venue, part, now):
        """
        Email once when the cash on a venue, or on its shard part, falls under config.LIVE_LOW_CASH, and log when it is back over.
        """
        dollars = self.cash.available(venue, part)
        where = venue if part is None else f"{venue} shard {part}"
        if dollars < config.LIVE_LOW_CASH and (venue, part) not in self.low:
            self.low.add((venue, part))
            subject = f"{NAME} live {where} cash low: {dollars:,.2f}$"
            body = (f"Live cash on {where} is {dollars:,.2f}$, under {config.LIVE_LOW_CASH:,.2f}$. Trades there go on as far as it "
                    f"pays for, so add money to the venue to keep trading"
                    + (", or move some to the shard with python3 -m tools.kalshi_shards." if part is not None else "."))
            if self.notifier:
                self.notifier.send("low_cash", subject, body, now)
            else:
                self.log(f"live {where} has {dollars:,.2f}$, under {config.LIVE_LOW_CASH:,.2f}$")
        elif dollars >= config.LIVE_LOW_CASH and (venue, part) in self.low:
            self.low.discard((venue, part))
            self.log(f"live {where} has {dollars:,.2f}$, back over {config.LIVE_LOW_CASH:,.2f}$")

    def tick(self, now):
        """
        The shared tick, and an email once when a venue's cash falls under
        config.LIVE_LOW_CASH, again only after it has been back over. A venue
        with shards in config.LIVE_SHARDS is watched shard by shard, since an
        order spends only its own shard's cash.
        """
        super().tick(now)
        for venue in VENUES:
            if not self.cash.known(venue):
                continue
            for part in config.LIVE_SHARDS.get(venue) or (None,):
                self.watch_cash(venue, part, now)

    # POSITIONS

    async def check_positions(self):
        """
        Compare what each venue holds of each contract with what the open live
        trades hold, and log a contract on which they differ by a hundredth
        of one or more, once until the difference changes. Fills are counted
        to the hundredth the venues trade in, so a difference means the
        records are wrong, and it is left to a human, see
        tools/repair_fills.py. Nothing is compared while a trade or a flatten
        is in flight, or when an order went out while a venue was read, since
        the trades may not show it yet.
        """
        if self.tasks:
            return
        for venue, read in self.positions.items():
            before = database.last_order_id(self.conn)
            position = await asyncio.to_thread(read)
            if self.tasks or database.last_order_id(self.conn) != before:
                return
            held = database.load_holdings(self.conn, self.mode)
            for contract in sorted(set(position) | {c for v, c in held if v == venue}):
                key, theirs, ours = (venue, contract), position.get(contract, 0.0), held.get((venue, contract), 0)
                if abs(round(theirs - ours, 2)) < orders.STEP:
                    self.told.pop(key, None)
                    continue
                message = (f"live {venue} holds {theirs:g} of {contract}, the trades {ours:g}: the records differ from the venue, "
                           f"see tools/repair_fills.py")
                if self.told.get(key) != message:
                    self.told[key] = message
                    self.log(message)

    def watch_positions(self, clock):
        """
        Once a second from the desk, with the wall clock in seconds. Starts a check of the positions when one is due and none runs.
        """
        self.checks.tick(clock, self.check_positions)

    # SIGNALS

    def by_game_rules(self, pair):
        """
        Whether live judges the pair by its rules for a game, match, or race, which it has only with in_play,
        rather than by the shared Executor's, as it does a future.
        """
        return self.in_play and pair.get("game_date") is not None

    def in_game(self, pair, yes, no, now):
        """
        Whether live trades the pair now as a game under way, which it does only with in_play, see plays().
        """
        return self.by_game_rules(pair) and self.under_way(pair, yes, no, now)

    def min_edge(self, pair, yes, no, now):
        """
        config.LIVE_IN_PLAY_MIN_EDGE on a game under way. Otherwise, on a
        future, the edge that returns config.MIN_ANNUAL_PCT a year until the
        bet pays, so each level its orders sweep returns that, as the top
        must, see pays_enough(). config.MIN_EDGE for a bet that gives no
        payout time, which pays_enough() turns down anyway. See
        pricing.live_min_edge(), which the scanner uses too.
        """
        pays_at = game.pays_at((yes, no), pair["sport"])
        return live_min_edge(self.in_game(pair, yes, no, now), game.days_until(now, pays_at))

    def most(self, pair, yes, no, now):
        """
        config.LIVE_IN_PLAY_CONTRACTS on a game under way, otherwise as many as the books and the cash allow.
        """
        return config.LIVE_IN_PLAY_CONTRACTS if self.in_game(pair, yes, no, now) else None

    def fewest(self):
        """
        config.LIVE_MIN_CONTRACTS, on a future or a game alike.
        """
        return config.LIVE_MIN_CONTRACTS

    def hold(self, pair, yes, no, now):
        """
        How many seconds until the pair's edge has stayed at live's least
        edge or more, see min_edge(), for config.LIVE_HOLD_SECONDS, by the
        scanner's episode, see pricing.live_hold(), 0 once it has, or without
        a scanner to say when it began, as in tests. On a future too from
        2026-10-07, taken at once until then.
        """
        since = self.edge_since(pair["id"]) if self.edge_since else None
        return 0.0 if since is None else live_hold(since, now)

    def opened(self, pair):
        """
        When each member's book of the pair had last changed as its edge
        reached live's least edge, by the scanner's episode, see
        Scanner.edge_books(), so a leg's wait for its book to catch up, see
        confirm_wait(), counts from the other leg's change as of then: a
        change since that left the edge there does not start it again.
        Polymarket US's order goes first, see lead(), so a quote it no
        longer shows costs only an order that fills nothing. Before
        2026-10-07 every Kalshi change started it again, and in a game under
        way, where Kalshi's books change many times a second, an edge could
        wait as long as it lasted.
        """
        return self.edge_books(pair["id"]) if self.edge_books else {}

    def plays(self, pair, yes, no, now):
        """
        A future, and with in_play a game, match, or race only once
        it is under way, judged by every member of the pair, since a Kalshi
        contract gives no kickoff. Otherwise as the shared Executor says:
        live's desk offers it only futures then, see run.py.
        """
        if not self.by_game_rules(pair):
            return super().plays(pair, yes, no, now)
        return self.under_way(pair, yes, no, now)

    def pays_in_time(self, hours, pair):
        """
        With in_play, a game paying within config.MAX_PAYOUT_HOURS, as on paper. Otherwise a bet paying
        config.MIN_PAYOUT_HOURS or more out.
        """
        if not self.by_game_rules(pair):
            return super().pays_in_time(hours, pair)
        return hours <= config.MAX_PAYOUT_HOURS

    def lead(self, yes, no, under_way):
        """
        Polymarket US, with a leg on each venue, on a future as on a game
        under way, from 2026-10-06. Futures sent both legs at once until
        then: Kalshi's orders took something 99.4% of the time and Polymarket
        US's 94%, and of 82 trades left on one leg, 79 held Kalshi's, which
        cost 12.85$ to sell back or stayed exposed.
        """
        return super().lead(yes, no, True)

    def allowance(self, trade):
        """
        config.LIVE_IN_PLAY_ALLOWANCE on a game under way, so Kalshi's order,
        sent once Polymarket US's has filled, may give up that share of the
        edge left to fill, see Executor.follow_limit(). Nothing on a future,
        whose orders sweep only the levels that return config.MIN_ANNUAL_PCT
        a year, see min_edge().
        """
        return config.LIVE_IN_PLAY_ALLOWANCE if trade.in_play else 0.0

    def signal(self, pair, yes, no, edge, size, fee_infos, now):
        """
        Take the signal by the shared rules and live's own, unless live trading has halted.
        """
        if self.halted:
            return False
        return super().signal(pair, yes, no, edge, size, fee_infos, now)

    # ORDERS

    def set_trade_aside(self, trade, order):
        """
        Send no more orders for a trade one of whose orders has an unknown
        outcome, and log which order to look up on the venue, with the ids it
        is found by there.
        """
        if trade.id in self.set_aside:
            return
        self.set_aside[trade.id] = f"set aside, order {order.id} has an unknown outcome"
        venue_id = f", venue order id {order.venue_order_id}" if order.venue_order_id else ""
        self.log(f"live trade {trade.id} set aside: order {order.id}, {order.action} {order.quantity} {order.outcome} of {order.venue} "
                 f"{order.contract_id} at {order.limit_price:.4f}, client id {order.client_id}{venue_id}, has an unknown outcome: "
                 f"{order.note}. Look it up on the venue and flatten the trade by hand if it traded.")

    async def send(self, trade, leg, purpose, action, quantity, price):
        """
        Store an order, with the times of the book it goes out on, send it,
        store the venue's answer, and return it as a Fill. The order trades
        the outcome of the contract the leg holds, see Leg.outcome.
        """
        if self.brakes.stopped or (purpose == "open" and self.halted):
            return Fill(ts=self.clock(), note="not sent, live trading halted")
        outcome = leg.outcome
        book = self.book(leg.key)
        order = Order(trade_id=trade.id, venue=leg.venue, contract_id=leg.contract_id, purpose=purpose, action=action, outcome=outcome,
                      quantity=quantity, limit_price=price, client_id=str(uuid.uuid4()), sent_at=self.clock(),
                      book_at=at_seconds(book.at) if book and book.at is not None else None, book_ts=book.ts if book else None)
        database.insert_order(self.conn, order)
        footprint = self.footprints.sent(leg, action == "sell", price) if self.footprints else None
        started = time.perf_counter()
        try:
            answer = await asyncio.get_running_loop().run_in_executor(
                self.threads, self.place[leg.venue], leg.contract_id, action, outcome, quantity, price, order.client_id)
        except Exception as e:
            answer = orders.unknown(e)          # The venue may have taken it before its answer could not be read.
        order.latency_ms = int((time.perf_counter() - started) * 1000)
        order.answered_at = self.clock()
        order.status, order.venue_order_id, order.filled, order.dollars, order.fees, order.note = (
            answer.status, answer.order_id, answer.filled, answer.dollars, answer.fees, answer.note)
        order.response = jsonutil.dump(answer.response)
        database.update_order(self.conn, order)
        if footprint:
            self.footprints.answer(footprint, leg.venue, answer.response, answer.filled)
        if order.status == "error":
            self.set_trade_aside(trade, order)
        elif order.status == "closed" and self.market_closed:
            self.market_closed(leg.venue, leg.contract_id, order.answered_at)
        self.brakes.watch(order)
        note = f"{answer.status}: {answer.note}" if answer.note else ""
        return Fill(answer.filled, answer.dollars, order.latency_ms, order.answered_at, note, answer.status == "unfunded")

    async def fill(self, trade, leg, when=None):
        return await self.send(trade, leg, "open", "buy", leg.quantity, leg.limit)       # Sent now: when is paper's.

    async def sell_back(self, trade, leg, quantity, floor, when=None):
        return await self.send(trade, leg, "flatten", "sell", quantity, floor)      # Sent now: live flattens as its answers come.

    # RESULTS, which the brakes check whenever a trade may have been decided.

    async def run_trade(self, trade, legs):
        await super().run_trade(trade, legs)
        self.brakes.check_results()

    async def retry(self, now):
        """
        Retry the exposed trades, and check the results only when a sale changed one. The retry runs every tick while
        a trade is exposed, and a check every tick held up the orders queued behind it, see brakes.check_results().
        """
        sold = await super().retry(now)
        if sold:
            self.brakes.check_results()
        return sold

    def settled(self, trade_id):
        super().settled(trade_id)
        self.brakes.check_results()

    def summary(self):
        if self.brakes.stopped:
            return super().summary() + f"; HALTED, no orders at all: {self.brakes.stopped}"
        return super().summary() + (f"; HALTED, still flattening: {self.halted}" if self.halted else "")
