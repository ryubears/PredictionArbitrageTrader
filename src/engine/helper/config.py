"""
The settings that decide what the live process does, in one place.

Each is read when it is used, as config.NAME, so engine.run's --set can
change one for a run, and the run logs them all when it starts. What
belongs here is what a run might be tuned by. What a venue's protocol or
fee schedule fixes stays with the code for that venue. A setting only
paper trading reads starts with PAPER_, one only live trading reads starts
with LIVE_, and the rest hold for both.

Override one for a run with --set, for example:
    python3 -m engine.run --sport nfl --set min_edge=0.03 --set min_annual_pct=20
"""

from common import sports

# TRADING, trading/executor.py

MIN_EDGE = 0.02             # Net dollars per contract at the top before orders are sent, and the floor for the deeper levels they sweep.
                            # MIN_ANNUAL_PCT weighs an edge against the time it ties the money up, so this floor only keeps out the noise
                            # of an edge of a cent or so. Paper's, and the stretch the scanner times. Live has its own: on a future each
                            # level its orders sweep must return MIN_ANNUAL_PCT a year, and on a game under way LIVE_IN_PLAY_MIN_EDGE.
MIN_PAYOUT_HOURS = 24       # The soonest a future, or a bet on a game before it starts, may pay out for live to trade it. A game
                            # under way live trades only with run.py --live-in-play, by MAX_PAYOUT_HOURS.
MAX_PAYOUT_HOURS = 24       # The latest a bet on a game, match, or race may pay out for paper to trade it, before it and while
                            # it is played, and for live to trade it once under way, so both trade the ones on the day.
MIN_ANNUAL_PCT = 100        # The least an edge must return a year on the capital it ties up until the bet pays, before a game or on
                            # a future. A 5 cent edge clears it if it pays within 19 days, 10 cents within 40, and 20 within 91.
                            # Live's future orders sweep only the levels that clear it too, see LiveExecutor.min_edge(). A game under
                            # way is asked none, see Executor.pays_enough().
FILL_SHARE = 1.0            # The share of visible size at a level a trade asks for. All of it: live orders nearly always fill in
                            # full, another taker seldom reaching the level first.
MIN_SALE_SHARE = 0.5        # The least share of what contracts cost a sale to flatten them takes. Below it they are kept, a bet that
                            # may still pay out, rather than given away: a 4 cent contract is not sold at the 1 cent bid.
SALE_RETRY_SECONDS = 60     # After a sale to flatten a trade fills nothing, how long until the trade is tried again. A book can
                            # show a bid an order never reaches, and without the wait such a sale would go to the venue every
                            # second.
FLATTEN_PAST_PAYOUT_HOURS = 24  # How long past its payout time an exposed trade that has not settled is still flattened, see
                            # Executor.retry(). The payout time is an estimate, kickoff plus GAME_HOURS plus SETTLE_HOURS, and a game
                            # can outlast it with its market trading, so a trade is flattened until it settles or its market stops
                            # trading, a game's books being dropped RECORD_HOURS after kickoff and a future's at its close: this only
                            # lets go of one whose contracts never resolve.
# A leg on a venue here trades only once its book is current: newer, by the venues' own clocks, than the other leg's last
# change, or else that change is this many seconds old, time for any reaction to it on this venue to reach us. Polymarket US
# books reach us 85 ms after the venue changes them at the median, 160 at the 90th percentile, so without the wait a price that
# moved on Kalshi sits next to Polymarket US's stale one. Live sends Polymarket US's leg first, so it counts only from the other
# leg's change as of when the edge reached live's least edge: a change that leaves the edge there does not start the wait again,
# see LiveExecutor.opened(). Paper counts from the other leg's last change.
CONFIRM_SECONDS = {"polymarket_us": 0.3}

# PAPER, trading/paper.py and money/paper.py. How paper orders fill, and the paper money.

PAPER_REJECT_PROBABILITY = 0.0  # The share of orders a venue turns away for no reason paper sees. Live orders are turned away only for
                                # lacking the cash or in Kalshi's maintenance, which paper turns away as live does.
# How long an order takes, in milliseconds, as the median and the 90th percentile of a lognormal draw. 'open' and 'flatten'
# are from sending an order to the venue's own time on it, the clock its books are stamped with, for an order opening a
# trade, sent beside the other leg's, and one flattening it, and 'back' from then until the answer reaches us. Measured on
# 1,096 live orders: Kalshi 12 there, 10 at the 10th percentile, and 8 back, 20 in all; Polymarket US 59 there for an
# opening order, 30 at the 10th, its median hour by hour from 34 to 79, but 24 for a sale, and 31 back.
PAPER_ORDER_MS = {
    "kalshi": {"open": (12, 16), "flatten": (12, 16), "back": (8, 9)},
    "polymarket_us": {"open": (59, 136), "flatten": (24, 94), "back": (31, 83)},
}
# How long, by our clock, after an order reaches its venue paper waits for a book the venue made later still to show
# that every change up to it has reached us. Kalshi's books came 12 ms after the venue's time, 15 at the 90th
# percentile, Polymarket US's 85, 160 at the 90th. A book with no later change fills as it last was.
PAPER_FEED_SECONDS = {"kalshi": 0.1, "polymarket_us": 0.3}
PAPER_START_BALANCE = 10000.0   # Paper dollars per venue at the start.

# LIVE, trading/live.py, trading/brakes.py, and money/live.py. A live trade is sized as a paper one is, by the books and the cash
# alone. The brakes are sized for a test with about 100 dollars on each venue.

LIVE_IN_PLAY_MIN_EDGE = 0.02    # The least edge live trades on a game under way, and the floor for the deeper levels its orders sweep
                                # there.
LIVE_HOLD_SECONDS = 0.1         # How long an edge must have stayed at live's least edge or more, unbroken, as the scanner times it,
                                # before live trades it, so only an edge that lasts is taken: on a game under way
                                # LIVE_IN_PLAY_MIN_EDGE, on a future the edge returning MIN_ANNUAL_PCT a year, see
                                # pricing.live_min_edge(). In the in-play test, trades on edges that went on to last a tenth to a half
                                # a second made money, and those on edges ending sooner matched nothing. A Polymarket US leg still
                                # waits for its book to catch up with a Kalshi change made before the edge began, see CONFIRM_SECONDS,
                                # so an edge Kalshi's change opened is taken 0.3s after that change, unless a newer Polymarket US book
                                # still shows it.
LIVE_IN_PLAY_CONTRACTS = 20     # The most contracts a live trade on a game under way asks for. Larger trades seldom fill in full on
                                # both venues, and one where Kalshi fills none of Polymarket US's leg leaves all of it to sell back at
                                # a loss.
LIVE_IN_PLAY_ALLOWANCE = 0.5    # The share of the edge left once Polymarket US's leg has filled, between what it cost and Kalshi's
                                # limit, that Kalshi's order on a game under way may give up to fill: its limit is raised, to the
                                # cent, as far as keeps the rest, fees included, see Executor.follow_limit(). Kalshi's price is often
                                # gone by the time our order comes, and selling back the Polymarket US contracts it leaves unmatched
                                # costs some three quarters of the edge on them. An immediate or cancel order fills at the best price
                                # there is, so the room costs nothing where Kalshi's price has not moved. A future's legs keep their
                                # limits, as every level its orders sweep must return MIN_ANNUAL_PCT.
LIVE_MIN_CONTRACTS = 0.1        # The fewest contracts a live trade opens with, on a future or a game, in hundredths above it, which both
                                # venues take orders and fill in. Many edges offer under a whole contract. Neither venue charges a small
                                # order more: Kalshi rounds its fee to the hundredth of a cent, and Polymarket US rounds a small order's
                                # to nothing.
LIVE_BALANCE_SECONDS = 15   # Between readings of the venues' balances. Under http.IDLE_SECONDS, so each reading also keeps the
                            # venue's kept connection open for the next order: one opened afresh took Polymarket US 11 ms longer.
LIVE_LOW_CASH = 5.0         # Dollars on a live venue, or on one of its shards in LIVE_SHARDS, under which a human is emailed, once until
                            # it is back over. Trades there go on as far as the cash pays for.
LIVE_SHARDS = {"kalshi": {0: 80, 3: 20}}    # The exchange shards a venue splits its cash by that live trading keeps
                                            # cash on, each with its own low cash email, and the whole percent of the
                                            # cash each keeps. Kalshi trades football, hockey, soccer, motorsport, UFC,
                                            # darts, esports, and politics on shard 0, and baseball, basketball, and
                                            # tennis on 3, and tools/kalshi_shards.py splits its cash between them by
                                            # these percents, a shard left out giving up all it has.
LIVE_POSITION_SECONDS = 300 # Between readings of the venues' positions, which are compared with what the live trades hold, so records
                            # gone wrong are logged, see LiveExecutor.check_positions().
LIVE_ORDER_WINDOW = 20      # The newest orders the unknown outcome brake looks at.
LIVE_UNKNOWN_LIMIT = 3      # Orders with an unknown outcome among the newest LIVE_ORDER_WINDOW at which live trading halts.
LIVE_REJECT_LIMIT = 3       # Orders one venue refuses in a row at which live trading halts.
LIVE_RESULT_HOURS = 6       # The sliding window the loss brake looks at, in hours.
LIVE_MAX_LOSS_SHARE = 0.10  # Net loss of the trades decided in the window, as a share of the live money, over which live trading halts.
KEY_CHECK_HOURS = 1         # Between readings of when the Kalshi key's location attestation lapses, see notify.AttestationWatch.
KEY_WARN_HOURS = 48         # How long before it lapses the email goes out, so it comes before the day it does.

# GAMES, game.py, which the recorder, the scanner, and the executor time games by. Game lengths are from the games
# Polymarket US has recorded as finished, the settling time from a live game.

# How long a game is expected to last, kickoff to final whistle: three in four of the sport's games end by then. 168 NFL
# games had a median of 3.11 hours, three in four by 3.24, and 382 college games a median of 3.48, three in four by 3.71.
# 2,399 baseball games had a median of 2.83, three in four by 3.09, 475 hockey games a median of 2.75, three in four by
# 2.88, and 418 basketball games a median of 2.53, three in four by 2.71, and all three get more for playoff games, which
# run longer: three in four of 79 hockey playoff games ended by 3.17, of 96 basketball ones by 2.87. A game's bets are
# taken to pay SETTLE_HOURS after it. The rest are allowances, not measured: a basketball game's, a soccer match's 90
# minutes with half time and stoppage time, a tennis match's best of three or five sets, a UFC fight's walk out and five
# rounds with the card running late, a race's, and an esports match's best of three maps, with the breaks between them
# and a start running late. Dota 2 matches can outlast theirs, see FLATTEN_PAST_PAYOUT_HOURS. Politics has no game, but
# every sport needs a length.
GAME_HOURS = {
    "nfl": 3.25, "ncaaf": 3.75, "mlb": 3.25, "nhl": 3.0, "nba": 3.0, "wnba": 2.75, "ncaab": 2.75,
    **{league: 2.0 for league in sports.SOCCER},
    "tennis": 3.0, "ufc": 1.0, "darts": 1.5, "f1": 2.0, "nascar": 4.0, "politics": 0.0,
    **{title: 3.0 for title in sports.ESPORTS},
}
SETTLE_HOURS = 0.5          # Final whistle to the venues settling. A game's money is back SETTLE_HOURS after it ends.
RECORD_HOURS = 5            # Kickoff to when a game's contracts stop being recorded, whatever their close time says. The kickoff
                            # is Polymarket US's, since Kalshi gives none, and holds for both venues' contracts.

# RECORDING, market/record.py

BOOK_LEVELS = 5             # Price levels kept per side.
FEED_PROCESSES = True       # Run each venue's feed in a process of its own, see market/feeds.py. False runs every feed in the main process.
GAME_WINDOW_DAYS = 7        # Games further out than this are not recorded.
CLOSED_MARKET_SECONDS = 600 # How long a market that turned an order away as not trading is left alone, unless its venue says sooner
                            # that it trades, see Recorder.refuse(). A paused match's market refuses orders, and trading into it
                            # would leave the other leg exposed.
EXCHANGE_STATUS_SECONDS = 30    # Between readings of whether Kalshi's exchange is trading, shard by shard, see market/exchange.py.

# SCANNING, market/scan.py and pricing.py

MAX_BOOK_AGE = 60           # Seconds. A book older than this is neither priced nor traded, see pricing.fresh(): its market may have closed.
LOG_PROFIT_DOLLARS = 10     # Live episodes worth at least this at the peak are logged as they end.

# TIMERS, run.py and money/settle.py

TICK_SECONDS = 1.0          # How often the session ticks, pricing open episodes again and running each desk.
STATUS_SECONDS = 60         # How often a status line is logged.
SUMMARY_SECONDS = 600       # How often each component logs its summary.
SETTLE_CHECK_SECONDS = 30   # Between passes over the open trades whose game has started.
FUTURES_SETTLE_CHECK_SECONDS = 3600     # Between checks of each open trade with no game behind it, before its payout time, spread
                                        # over the passes, see money/settle.py. The venues settle many futures months early.


def override(assignments):
    """
    Apply NAME=VALUE assignments for this run, with names in any case. The
    value is read as the setting's own type, so a setting that is a whole
    number stays one, and one that is true or false takes true or false.
    Raises ValueError for an unknown setting, a value of the wrong type, or
    a setting that is not a single number or true or false.
    """
    for assignment in assignments:
        name, sep, text = assignment.partition("=")
        name = name.strip().upper()
        if not sep or not name.isidentifier() or name not in globals() or not name.isupper():
            raise ValueError(f"unknown setting in {assignment!r}")
        current = globals()[name]
        if isinstance(current, bool):
            value = {"true": True, "false": False}.get(text.strip().lower())
            if value is None:
                raise ValueError(f"{name} needs true or false, not {text.strip()!r}")
        elif not isinstance(current, (int, float)):
            raise ValueError(f"{name} is not a single number and cannot be set from the command line")
        else:
            try:
                value = type(current)(text.strip())
            except ValueError:
                kind = "a whole number" if isinstance(current, int) else "a number"
                raise ValueError(f"{name} needs {kind}, not {text.strip()!r}") from None
        globals()[name] = value
