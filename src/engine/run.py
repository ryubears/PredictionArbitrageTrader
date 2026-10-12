"""
Run the live process: follow the books, scan them, trade on paper or with real money, and settle.

What runs, and where it lives in components/: market/ follows the
venues, trading/ makes the trades, and money/ keeps the cash.

- The feeds, from market/streams.py and market/feeds.py. Each venue's
  connections and books run in a child process, which passes every
  changed book on.
- The recorder, from market/record.py, holds the newest book of every
  paired contract in memory. Books are not stored. While a paper order is
  in flight, every book of its contract also goes on a tape, from
  market/tape.py, so the order can meet the venue's book as it was when a
  live order would have arrived. It also holds which markets are not
  trading, as their venues' feeds say or an order turned away showed, and
  their books are not traded.
- The exchange status, from market/exchange.py, reads whether Kalshi
  trades, shard by shard, and no trade opens with a leg where it has
  stopped.
- The scanner, from market/scan.py, prices each pair a changed book
  belongs to, stores every episode of positive edge in the opportunities
  table, and offers each episode to the desks.
- The attestation watch, from trading/notify.py, which emails a human
  before the Kalshi key's location attestation lapses.
- A Desk for each mode the run trades in, with its own money, executor, and
  settler, and its trades stored with its mode, so paper and live never
  mix. They trade apart: the paper desk the bets on one event, a game, a
  match, or a race, before it and while it is played, that pay within
  MAX_PAYOUT_HOURS, and the live desk the futures, which pay
  MIN_PAYOUT_HOURS or more out, bar the sports given to --not-live.
  On the same signal live's real orders took the contracts paper's
  simulated ones looked for: from 2026-10-04, when live began asking for
  all it saw, paper twins of live trades failed 10 times in 13. Each desk
  still flattens and settles every trade it holds. With --live-in-play
  live also trades the games, matches, and races under way that
  pay within MAX_PAYOUT_HOURS, of every sport, Polymarket US's order
  first, an edge of LIVE_IN_PLAY_MIN_EDGE or more, at most
  LIVE_IN_PLAY_CONTRACTS a trade. Live takes a future's edge, as a game's,
  once it has lasted LIVE_HOLD_SECONDS, the scanner offering a held edge
  again once it has lasted, through Scanner.edge_since() and recheck(),
  and counts a leg's wait for its book to catch up from the other's change
  as the edge began, through Scanner.edge_books(). A future's edge lasting
  past a trade that matched is offered again, through
  Scanner.offer_again(). When paper runs too it trades the same games,
  given back what our live orders took from the books, see
  trading/footprints.py.
  - Paper: trading/paper.py fills against the same books with the paper
    money of money/paper.py.
  - Live: trading/live.py sends real orders with the money the venues
    report through money/live.py, and emails a human through
    trading/notify.py when live trading halts.

The Session ties them together and ticks once a second. Every
CATALOG_MINUTES the catalog of each sport is refreshed in a child process,
fetch then classify then match, and the new pairs' contracts are added to
the running feeds and the closed ones removed, without reconnecting.

One run trades every sport given to --sport, comma separated, or every
sport the catalog knows with --sport all, since the money is one pool: a
second process would spend the same dollars. The catalog holds both the
bets on one event and the futures. A sport given to --not-live has its
futures followed but not traded live, to see how they do before real
money goes on them; by default, or with --not-live none, live trades every
sport's. Bitcoin was left out of the catalog from 2026-10-10.

Run with:
    python3 -m engine.run --sport nfl
    python3 -m engine.run --sport nfl,ncaaf,mlb,nhl,nba
    python3 -m engine.run --sport all --execute live
    python3 -m engine.run --sport all --execute both
    python3 -m engine.run --sport all --execute both --not-live politics
    python3 -m engine.run --sport all --execute live --live-in-play --not-live none
    python3 -m engine.run --sport nfl --seconds 120 --catalog-minutes 0
    python3 -m engine.run --sport nfl --skip-refresh
    python3 -m engine.run --sport nfl --no-scan
    python3 -m engine.run --sport nfl --no-trade
    python3 -m engine.run --sport nfl --execute live
    python3 -m engine.run --sport nfl --execute both --not-live none
    python3 -m engine.run --sport nfl --set min_edge=0.03 --set min_annual_pct=50

For a long run on a laptop, stop the Mac from sleeping while it runs:
    caffeinate -i -s python3 -m engine.run --sport nfl
"""

import argparse
import asyncio
import functools
import subprocess
import sys
import time
from dataclasses import dataclass
from catalog import fetch, pipeline
from common.log import log, with_traceback
from common.paths import ROOT
from common.processes import CONTEXT, set_up_child
from common.timeutil import now_iso
from db import database
from engine.components.market import scan
from engine.components.market.exchange import ExchangeStatus
from engine.components.market.record import Recorder, load_targets
from engine.components.market.streams import Streams
from engine.components.market.tape import Tapes
from engine.components.money import settle
from engine.components.money.live import LiveBalances
from engine.components.money.paper import PaperBalances
from engine.components.trading import notify
from engine.components.trading.footprints import Footprints
from engine.components.trading.live import LiveExecutor
from engine.components.trading.paper import PaperExecutor
from engine.helper import config

CATALOG_MINUTES = 60    # How often the catalog is refreshed and subscriptions updated. Zero disables it.
EXECUTE = {"paper": ("paper",), "live": ("live",), "both": ("live", "paper")}     # What --execute trades in. Live first, so its orders go out first.


@dataclass(frozen=True)
class RunOptions:
    """
    What one run of the live process does, as the command line sets it.
    """
    sports: tuple = ("nfl",)                        # The sports to record and trade, from one pool of money.
    seconds: float = 0                              # Stop after this many seconds, or never when zero.
    catalog_seconds: float = CATALOG_MINUTES * 60   # Between catalog refreshes, or never when zero.
    refresh_at_start: bool = True                   # Refresh the catalog before streaming, when refreshes are on.
    scan: bool = True                               # Price the books and store episodes.
    executors: tuple = ("paper",)                   # The modes that trade the scanner's signals, 'paper' and 'live', when scanning.
    not_live: tuple = ()                            # Sports whose futures the live desk leaves alone.
    live_in_play: bool = False                      # Whether live also trades games under way.


def code_version():
    """
    The commit the process runs, with '-dirty' when files differ from it, or 'unknown' outside a git checkout.
    """
    try:
        return subprocess.run(["git", "describe", "--always", "--dirty"], cwd=ROOT, capture_output=True, text=True,
                              timeout=5).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def book_waits():
    """
    How long both executors let a leg wait for its book to catch up with the other leg's, in words.
    """
    waits = ", ".join(f"{venue} up to {seconds}s" for venue, seconds in config.CONFIRM_SECONDS.items())
    return f"a leg waits for its book to catch up with the other's last change: {waits}"


def trading_settings():
    """
    The settings that decide what both traders do, in one line, so each run's log says what it ran with.
    """
    c = config
    return (f"settings: min edge {c.MIN_EDGE:.2f}$ and {c.MIN_ANNUAL_PCT}% a year, live paying {c.MIN_PAYOUT_HOURS}h or more out "
            f"before a game and on a future, a game paying within {c.MAX_PAYOUT_HOURS}h, legs on two venues, Polymarket US's first, "
            f"or on a future two of one venue with the same rules, fill share {c.FILL_SHARE}, expected game "
            f"{', '.join(f'{sport} {hours}h' for sport, hours in c.GAME_HOURS.items())} + settle {c.SETTLE_HOURS}h; {book_waits()}")


def paper_settings():
    """
    The settings that decide how paper orders fill, in one line.
    """
    c = config
    trips = ", ".join(f"{venue} {times['open'][0]}ms there ({times['flatten'][0]} to flatten) and {times['back'][0]} back"
                      for venue, times in c.PAPER_ORDER_MS.items())
    return (f"paper rejects {c.PAPER_REJECT_PROBABILITY:.0%} of orders, which take {trips}, filling at the venue's book then, waiting "
            f"up to {', '.join(f'{venue} {s}s' for venue, s in c.PAPER_FEED_SECONDS.items())} for it to reach us, start balance "
            f"{c.PAPER_START_BALANCE:,.0f}$")


def in_play_settings():
    """
    What edge live trades on a game under way, when, and how many contracts, in words.
    """
    c = config
    return (f"an edge of {c.LIVE_IN_PLAY_MIN_EDGE:g}$ or more once it has lasted {c.LIVE_HOLD_SECONDS:g}s at "
            f"{c.LIVE_IN_PLAY_MIN_EDGE:g}$ or more, at most {c.LIVE_IN_PLAY_CONTRACTS} contracts a trade")


def live_settings():
    """
    The settings that decide what the live trader does, in one line.
    """
    c = config
    return (f"LIVE TRADING with real money: a future's edge once it has lasted {c.LIVE_HOLD_SECONDS:g}s returning "
            f"{c.MIN_ANNUAL_PCT}% a year, its orders down to the levels returning that, a leg's book waiting only for the other's "
            f"change before the edge began, balances read every "
            f"{c.LIVE_BALANCE_SECONDS}s, email under {c.LIVE_LOW_CASH:,.2f}$ on a venue or shard; halt at {c.LIVE_UNKNOWN_LIMIT} "
            f"unknown outcomes in {c.LIVE_ORDER_WINDOW} orders, {c.LIVE_REJECT_LIMIT} refusals in a row, or a loss over "
            f"{c.LIVE_MAX_LOSS_SHARE:.0%} in {c.LIVE_RESULT_HOURS}h")


class Desk:
    """
    One mode of trading, paper or live: its executor, the money it trades,
    and the settler that pays its trades out. books is a function returning
    the recorder's newest books. markets are the bets it trades, 'events',
    those on one game, match, or race, or 'futures', or None for
    both, and held_out the sports whose futures it leaves alone. tapes are
    the recorder's tapes, which paper orders meet the venues' books on, see
    market/tape.py. in_play has live trade games under way too, and
    footprints keep what live orders took for paper to give back, see
    trading/footprints.py.
    """

    def __init__(self, mode, conn, books, notifier, markets=None, held_out=(), tapes=None, in_play=False, footprints=None):
        self.mode = mode
        self.markets = markets
        self.held_out = tuple(held_out)
        if mode == "paper":
            self.cash = PaperBalances(conn)
            self.executor = PaperExecutor(conn, self.cash, books, log, tapes=tapes, footprints=footprints)
        elif mode == "live":
            self.cash = LiveBalances(log)
            self.executor = LiveExecutor(conn, self.cash, books, log, notifier=notifier, in_play=in_play, footprints=footprints)
        else:
            raise ValueError(f"unknown mode {mode!r}")
        self.settler = settle.Settler(conn, self.cash, log, executor=self.executor)

    def trades(self, pair):
        """
        Whether this desk trades a pair: one on an event, which has a game date, or a future, which has none, as its
        markets say, a future only of a sport it does not hold out.
        """
        event = pair.get("game_date") is not None
        if self.markets is not None and event != (self.markets == "events"):
            return False
        return event or pair["sport"] not in self.held_out

    def signal(self, pair, yes, no, edge, size, fee_infos, now):
        """
        Offer the executor a scanner signal on a pair this desk trades. Returns whether it traded.
        """
        if not self.trades(pair):
            return False
        return self.executor.signal(pair, yes, no, edge, size, fee_infos, now)

    def tick(self, now, clock):
        """
        Read the live balances when due, retry exposed trades, and settle.
        """
        if self.mode == "live":
            self.cash.tick(now, clock)
            self.executor.watch_positions(clock)
        self.executor.tick(now)
        self.settler.tick(now, clock)

    def summaries(self):
        """
        One line per component about what it did since the last summary.
        """
        return [self.executor.summary(), self.settler.summary()]


class Session:
    """
    The recorder and everything that runs on its books, wired together:
    the venue connections, the scanner, and a Desk for each mode it trades
    in. Without a scanner only the books are recorded, and without a desk
    the scanner only stores what it sees. The paper desk trades the bets on
    one event and the live desk the futures, bar the sports in not_live,
    and with live_in_play the events under way too.
    """

    def __init__(self, conn, sports, with_scanner=True, executors=("paper",), not_live=(), live_in_play=False):
        if live_in_play and "live" not in executors:
            raise ValueError("live trading in play needs the live desk")
        self.conn = conn
        self.sports = sports
        self.not_live = tuple(s for s in sports if s in not_live)
        self.live_in_play = live_in_play
        self.notifier = notify.Notifier(conn, log)
        self.attestation = notify.AttestationWatch(conn, self.notifier, log)
        # The executors trade against the recorder's books, which exist once the recorder does, below. The desks trade
        # apart, since on one signal live's real orders take what paper's look for, but for games under way with
        # live_in_play, where paper is given back what live took.
        self.tapes = Tapes()
        footprints = Footprints() if live_in_play and "paper" in executors else None
        rules = {"paper": dict(markets="events", tapes=self.tapes, footprints=footprints),
                 "live": dict(markets=None if live_in_play else "futures", held_out=self.not_live, in_play=live_in_play, footprints=footprints)}
        self.desks = []
        for mode in executors if with_scanner else ():     # Live first, so its orders go out first.
            self.desks.append(Desk(mode, conn, lambda: self.recorder.books, self.notifier, **rules[mode]))
        self.scanner = scan.Scanner(conn, sports, log, [d.signal for d in self.desks], books=lambda: self.recorder.books) if with_scanner else None
        self.recorder = Recorder(conn, self.scanner, self.tapes, log)
        self.exchange = ExchangeStatus(log) if self.desks else None
        for position, desk in enumerate(self.desks):
            ex = desk.executor
            ex.recheck, ex.edge_since, ex.edge_books = self.scanner.recheck, self.scanner.edge_since, self.scanner.edge_books
            ex.offer_again = functools.partial(self.scanner.offer_again, position)
            ex.venue_trading, ex.market_closed = self.exchange.trading, self.recorder.refuse
        self.streams = Streams(self.recorder)
        self.last_status = self.last_summary = time.time()

    def start(self):
        """
        Open the venue connections for everything the catalog says to record.
        """
        if self.desks:
            log(trading_settings())
        if any(d.mode == "live" for d in self.desks):
            log(live_settings())
            live = [s for s in self.sports if s not in self.not_live]
            log(f"live trades the futures of {', '.join(live) or 'no sport'}"
                + (f", not of {', '.join(self.not_live)}" if self.not_live else ""))
            if self.live_in_play:
                log(f"live also trades the games, matches, and races under way of every sport that pay within "
                    f"{config.MAX_PAYOUT_HOURS}h, {in_play_settings()}, Polymarket US's order first and Kalshi's for what it filled, "
                    f"giving up as much as {config.LIVE_IN_PLAY_ALLOWANCE:.0%} of the edge left")
            if not notify.EMAIL_FILE.exists():
                log(f"no email settings in {notify.EMAIL_FILE}, alerts are only logged and stored")
        if any(d.mode == "paper" for d in self.desks):
            log(paper_settings())
            log(f"paper trades the games, matches, and races of every sport that pay within {config.MAX_PAYOUT_HOURS}h, "
                f"in play too")
        targets = load_targets(self.conn, self.sports)
        log("recording " + ", ".join(f"{len(ids)} {venue}" for venue, ids in targets.items()) + " contracts")
        if not any(targets.values()):
            log("nothing to record, run pipeline.py first")
        for venue, contract_ids in targets.items():
            self.streams.start(venue, contract_ids)
            if self.streams.connections(venue) > 1:
                log(f"{venue} needs {self.streams.connections(venue)} connections for {len(contract_ids)} contracts")

    def summaries(self):
        """
        Log one line per component about what it did since the last summary.
        """
        if self.scanner:
            log(self.scanner.summary())
        for desk in self.desks:
            for line in desk.summaries():
                log(line)

    def tick(self):
        """
        One pass of the timer: price the open episodes again, let each desk
        retry and settle, and log the status and summaries when they are due.
        """
        now = now_iso()
        self.recorder.tick(now)
        if self.exchange:
            self.exchange.tick(time.time())
        if self.scanner:
            self.scanner.tick(self.recorder.books, now)
        self.attestation.tick(now, time.time())
        for desk in self.desks:
            desk.tick(now, time.time())
        if self.scanner and time.time() - self.last_summary >= config.SUMMARY_SECONDS:
            self.summaries()
            self.last_summary = time.time()
        if time.time() - self.last_status >= config.STATUS_SECONDS:
            log(self.recorder.status())
            self.last_status = time.time()

    def refreshed(self):
        """
        After a catalog refresh, apply the new targets to the connections and the scanner.
        """
        log(f"subscriptions {self.streams.update(load_targets(self.conn, self.sports))}")
        if self.scanner:
            self.scanner.reload()

    async def close(self):
        """
        Stop the connections, end the open episodes, finish the trades in
        flight and the emails being sent, and log the final summaries.
        """
        await self.streams.stop_all()
        if self.scanner:
            self.scanner.tick({}, now_iso())
        pending = [task for desk in self.desks for task in desk.executor.tasks] + list(self.notifier.tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.summaries()
        log(self.recorder.status())


def refresh_catalog(sports, log):
    """
    Refresh the catalog of each sport in turn and say what came of it, in one
    line. A sport whose refresh fails is logged and keeps the catalog it had,
    and the others refresh all the same.
    """
    results = []
    for sport in sports:
        try:
            results.append(f"{sport}: {pipeline.refresh(sport, log)}")
        except Exception as e:
            log(with_traceback(f"{sport} catalog refresh failed ({e!r}), keeping its stored catalog", e))
            results.append(f"{sport}: failed")
    return "; ".join(results)


def refresh_child(refresh, sports, answer):
    """
    Where a refresh child starts: run refresh(sports, log) and send its line back over answer.
    """
    set_up_child()
    answer.send(refresh(sports, log))


async def refresh_in_child(sports, refresh=refresh_catalog):
    """
    Run refresh(sports, log) in a child process and return its line. A
    refresh briefly holds some 300 MB, all freed when it ends, but a thread
    of this process would not give it back: the C library's malloc keeps
    what each thread frees for that thread, so each refresh that landed on
    another thread added some 50 MB for good. A child returns it all when it
    ends, and its work leaves this process's core to the books. refresh
    must be importable by name, since the child imports it.
    """
    loop = asyncio.get_running_loop()
    answers, answer = CONTEXT.Pipe(duplex=False)
    child = CONTEXT.Process(target=refresh_child, args=(refresh, sports, answer), name="catalog refresh", daemon=True)
    child.start()
    answer.close()              # The child's end, which it holds now.
    ready = loop.create_future()
    loop.add_reader(answers.fileno(), lambda: ready.done() or ready.set_result(None))
    try:
        await ready             # The child's line, or its end closing when it died without one.
        return answers.recv()
    except EOFError:
        await asyncio.to_thread(child.join)
        raise RuntimeError(f"the refresh process ended without an answer, exit code {child.exitcode}") from None
    except asyncio.CancelledError:
        child.kill()            # The run is stopping, and the next run refreshes when it starts.
        raise
    finally:
        loop.remove_reader(answers.fileno())
        answers.close()
        await asyncio.to_thread(child.join)


async def run(conn, options):
    """
    Refresh the catalog, start a Session, tick it every config.TICK_SECONDS,
    and keep the catalog fresh on a timer, as the RunOptions say, each
    refresh in a child process. A refresh that fails is logged and tried
    again at the next interval, so a bad fetch never stops the recording.
    """
    sports, seconds, catalog_seconds = options.sports, options.seconds, options.catalog_seconds
    log(f"starting {', '.join(sports)}, code {code_version()}")
    if catalog_seconds and options.refresh_at_start:
        log("refreshing catalog before starting")
        try:
            log(await refresh_in_child(sports))
        except Exception as e:
            log(with_traceback(f"catalog refresh failed ({e!r}), starting with the stored catalog", e))
    session = Session(conn, sports, options.scan, options.executors, options.not_live, options.live_in_play)
    session.start()
    started = last_catalog = time.time()
    refresh = None      # The background catalog refresh while one is running.
    try:
        while not seconds or time.time() - started < seconds:
            await asyncio.sleep(config.TICK_SECONDS)
            session.tick()
            if catalog_seconds and refresh is None and time.time() - last_catalog >= catalog_seconds:
                refresh, last_catalog = asyncio.create_task(refresh_in_child(sports)), time.time()
            if refresh is not None and refresh.done():
                if refresh.exception():
                    log(with_traceback(f"catalog refresh failed ({refresh.exception()!r}), keeping current subscriptions", refresh.exception()))
                else:
                    log(f"catalog refreshed, {refresh.result()}")
                    session.refreshed()
                refresh = None
    finally:
        if refresh is not None:
            refresh.cancel()
        await session.close()


# MAIN

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Stream the books of paired contracts, scan them, and trade.")
    ap.add_argument("--sport", default="nfl", help=f"the sports to trade, comma separated, from {', '.join(sorted(fetch.SPORTS))}, or all")
    ap.add_argument("--seconds", type=int, default=0, help="stop after this many seconds, 0 means run forever")
    ap.add_argument("--catalog-minutes", type=int, default=CATALOG_MINUTES,
                    help="minutes between catalog refreshes, 0 means never refresh")
    ap.add_argument("--skip-refresh", action="store_true",
                    help="start streaming at once from the stored catalog instead of refreshing first")
    ap.add_argument("--no-scan", action="store_true", help="stream the books only, without the scanner, to check the connections")
    ap.add_argument("--not-live", default="none",
                    help="sports whose futures live leaves alone, comma separated, or none, the default")
    ap.add_argument("--no-trade", action="store_true", help="scan without trading")
    ap.add_argument("--live-in-play", action="store_true",
                    help="with --execute live or both, live also trades games under way, Polymarket US's order first")
    ap.add_argument("--execute", choices=sorted(EXECUTE), default="paper",
                    help="trade on paper, the bets on one event, with real money on the venues, the futures, or both")
    ap.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                    help="override a setting from engine/helper/config.py for this run, for example --set min_edge=0.03, repeatable")
    args = ap.parse_args()
    sports = tuple(fetch.SPORTS) if args.sport.strip() == "all" else tuple(s.strip() for s in args.sport.split(",") if s.strip())
    if not sports or any(s not in fetch.SPORTS for s in sports):
        ap.error(f"--sport takes sports from {', '.join(sorted(fetch.SPORTS))}, or all, not {args.sport!r}")
    not_live = () if args.not_live.strip() == "none" else tuple(s.strip() for s in args.not_live.split(",") if s.strip())
    if any(s not in fetch.SPORTS for s in not_live):
        ap.error(f"--not-live takes sports from {', '.join(sorted(fetch.SPORTS))}, or none, not {args.not_live!r}")
    if args.live_in_play and (args.execute == "paper" or args.no_trade or args.no_scan):
        ap.error("--live-in-play needs --execute live or both, scanning and trading")
    try:
        config.override(args.set)
    except ValueError as e:
        ap.error(str(e))
    options = RunOptions(sports=sports, seconds=args.seconds,
                         catalog_seconds=args.catalog_minutes * 60, refresh_at_start=not args.skip_refresh, scan=not args.no_scan,
                         executors=() if args.no_trade else EXECUTE[args.execute], not_live=not_live, live_in_play=args.live_in_play)
    sys.stdout.reconfigure(line_buffering=True)     # Print immediately even when output goes to a file.
    with database.connect() as conn:
        try:
            asyncio.run(run(conn, options))
        except KeyboardInterrupt:
            print("stopped")
