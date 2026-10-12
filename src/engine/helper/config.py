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
                            # Five cents to 2026-10-04. MIN_ANNUAL_PCT weighs an edge against the time it ties the money up, so this
                            # floor only keeps out the noise of an edge of a cent or so. Paper's, and the stretch the scanner times.
                            # Live has its own from 2026-10-05: on a future each level its orders sweep must return MIN_ANNUAL_PCT a
                            # year, and on a game under way LIVE_IN_PLAY_MIN_EDGE.
MIN_PAYOUT_HOURS = 24       # The soonest a future, or a bet on a game before it starts, may pay out for live to trade it. A game
                            # under way live trades only with run.py --live-in-play, by MAX_PAYOUT_HOURS.
MAX_PAYOUT_HOURS = 24       # The latest a bet on a game, match, or race may pay out for paper to trade it, before it and while
                            # it is played, and for live to trade it once under way, so both trade the ones on the day.
MIN_ANNUAL_PCT = 100        # The least an edge must return a year on the capital it ties up until the bet pays, before a game or on
                            # a future. A 5 cent edge clears it if it pays within 19 days, 10 cents within 40, and 20 within 91. 50
                            # to 2026-10-06, when live's cash ran short and the money was kept for the better trades.
                            # Live's future orders sweep only the levels that clear it too, see LiveExecutor.min_edge(). A game under
                            # way is asked none from 2026-10-05, see Executor.pays_enough().
FILL_SHARE = 1.0            # The share of visible size at a level a trade asks for. At 0.5 to 2026-10-03, 222 of 231 live trades were
                            # sized by it, not the cash, every Kalshi order filled in full, Polymarket US orders of 10 or more filled
                            # in full 45 times in 46, and another taker bought the same side at our price within 10 s of 4 of 140.
MIN_SALE_SHARE = 0.5        # The least share of what contracts cost a sale to flatten them takes. Below it they are kept, a bet that
                            # may still pay out, rather than given away: a 4 cent contract is not sold at the 1 cent bid.
SALE_RETRY_SECONDS = 60     # After a sale to flatten a trade fills nothing, how long until the trade is tried again. A book can
                            # show a bid an order never reaches, and on 2026-10-01 such a sale went to Kalshi every second.
FLATTEN_PAST_PAYOUT_HOURS = 24  # How long past its payout time an exposed trade that has not settled is still flattened, see
                            # Executor.retry(). The payout time is an estimate, kickoff plus GAME_HOURS plus SETTLE_HOURS, and
                            # a game can outlast it with its market trading. To 2026-10-10 a trade was given up at it: live trade
                            # 4135, a Dota 2 map signalled on 2026-10-09 at 20:26 UTC, 56 minutes past its own 19:30, sold back
                            # 25 of the 50 Polymarket US filled over Kalshi's none, and the other 25, 14.43$, were never tried
                            # again, though the pair traded again at 20:43 and 20:45. From 2026-10-05, 6 of 19 Dota 2 trades in
                            # play, 1 of 8 LoL and 2 of 154 tennis were signalled past their own. A trade is now flattened until
                            # it settles or its market stops trading, a game's books being dropped RECORD_HOURS after kickoff and
                            # a future's at its close: this only lets go of one whose contracts never resolve.
# A leg on a venue here trades only once its book is current: newer, by the venues' own clocks, than the other leg's last
# change, or else that change is this many seconds old, time for any reaction to it on this venue to reach us. Polymarket US
# books reached us 85 ms after the venue changed them at the median, 160 at the 90th percentile, so a price that moved on
# Kalshi sat next to Polymarket US's old one, and on 2026-09-28 only 4 of 72 orders there filled. Then both legs went out at
# once, and a missed Polymarket US order left Kalshi's to sell back. Live sends Polymarket US's first, so from 2026-10-07, at
# the user's asking, it counts only from the other leg's change as of when the edge reached live's least edge: a change that
# leaves the edge there does not start the wait again, see LiveExecutor.opened(). Paper counts from the other leg's last change.
CONFIRM_SECONDS = {"polymarket_us": 0.3}

# PAPER, trading/paper.py and money/paper.py. How paper orders fill, and the paper money.

PAPER_REJECT_PROBABILITY = 0.0  # The share of orders a venue turns away for no reason paper sees. Of 1,183 live orders to 2026-10-04 none
                                # was: 3 lacked the cash and 3 came in Kalshi's maintenance, which paper turns away as live does.
# How long an order takes, in milliseconds, as the median and the 90th percentile of a lognormal draw. 'open' and 'flatten'
# are from sending an order to the venue's own time on it, the clock its books are stamped with, for an order opening a
# trade, sent beside the other leg's, and one flattening it, and 'back' from then until the answer reaches us. From the
# 1,096 live orders 2026-10-01 to 10-04 with the venue's time: Kalshi 12 there, 10 at the 10th percentile, and 8 back,
# 20 in all; Polymarket US 59 there for an opening order, 30 at the 10th, its median hour by hour from 34 to 79, but
# 24 for a sale, and 31 back.
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
                                # there: five cents from 2026-10-05, two, as MIN_EDGE, from 2026-10-06, at the user's asking.
LIVE_HOLD_SECONDS = 0.1        # How long an edge must have stayed at live's least edge or more, unbroken, as the scanner times it,
                                # before live trades it, so only an edge that lasts is taken: on a game under way
                                # LIVE_IN_PLAY_MIN_EDGE, on a future the edge returning MIN_ANNUAL_PCT a year, see
                                # pricing.live_min_edge(). At the user's asking: games' alone, half a second, from 2026-10-06, a
                                # tenth from 2026-10-07, and futures', taken at once until then, too from that day's second build. In
                                # eleven hours the half second let through 12 episodes with a whole contract left, of 2,069 that
                                # lasted it, the rest fractions of one, while 205 more with a contract or more ended between a
                                # tenth and a half; in the in-play test, trades sent Polymarket US's order first on edges that
                                # went on to last a tenth to a half a second made 1.92$ and those on edges ending sooner matched
                                # nothing. A Polymarket US leg still waits for its book to catch up with a Kalshi change made
                                # before the edge began, see CONFIRM_SECONDS, so an edge Kalshi's change opened is taken 0.3s
                                # after that change, unless a newer Polymarket US book still shows it. From 2026-10-05 to 10-06
                                # live took a game's edge only on the signal a change of the Polymarket US leg's book brought,
                                # which in seven hours let one episode through.
LIVE_IN_PLAY_CONTRACTS = 20     # The most contracts a live trade on a game under way asks for: 5 from 2026-10-05, for 200 trades,
                                # 10 from 2026-10-06, for as many as come, 50 from 2026-10-08, 100 from 2026-10-10 13:01 UTC, and 20
                                # from that evening, at the user's asking. At 10, the 4 of 46 capped trades that filled had 330
                                # contracts on offer at two cents, by the scanner, for 40 taken. At 50, from 2026-10-08 05:55 to 10-10
                                # 05:55 UTC, 4 of 88 capped trades filled in full, locking in 14.58$, and 2 filled only on Polymarket
                                # US, sold back for 6.48$ less. From 2026-10-06 15:34 to 10-10 17:55 UTC live made 26.67$ in play,
                                # where a cap of 10 throughout would have made some 22$: of the 162 trades asking the cap, 10 matched
                                # it in full, and one at 100 lost 7.82$ when Kalshi filled none of Polymarket US's 100.
LIVE_IN_PLAY_ALLOWANCE = 0.5    # The share of the edge left once Polymarket US's leg has filled, between what it cost and Kalshi's
                                # limit, that Kalshi's order on a game under way may give up to fill: its limit is raised, to the
                                # cent, as far as keeps the rest, fees included, see Executor.follow_limit(). From 2026-10-12, at the
                                # user's asking. From 2026-10-06 15:34 to 10-11 23:00 UTC Polymarket US filled more than Kalshi on
                                # 210 trades in play, 1,016 contracts, Kalshi filling none of them on 199, its price gone by the
                                # time our order came. Selling those back cost 47.95$, 4.72 cents a contract, of the 129.00$ the
                                # in-play trades locked in, where the edge on them had been 6.32 cents. An immediate or cancel order
                                # fills at the best price there is, so the room costs nothing where Kalshi's price has not moved.
                                # A future's legs keep their limits, as every level its orders sweep must return MIN_ANNUAL_PCT.
LIVE_MIN_CONTRACTS = 0.1        # The fewest contracts a live trade opens with, on a future or a game, in hundredths above it, which both
                                # venues take orders and fill in. One whole contract until 2026-10-07, when the user asked for a tenth:
                                # from 2026-10-05 to 10-07 the episodes offering under a contract at live's least edge would have added
                                # some 2.2$ a day on futures, about 18 trades, and 4.7$ on games, about 520, and a tenth kept nine in
                                # ten of the futures' dollars at under half their trades. Neither venue charges a small order more:
                                # Kalshi rounds its fee to the hundredth of a cent, and Polymarket US rounds a small order's to nothing.
LIVE_BALANCE_SECONDS = 15   # Between readings of the venues' balances. Under http.IDLE_SECONDS, so each reading also keeps the
                            # venue's kept connection open for the next order: one opened afresh took Polymarket US 11 ms longer.
LIVE_LOW_CASH = 5.0         # Dollars on a live venue, or on one of its shards in LIVE_SHARDS, under which a human is emailed, once until
                            # it is back over. Trades there go on as far as the cash pays for.
LIVE_SHARDS = {"kalshi": {0: 80, 3: 20}}    # The exchange shards a venue splits its cash by that live trading keeps
                                            # cash on, each with its own low cash email, and the whole percent of the
                                            # cash each keeps. Kalshi trades football, hockey, soccer, motorsport, UFC,
                                            # darts, esports, and politics on shard 0, Bitcoin on 2, and baseball,
                                            # basketball, and tennis on 3, and tools/kalshi_shards.py splits its cash
                                            # between them by these percents, a shard left out giving up all it has. 90
                                            # to 10 between 0 and 3 until 2026-10-05, when the user gave Bitcoin's
                                            # shard 10%: with none, live had traded no Bitcoin market. 80/10/10 until
                                            # 2026-10-10, when the user left Bitcoin out again: its 31 window trades in
                                            # the two days before filled nothing on Polymarket US, its futures are held
                                            # out, and its shard sat at 54.09$ untouched while shard 3 ran dry. The
                                            # user had Bitcoin taken out of the catalog that day too.
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
# Polymarket US has recorded as finished, the settling time from the first live game, Atlanta at Green Bay.

GAME_HOURS = {              # How long a game is expected to last, kickoff to final whistle: three in four of the sport's games end
    "nfl": 3.25,            # by then. 168 NFL games had a median of 3.11 hours, three in four by 3.24, and 382 college games a median
    "ncaaf": 3.75,          # of 3.48, three in four by 3.71. 2,399 baseball games had a median of 2.83, three in four by 3.09,
    "mlb": 3.25,            # 475 hockey games a median of 2.75, three in four by 2.88, and 418 basketball games a median of 2.53,
    "nhl": 3.0,             # three in four by 2.71, and all three get more for playoff games, which run longer: three in four of 79
    "nba": 3.0,             # hockey playoff games ended by 3.17, of 96 basketball ones by 2.87. A game's bets are taken to pay
                            # SETTLE_HOURS after it. The rest are allowances, not yet measured: a basketball game's, a soccer
    "wnba": 2.75, "ncaab": 2.75,     # match's 90 minutes with half time and stoppage time, a tennis match's best of three or five
    **{league: 2.0 for league in sports.SOCCER},   # sets, a
    "tennis": 3.0, "ufc": 1.0, "darts": 1.5,     # UFC fight's walk out and five rounds with the card running late, and a
    "f1": 2.0, "nascar": 4.0,                   # race's. Politics has no game, but every sport
    "politics": 0.0,                            # needs a length. An esports match's best of three maps, with the breaks
    **{title: 3.0 for title in sports.ESPORTS},  # between them and a start running late, also an allowance. It was short
                                                # for Dota 2: from 2026-10-05, 6 of its 19 live trades in play were
                                                # signalled past their payout time, see FLATTEN_PAST_PAYOUT_HOURS.
}
SETTLE_HOURS = 0.5          # Final whistle to the venues settling. A game's money is back SETTLE_HOURS after it ends.
RECORD_HOURS = 5            # Kickoff to when a game's contracts stop being recorded, whatever their close time says. The kickoff
                            # is Polymarket US's, since Kalshi gives none, and holds for both venues' contracts.

# RECORDING, market/record.py

BOOK_LEVELS = 5             # Price levels kept per side.
FEED_PROCESSES = True       # Run each venue's feed in a process of its own, see market/feeds.py. False runs every feed in the main process.
GAME_WINDOW_DAYS = 7        # Games further out than this are not recorded.
CLOSED_MARKET_SECONDS = 600 # How long a market that turned an order away as not trading is left alone, unless its venue says sooner
                            # that it trades, see Recorder.refuse(). From 2026-10-07 at the user's asking, after a paused CS2
                            # match's Kalshi leg was refused once the Polymarket US one had filled.
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
                                        # over the passes, see money/settle.py. The venues settle many futures months early: on
                                        # 2026-10-11, 26 live trades waiting on a payout time in 2027 had both legs settled already.


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
