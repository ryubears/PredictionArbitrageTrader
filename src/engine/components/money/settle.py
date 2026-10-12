"""
Settle trades, paper or live, once their contracts have resolved.

The settler keeps asking the venues how the held contracts of open trades
resolved, from the moment their game kicks off, since Kalshi settles a
prop as soon as it is decided and both venues settle the rest within
half an hour of the final whistle. Each pass costs one Kalshi call per
fifty tickers and one Polymarket US call per event, and the next pass
starts config.SETTLE_CHECK_SECONDS after the previous one began.

A trade with no game behind it, a future, is checked at every pass from
its payout time, and before it on a slower cadence, since the venues
settle many futures months early: an eliminated team's qualifier, an
award for an eliminated player, or season wins once decided. Each is
checked once every config.FUTURES_SETTLE_CHECK_SECONDS, spread over the
passes, so a restart or a batch of new futures adds a few calls to each
pass rather than one burst. Polymarket US costs a call per event against
a limit the live orders share, see due() and polymarket_us.RESULTS_SLEEP.

The venues report what a yes contract paid, from 0 to 1: a dollar or
nothing as a rule, and part of a dollar when the rules pay one, as for a
tie or a match settled at its last fair price. A leg holding yes is paid
that much a contract and one holding no the rest of the dollar, through
the shared Balances, and each leg's result and payout is stored as the
trade's Settlement, see written(). A trade settles only once every held
leg has a result, so a venue that is slow to resolve just delays it.

A trade still exposed on one side may be settled while the executor keeps
trying to flatten it. The two stay apart: the settler skips a trade while
an order to flatten it is in flight, pays out what a trade holds after
the venues answer rather than before, and tells the executor when a trade
has settled so it stops flattening it.
"""

import asyncio
import math
from api import kalshi, polymarket_us
from common.log import with_traceback
from common.periodic import Periodic
from common.timeutil import seconds_between
from db import database
from db.models import Ledger, Settlement
from engine.helper import config

RESULTS = {"kalshi": kalshi.results, "polymarket_us": polymarket_us.results}    # What a yes contract of each venue paid, from 0 to 1.
RESULTS_BY_EVENT = {"kalshi": False, "polymarket_us": True}     # Whether a venue's lookup takes event ids rather than contract ids.


def written(paid):
    """
    How a contract resolved, as a Settlement stores it, from what a yes
    contract paid: 'yes' for a dollar and 'no' for nothing, and part of a
    dollar as the decimal it paid, '0.6' for 60 cents.
    """
    if paid in (0, 1):
        return "yes" if paid == 1 else "no"
    return f"{paid:g}"


class Settler:
    """
    Pays out trades whose contracts have resolved, the trades of the mode
    its cash is for, paper or live.
    results maps a venue to a function giving what its yes contracts paid, see RESULTS.
    executor is the Executor whose exposed trades are kept apart from settlement, if trading.
    """

    def __init__(self, conn, cash, log=print, results=None, executor=None):
        self.conn = conn
        self.cash = cash
        self.mode = cash.mode
        self.log = log
        self.results = results or RESULTS
        self.executor = executor
        self.settled = []           # (Trade, realized dollars) since the last summary.
        self.checked = {}           # Id of each open trade with no game, before its payout time, to when a pass last asked about it.
        self.passes = Periodic(lambda: config.SETTLE_CHECK_SECONDS, log, "settlement pass")

    async def ask_venues(self, trades):
        """
        How the contracts the trades hold resolved, as {(venue, contract_id):
        (paid, settled_at)} for those that have, paid what a yes contract
        paid. A venue whose lookup fails is logged and asked again on the
        next pass.
        """
        wanted = {}
        for t in trades:
            for leg in t.legs():
                if leg.held:
                    wanted.setdefault(leg.venue, set()).add(leg.contract_id)
        results = {}
        for venue, ids in wanted.items():
            lookup = list(database.event_ids(self.conn, venue, list(ids)).values()) if RESULTS_BY_EVENT[venue] else list(ids)
            try:
                found = await asyncio.to_thread(self.results[venue], lookup)
            except Exception as e:
                self.log(with_traceback(f"settlement lookup failed for {venue} ({e!r}), will retry", e))
                continue
            results.update({(venue, cid): r for cid, r in found.items()})
        return results

    def pay_out(self, t, results, now):
        """
        Settle a trade once every leg it holds has a result: pay each leg
        what its contracts paid, what a yes contract paid on a leg holding
        yes and the rest of the dollar on one holding no, store the
        Settlement, and tell the executor.
        """
        held = [leg for leg in t.legs() if leg.held]
        if any(leg.key not in results for leg in held):
            return
        settlement = Settlement(t.id, mode=t.mode, settled_at=max(results[leg.key][1] or now for leg in held))
        paid = []
        for leg in held:
            yes_paid, settled_at = results[leg.key]
            result, payout = written(yes_paid), leg.held * (yes_paid if leg.outcome == "yes" else 1 - yes_paid)
            settlement.record(leg.side, result, payout, settled_at or now)
            if payout:
                self.cash.apply(Ledger(settlement.settled_at, leg.venue, payout, "payout", t.id))
            paid.append((leg, result, payout))
        database.insert_settlement(self.conn, settlement)
        if self.executor:
            self.executor.settled(t.id)
        realized = sum(payout - leg.cost for leg, _, payout in paid)
        self.settled.append((t, realized))
        self.log(f"{self.mode} settled {t.label}: " + ", ".join(
            f"{leg.venue} {leg.side} {result} pays {payout:,.2f}$" for leg, result, payout in paid) + f", realized {realized:+.2f}$")

    def due(self, trades, now):
        """
        The open trades a pass asks about: each whose game has started or,
        with no game, whose payout time has passed, and the turn of those
        with no game still to pay out. Each of those is asked about once
        every config.FUTURES_SETTLE_CHECK_SECONDS, and a pass takes no more
        of them than its share of that time, those asked about longest ago
        first, so that a restart, which forgets when each was asked, spreads
        them over the passes again rather than asking about all at once. One
        whose lookup failed waits for its next turn.
        """
        due = [t for t in trades if (t.starts_at or t.pays_at) <= now]
        early = [t for t in trades if t.starts_at is None and t.pays_at > now]
        self.checked = {t.id: self.checked[t.id] for t in early if t.id in self.checked}  # Forget the trades settled or due at every pass.
        cadence = max(config.FUTURES_SETTLE_CHECK_SECONDS, config.SETTLE_CHECK_SECONDS)
        waiting = [t for t in early if t.id not in self.checked or seconds_between(self.checked[t.id], now) >= cadence]
        share = math.ceil(len(early) * config.SETTLE_CHECK_SECONDS / cadence)
        turn = sorted(waiting, key=lambda t: self.checked.get(t.id, ""))[:share]   # Never asked about first, then the oldest.
        for t in turn:
            self.checked[t.id] = now
        return due + turn

    async def settle(self, now):
        """
        One pass: ask the venues how the contracts of the open trades due a
        check resolved, see due(), and pay out each trade whose held legs all have.
        """
        due = self.due(database.load_open_trades(self.conn, self.mode), now)
        if not due:
            return
        results = await self.ask_venues(due)
        # The executor may have flattened some of these while the venues were asked, so pay out what the trades hold now.
        current = {t.id: t for t in database.load_open_trades(self.conn, self.mode)}
        flattening = self.executor.flattening if self.executor else set()
        for t in (current.get(t.id) for t in due):
            if t is not None and t.id not in flattening:
                self.pay_out(t, results, now)

    def tick(self, now, clock):
        """
        Once a second from the session, with the wall clock in seconds. Starts a pass when one is due and none is running.
        """
        self.passes.tick(clock, lambda: self.settle(now))

    def summary(self):
        """
        One line for the trades settled since the last summary.
        """
        settled = self.settled
        self.settled = []
        open_count = len(database.load_open_trades(self.conn, self.mode))
        return f"{self.mode} settled: {len(settled)} trades for {sum(r for _, r in settled):+.2f}$, {open_count} still open"
