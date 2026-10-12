"""
Paper trade the scanner's signals against the live books, timed as live orders are.

The paper executor pretends to send each order the way a live one goes.
An order takes a trip to its venue and a trip back, each drawn around
what the live orders took, see config.PAPER_ORDER_MS. The trip there ends
at the venue's own time on the order, the clock both venues stamp their
books with, and the order fills against the book the venue had then: the
newest it made by then on the contract's tape, which the recorder keeps
while the trade is in flight, see market/tape.py. Our copy of a book runs
behind the venue's, Kalshi's by some 12 ms and Polymarket US's by some 85,
so the order waits for a book the venue made later still, which shows
that every change up to then has reached us, or config.PAPER_FEED_SECONDS
when none comes. A level still there fills, a level that shrank fills
partly, and a level that is gone does not fill. The answer comes back
after the trip back, and a trade whose legs filled unevenly is flattened
on the books we had seen by then, as live trading would, with a sale that
meets the venue's book when it would arrive. Without tapes, as in tests,
an order fills against our newest book when it would arrive.

It asks for config.FILL_SHARE of what the books show, all of it by
default, and config.PAPER_REJECT_PROBABILITY of its orders are turned away
outright, none by default, as no live order was for no reason paper sees.

A real order takes the contracts it fills, but a paper one leaves the
venue's book as it was, so an edge that stays, or comes back, would be
filled again from the same contracts. So the paper executor remembers what
its orders took from each level and sees every book without it, when it
sizes, fills, and flattens. What it took from a level counts until the
level shrinks below that or goes, as other takers or cancels would have
taken ours too. It learns that when it next looks at the newest book, so
a level gone and back between two looks still has ours taken off, never
more.

Paper trades the games, matches, and races that pay within
config.MAX_PAYOUT_HOURS, in play too. Everything else, the sizing, the
flattening, the order its legs go in, and storing each trade, is the
shared Executor's, and the money is the PaperBalances from money/paper.py.

When live trades the same games in play, run.py --live-in-play, both take
the same signals. Our live orders are real and take from the books paper's
orders meet, so paper adds back what they took, see footprints.py.
"""

import asyncio
import dataclasses
import math
import random
from contextlib import ExitStack, contextmanager
from statistics import NormalDist
from common.timeutil import at_seconds, epoch, now_iso
from common.venues import is_maintenance
from engine.components.trading.executor import Executor, Fill
from engine.helper import config
from engine.helper.pricing import book_level, fresh, ladder, sell_ladder, sweep, takes

Z90 = NormalDist().inv_cdf(0.9)     # Standard deviations from the median to the 90th percentile of a normal draw.


class PaperExecutor(Executor):
    """
    Fills orders against the recorder's books as the venue had them when a live order would have arrived.
    tapes are the recorder's, see market/tape.py, or None to fill against the newest books.
    """

    mode = "paper"
    in_play = True      # Paper trades games, matches, and races, before and while they are played.

    def __init__(self, conn, cash, books, log=print, rng=None, clock=now_iso, is_maintenance=is_maintenance, tapes=None, footprints=None):
        super().__init__(conn, cash, books, log, clock, is_maintenance)
        self.rng = rng or random.Random()
        self.tapes = tapes
        self.footprints = footprints    # What our live orders took from the books, given back, or None without live trading.
        self.taken = {}             # (venue, contract_id) maps to {(book side, price): contracts our orders took from that level}.

    def pays_in_time(self, hours, pair):
        """
        Paper trades a bet paying out within config.MAX_PAYOUT_HOURS.
        """
        return hours <= config.MAX_PAYOUT_HOURS

    # BOOKS

    def less_ours(self, key, book, newest=False):
        """
        A book less what our orders took from it. A level in the newest book
        smaller than what we took, or gone, has lost ours with the rest, so
        what we took is kept down to it, and forgotten once it is gone.
        """
        ours = self.taken.get(key)
        if book is None or not ours:
            return book
        still = {}                  # What we took from the levels still there, no more than each holds now.
        sides = {}
        for side in ("bids", "asks"):
            sides[side] = []
            for price, size in getattr(book, side):
                level = (side, round(price, 4))
                mine = min(ours.get(level, 0), size)
                if mine > 0:
                    still[level] = mine
                if size > mine:
                    sides[side].append([price, size - mine])
        if newest:
            if still:
                self.taken[key] = still
            else:
                del self.taken[key]
        return dataclasses.replace(book, **sides)

    def give_back(self, key, book, tape):
        """
        A book with what our live orders took from it added back, see footprints.py.
        """
        return self.footprints.give_back(key, book, tape) if self.footprints else book

    def book(self, key, when=None):
        """
        A contract's newest book, or with when the newest that had reached us
        by then, from its tape when one is kept, less what our orders took.
        """
        tape = self.tapes.get(key) if self.tapes and when else None
        if tape:
            return self.less_ours(key, self.give_back(key, tape.seen(epoch(when)), tape))
        return self.less_ours(key, super().book(key), newest=True)

    @contextmanager
    def taping(self, keys):
        """
        Keep a tape of each contract's books while the with block runs, when there are tapes to keep.
        """
        with ExitStack() as stack:
            for key in keys if self.tapes else ():
                stack.enter_context(self.tapes.follow(key, self.books().get(key)))
            yield

    async def until(self, moment):
        """
        Wait until moment, our clock in seconds.
        """
        await asyncio.sleep(max(0.0, moment - epoch(self.clock())))

    async def at_venue(self, key, venue, arrived, aging):
        """
        The book an order arriving at arrived, the venue's clock in seconds,
        meets, less what our orders took, or None when there is none fresh
        enough to trade, once every book the venue made by then has reached
        us or config.PAPER_FEED_SECONDS have passed.
        """
        tape = self.tapes.get(key) if self.tapes else None
        if tape:
            await tape.wait(arrived, arrived + config.PAPER_FEED_SECONDS[venue] - epoch(self.clock()))
            if self.footprints:
                await self.footprints.known(key, arrived)
            book = self.less_ours(key, self.give_back(key, tape.at(arrived), tape))
        else:
            await self.until(arrived)
            book = self.book(key)
        return book if fresh(book, self.clock(), aging) else None

    # ORDERS

    def take(self, leg, levels, quantity, limit=None, selling=False):
        """
        Fill an order for quantity of a leg from levels, the ladder made from
        its book, or the sell_ladder() when selling, as pricing.sweep() does,
        and remember what it took from each level of the book. Returns
        (contracts, dollars).
        """
        ours = self.taken.setdefault(leg.key, {})
        for price, contracts in takes(levels, quantity, config.FILL_SHARE, limit, selling=selling):
            level = book_level(leg.polarity, leg.side, price, selling)
            ours[level] = ours.get(level, 0) + contracts
        return sweep(levels, quantity, leg.venue, leg.fee_info, config.FILL_SHARE, limit=limit, selling=selling)

    def draw(self, median, p90):
        """
        Milliseconds drawn from a lognormal with this median and 90th percentile.
        """
        sigma = math.log(p90 / median) / Z90 if p90 > median else 0.0
        return self.rng.lognormvariate(math.log(median), sigma)

    def trip(self, venue, purpose):
        """
        Seconds an order to open or flatten, its purpose, takes to reach its
        venue, by the venue's clock, and for its answer to come back.
        """
        times = config.PAPER_ORDER_MS[venue]
        return self.draw(*times[purpose]) / 1000, self.draw(*times["back"]) / 1000

    async def order(self, trade, leg, purpose, quantity, limit=None, when=None):
        """
        Send an order for a leg, to open a trade by buying quantity at no more
        than limit, or to flatten it by selling quantity at whatever the book
        offers, at when, by our clock, or now. Returns its Fill once its
        answer would have come back.
        """
        sent = epoch(when) if when else epoch(self.clock())
        there, back = self.trip(leg.venue, purpose)
        answered = sent + there + back
        ms, ts = round(1000 * (there + back)), at_seconds(answered)
        if self.rng.random() < config.PAPER_REJECT_PROBABILITY:
            await self.until(answered)
            return Fill(ms=ms, ts=ts, note="rejected")
        with self.taping([leg.key]):
            book = await self.at_venue(leg.key, leg.venue, sent + there, self.aging(trade))
        filled, dollars, selling = 0, 0.0, purpose == "flatten"
        if book is not None:
            levels = sell_ladder(book, leg.polarity, leg.side) if selling else ladder(book, leg.polarity, leg.side)
            filled, dollars = self.take(leg, levels, quantity, limit, selling)
        await self.until(answered)
        return Fill(filled, dollars, ms, ts, "no book" if book is None and not selling else "")

    async def fill(self, trade, leg, when=None):
        """
        Send one leg's buy order, at when or now, and fill it against the book the venue had when it would arrive.
        """
        return await self.order(trade, leg, "open", leg.quantity, leg.limit, when)

    async def sell_back(self, trade, leg, quantity, floor, when=None):
        """
        Sell back contracts held through a leg at whatever the venue's book offers when the order would arrive, whatever the floor.
        """
        return await self.order(trade, leg, "flatten", quantity, when=when)

    async def run_trade(self, trade, legs):
        """
        Fill and flatten a trade as the shared executor does, with its
        contracts' books on tape from now until it is done, so its orders can
        meet the venues' books when they would arrive and its flatten the
        books we had seen when the answers would have come.
        """
        with self.taping([leg.key for leg in legs]):
            await super().run_trade(trade, legs)
