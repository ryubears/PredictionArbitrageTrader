"""
Find arbitrage episodes in pairs as the recorder's books change.

Whenever a member's book changes, the scanner finds the cheapest way to
hold yes and the cheapest way to hold no across the pair's members, on two
different venues, or on a future two contracts of one venue with the same
rules, and prices buying both. An episode is a stretch where that net edge
stays above zero after fees. Each episode becomes an Opportunity with its
two legs, its duration, its peak edge, how many contracts could have been
filled at the peak by walking the books' depth, and the return on the
capital tied up, annualized as if held until the bet pays out. A nested
pair, yes on one line of a bet and no on a higher one, see
catalog/nest.py, is priced as any other, each member holding only its own
side, see pricing.best_trade().

Within an episode the edge worth trading, config.MIN_EDGE or more, may
come and go. The Opportunity also keeps the longest unbroken stretch of it,
and the contracts that stayed fillable at that edge through all of it,
which is what an order sent any time in the stretch could have had, and
so what an edge that lasts is worth. Live takes it in one order, though,
and its own fill empties the levels it takes, so the Opportunity keeps
too what one order could have had as live takes it: the contracts
fillable on the levels at live's least edge or more, see
pricing.live_min_edge(), and what they lock in, and whether that moment
came with a change of the Polymarket US leg's book. It is the best of the
moments at the peak's edge, and of every moment while none of those had
offered config.LIVE_MIN_CONTRACTS, the fewest live opens a trade with: a
peak's first moment can offer less, where live trades a moment later at
the same edge or a little less. Only the moments once the edge has stayed
at live's least edge or more for config.LIVE_HOLD_SECONDS count, since
live trades it only then.

The episode also follows that stretch at live's least edge, see
edge_since(), and keeps when each member's book had last changed as it
began, see edge_books(): live counts a Polymarket US leg's wait for its
book to catch up from the other leg's change as of then, so a change that
leaves the edge where live takes it does not start the wait again.

The recorder drives the Scanner with the books it holds in memory and the
Scanner stores every episode as it ends, so the opportunities table is the
log of everything it saw. The summary script reads it. The pricing itself
lives in pricing.py. A member whose market is not trading, as its venue
says or an order it turned away showed, is left out, as a stale book is,
and so is a future's member past its close time, so an edge on a market
that cannot trade is never offered. So is a member whose venue is in its
weekly maintenance, see common/venues.py: its book may freeze, or go on
changing, while no order can trade at it, so an episode open as the window
begins ends then, and none opens on the pair until it is over.

A desk that turns an edge down only to wait for a book to catch up, or
for the edge to last, see edge_since(), asks for the pair again once the
wait ends, through recheck(), so the edge is offered then rather than at
the next change or tick, up to a second later.
"""

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from common.timeutil import now_iso, seconds_between
from common.venues import is_maintenance
from db import database
from db.models import Opportunity
from engine.helper import config
from engine.helper.game import days_until, past_close, pays_at as payout_time, started
from engine.helper.pricing import (Priced, annual_pct, best_trade, changed_at, fillable, fresh, live_hold, live_min_edge,
                                   polymarket_us_just_changed, return_pct, trade_words)

RECHECK_MARGIN = 0.005      # Seconds past a wait's end that a recheck prices the pair, so the wait is surely over by our clock.


def opens_live(size):
    """
    Whether size contracts are enough for live to open a trade with, config.LIVE_MIN_CONTRACTS or more, give or take
    float residue.
    """
    return size >= config.LIVE_MIN_CONTRACTS - 1e-9


@dataclass
class Episode:
    """
    One pair's stretch of positive net edge, while the scanner follows it.
    """
    pair: dict              # The pair, as the scanner loaded it.
    start_ts: str           # When the edge went positive.
    peak: Priced            # The best moment so far.
    peak_ts: str            # When the best moment was.
    take: tuple = (0.0, 0.0)        # Contracts one order could have had at live's least edge or more, and their profit, see
                                    # Scanner.weigh_take().
    pm_changed: bool = False        # Whether that moment came with a change of the Polymarket US leg's book.
    take_rank: tuple | None = None  # How good that moment was, see Scanner.weigh_take(), or None before any.
    taken: set = field(default_factory=set)     # Which of the scanner's on_signals took a trade on it, by position.
    again_ts: str | None = None         # When a desk last gave it back after a future's trade on it was done, see
                                        # Scanner.offer_again(), or None.
    worth_since: str | None = None      # When the current stretch at config.MIN_EDGE or more began, or None outside one.
    worth_least: tuple = (0.0, 0.0)     # The fewest contracts fillable at that edge so far in the stretch, and their profit.
    worth_best: tuple | None = None     # The longest stretch so far, as (seconds, contracts, profit), a moment's included.
    live_since: str | None = None       # When the current stretch at live's least edge or more began, by our clock, or None outside
                                        # one, see Scanner.live_floor().
    live_books: dict = field(default_factory=dict)  # When each member's book had last changed as that stretch began, by
                                                    # (venue, contract id), see pricing.changed_at().

    def end_stretch(self, now):
        """
        Close the current stretch at config.MIN_EDGE or more at now, keeping it when it is the longest.
        """
        if self.worth_since is None:
            return
        seconds = seconds_between(self.worth_since, now)
        if self.worth_best is None or seconds > self.worth_best[0]:
            self.worth_best = (seconds, *self.worth_least)
        self.worth_since = None

    def see(self, priced, now):
        """
        Follow the stretches at config.MIN_EDGE or more through one pricing of the pair.
        """
        if priced is not None and priced.edge >= config.MIN_EDGE:
            least = (priced.min_edge_size, priced.min_edge_profit)
            if self.worth_since is None:
                self.worth_since, self.worth_least = now, least
            elif least[0] < self.worth_least[0]:
                self.worth_least = least
        else:
            self.end_stretch(now)

    def see_live(self, worth, books, now):
        """
        Follow the stretch at live's least edge or more through one pricing
        of the pair, worth saying whether the edge is there: when it began,
        and when each member's book had last changed then, which later
        changes in the stretch leave as they were.
        """
        if not worth:
            self.live_since, self.live_books = None, {}
        elif self.live_since is None:
            self.live_since = now
            for m in self.pair["members"]:
                key = (m["venue"], m["contract_id"])
                if books.get(key) is not None:
                    self.live_books[key] = changed_at(books[key])

    def opportunity(self, end_ts):
        """
        The episode as an Opportunity, ended at end_ts.
        """
        self.end_stretch(end_ts)
        worth_seconds, worth_size, worth_profit = self.worth_best or (0.0, 0.0, 0.0)
        start_time = next((m["start_time"] for m in self.pair["members"] if m["start_time"]), None)
        live = 1 if start_time and self.peak_ts >= start_time else 0
        # Capital is locked until the slower of the two legs pays, so the later resolution counts.
        pays_at = payout_time((self.peak.yes, self.peak.no), self.pair["sport"])
        days_held = days_until(self.peak_ts, pays_at)
        return Opportunity(
            pair_id=self.pair["id"],
            trade=trade_words(self.peak.yes, self.peak.no),
            yes_venue=self.peak.yes["venue"],
            yes_contract=self.peak.yes["contract_id"],
            no_venue=self.peak.no["venue"],
            no_contract=self.peak.no["contract_id"],
            start_ts=self.start_ts,
            end_ts=end_ts,
            seconds=seconds_between(self.start_ts, end_ts),
            peak_ts=self.peak_ts,
            peak_edge=self.peak.edge,
            peak_size=self.peak.size,
            peak_profit=self.peak.profit,
            live=live,
            days_held=days_held,
            return_pct=return_pct(self.peak.edge),
            annual_pct=annual_pct(self.peak.edge, days_held) if days_held else None,
            min_edge_seconds=worth_seconds,
            min_edge_size=worth_size,
            min_edge_profit=worth_profit,
            take_size=self.take[0],
            take_profit=self.take[1],
            pm_changed=int(self.pm_changed),
        )


class Scanner:
    """
    Tracks episodes for the pairs of the sports from a map of the newest
    books, keyed by (venue, contract_id). Only the pairs a contract belongs
    to are priced when its book changes. A member is left out while its book
    is older than config.MAX_BOOK_AGE or missing from the map, which is how the
    recorder says a venue's books went unseen, and while its venue is in its
    weekly maintenance, as is_maintenance, (venue, ISO 8601 UTC), says, see
    common/venues.py. Pairs and fee schedules come from the database and are
    reloaded after each catalog refresh. Every finished episode is stored at
    once, and the big ones are logged. Each of on_signals, one per executor,
    is offered every moment of an episode until it takes a trade, and then
    not again, see trading/executor.py, unless its executor gives the episode
    back once a future's trade on it is done, see offer_again().
    """

    def __init__(self, conn, sports, log=print, on_signals=(), books=None, is_maintenance=is_maintenance):
        self.conn = conn
        self.sports = sports
        self.log = log
        self.on_signals = list(on_signals)  # Each is called with the trade to make until it takes one in the episode.
        self.books = books                  # Returns the recorder's books, for a recheck, which no book change brings.
        self.is_maintenance = is_maintenance    # Whether a venue is in its weekly maintenance at a time, see common/venues.py.
        self.rechecks = {}                  # Pair id maps to the timer that prices it again once a desk's wait ends.
        self.episodes = {}                  # Pair id maps to its open Episode.
        self.finished = []                  # (kind, Opportunity) for episodes ended since the last summary.
        self.reload()

    def close(self, pair_id, now):
        """
        End an episode, store it, and log it when it was worth something.
        """
        episode = self.episodes.pop(pair_id)
        timer = self.rechecks.pop(pair_id, None)
        if timer is not None:
            timer.cancel()
        o = episode.opportunity(now)
        database.insert_opportunities(self.conn, [o])
        self.finished.append((episode.pair["kind"], o))
        if o.peak_profit >= config.LOG_PROFIT_DOLLARS:
            self.log(f"episode {episode.pair['label']}: {o.trade}, {100 * o.peak_edge:.1f}c x {o.peak_size:.0f} = {o.peak_profit:.2f}$, lasted {o.seconds:.1f}s, "
                     f"{100 * config.MIN_EDGE:.0f}c or more for {o.min_edge_seconds:.1f}s with {o.min_edge_size:.0f} contracts throughout")

    def reload(self):
        """
        Load the pairs and fee schedules again, ending open episodes of pairs that are gone.
        """
        self.fee_infos, self.pairs = defaultdict(dict), {}
        for sport in self.sports:
            self.fee_infos.update(database.load_fee_infos(self.conn, sport))
            self.pairs.update(database.load_pairs(self.conn, sport))
        by_contract = defaultdict(list)
        for pair_id, pair in self.pairs.items():
            for m in pair["members"]:
                by_contract[(m["venue"], m["contract_id"])].append(pair_id)
        self.by_contract = dict(by_contract)
        for pair_id in [pair_id for pair_id in self.episodes if pair_id not in self.pairs]:
            self.close(pair_id, now_iso())

    def price(self, pair, books, now):
        """
        The best trade across the pair's members whose books are fresh, as a Priced, or None. Books age only
        once their game may have started, see game.started(), and a halted one, whose market is not trading,
        counts as none, see pricing.fresh(). A future's member past its close time is left out, see
        game.past_close(), and so is any member while its venue is in its weekly maintenance, see
        common/venues.py. A future's legs may be two contracts of one venue with the same rules, see
        pricing.best_trade().
        """
        aging = started(pair["game_date"], pair["members"], now)
        members = [m for m in pair["members"]
                   if fresh(books.get((m["venue"], m["contract_id"])), now, aging) and not past_close(pair["game_date"], m, now)
                   and not self.is_maintenance(m["venue"], now)]
        if len(members) < 2:
            return None
        return best_trade(members, books, self.fee_infos, one_venue=pair["game_date"] is None)

    def live_floor(self, pair, priced, now):
        """
        Live's least edge on the pair at now, see pricing.live_min_edge(): on a game under way, once it may have started,
        a set one, otherwise the edge returning its rate a year until the legs pay.
        """
        under_way = started(pair["game_date"], pair["members"], now)
        return live_min_edge(under_way, days_until(now, payout_time((priced.yes, priced.no), pair["sport"])))

    def weigh_take(self, episode, priced, books, floor, now):
        """
        What an order sent at now could have had as live takes it, kept on
        the episode when it is the best yet: the contracts fillable on the
        levels at live's least edge on the pair, floor, or more, see
        live_floor(), the net dollars they lock in, and whether the pricing
        came with a change of the Polymarket US leg's book, see
        pricing.polymarket_us_just_changed(). The best has
        config.LIVE_MIN_CONTRACTS or more, the fewest live opens a trade with,
        then the most profit. A moment counts only once the episode's edge has
        stayed at live's least edge or more for config.LIVE_HOLD_SECONDS, as
        live waits for it to, see pricing.live_hold().
        """
        if episode.live_since is None or live_hold(episode.live_since, now) > 0:
            return
        size, profit = fillable(priced.yes, priced.no, books, self.fee_infos, floor)
        pm_changed = polymarket_us_just_changed(priced.yes, priced.no, books, now)
        rank = (opens_live(size), profit)
        if episode.take_rank is None or rank > episode.take_rank:
            episode.take, episode.pm_changed, episode.take_rank = (size, profit), pm_changed, rank

    def update(self, pair_id, books, now):
        """
        Open, extend, or end the episode for one pair from the current books.
        """
        pair = self.pairs[pair_id]
        priced = self.price(pair, books, now)
        episode = self.episodes.get(pair_id)
        if priced is not None and priced.edge > 0:
            if episode is None:
                episode = self.episodes[pair_id] = Episode(pair, now, priced, now)
            elif priced.edge > episode.peak.edge:
                episode.peak, episode.peak_ts = priced, now
            episode.see(priced, now)
            floor = self.live_floor(pair, priced, now)
            episode.see_live(priced.edge >= floor, books, now)
            # What one order could have had, worked out at the peak's edge, and at every moment until the fewest contracts
            # live opens a trade with were on offer, not on every pricing.
            if priced.edge >= episode.peak.edge or not opens_live(episode.take[0]):
                self.weigh_take(episode, priced, books, floor, now)
            for i, on_signal in enumerate(self.on_signals):
                if i not in episode.taken and on_signal(pair, priced.yes, priced.no, priced.edge, priced.size, self.fee_infos, now):
                    episode.taken.add(i)
        elif episode is not None:
            self.close(pair_id, now)

    def edge_since(self, pair_id):
        """
        When the pair's open episode last reached live's least edge, by our
        clock, see live_floor(), if it has stayed there since, or None. A
        desk that trades an edge only once it has lasted asks, see
        Executor.hold(). Once a desk has given the episode back, see
        offer_again(), it counts from then, so the edge must last as long
        again after the trade.
        """
        episode = self.episodes.get(pair_id)
        if episode is None or episode.live_since is None:
            return None
        if episode.again_ts is not None and seconds_between(episode.live_since, episode.again_ts) > 0:
            return episode.again_ts
        return episode.live_since

    def edge_books(self, pair_id):
        """
        When each member's book of the pair had last changed as its open
        episode last reached live's least edge, by (venue, contract id), see
        pricing.changed_at(), or nothing outside that stretch. Live counts a
        leg's wait for its book to catch up from the other's, see
        LiveExecutor.opened().
        """
        episode = self.episodes.get(pair_id)
        return dict(episode.live_books) if episode else {}

    def reprice(self, pair_id):
        """
        A recheck come due: price the pair again if its episode is still open.
        """
        self.rechecks.pop(pair_id, None)
        if pair_id in self.episodes:
            self.update(pair_id, self.books(), now_iso())

    def recheck(self, pair_id, seconds):
        """
        Price a pair again in seconds, when a desk waits that long for a
        book to catch up, see Executor.confirm_wait(), or for its edge to
        last, see Executor.hold(). A pair keeps one timer, the soonest asked
        for. Outside a running loop, and without books, the next change or
        tick prices it instead.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self.books is None:
            return
        due = loop.time() + seconds + RECHECK_MARGIN
        timer = self.rechecks.get(pair_id)
        if timer is not None:
            if timer.when() <= due:
                return
            timer.cancel()
        self.rechecks[pair_id] = loop.call_at(due, self.reprice, pair_id)

    def offer_again(self, desk, pair_id, signal_ts, now):
        """
        Offer the desk at position desk the pair's episode again: its trade
        on it, signalled at signal_ts, is done at now, see
        Executor.again(), so an edge lasting past a future's trade is
        traded again. Nothing changes when the episode has ended since, a
        later one being its own. The edge counts from now for the desk's
        wait, see edge_since(), and the pair is priced again once that is
        over, as no book may change to bring it.
        """
        episode = self.episodes.get(pair_id)
        if episode is None or seconds_between(episode.start_ts, signal_ts) < 0:
            return
        episode.taken.discard(desk)
        episode.again_ts = now
        self.recheck(pair_id, config.LIVE_HOLD_SECONDS)

    def on_book(self, venue, contract_id, books, now):
        """
        Price every pair this contract belongs to.
        """
        for pair_id in self.by_contract.get((venue, contract_id), ()):
            self.update(pair_id, books, now)

    def tick(self, books, now):
        """
        Price every open episode again, so ones whose books went stale or unseen end.
        """
        for pair_id in list(self.episodes):
            self.update(pair_id, books, now)

    def summary(self):
        """
        One line per kind for the episodes ended since the last summary, then forget them.
        """
        by_kind = defaultdict(list)
        for kind, o in self.finished:
            by_kind[kind].append(o)
        parts = []
        for kind, os in sorted(by_kind.items()):
            best = max(os, key=lambda o: o.peak_profit)
            beat = sum(1 for o in os if o.annual_pct is not None and o.annual_pct >= config.MIN_ANNUAL_PCT)
            parts.append(f"{kind} {len(os)} episodes, {beat} beat target, best {best.peak_profit:.2f}$ for {best.seconds:.0f}s")
        self.finished = []
        return f"scanner: {'; '.join(parts) if parts else 'no episodes'}; {len(self.episodes)} open"
