"""
Data classes shared across the project.

Each model lists its key first, as its table does: the row id, or the
columns of the primary key. A row id is keyword only, since it is set when
the row is stored rather than when the model is made.
"""

from dataclasses import dataclass, field


def row_id():
    """
    A model's row id field: keyword only, and None until the row is stored.
    """
    return field(default=None, kw_only=True)


@dataclass
class Contract:
    """
    One tradable Yes side outcome, described the same way for every venue.
    """
    venue: str
    contract_id: str        # Kalshi market ticker, or Polymarket US market slug.
    market_id: str          # Kalshi market ticker, or Polymarket US market id.
    event_id: str           # Kalshi event ticker, or Polymarket US event slug.
    series_id: str | None   # Kalshi series ticker, or Polymarket US series slug.
    sport: str
    event_title: str | None
    title: str
    outcome: str            # 'Yes', a team name, 'Over', and so on.
    market_type: str | None
    line: float | None
    rules: str | None
    start_time: str | None
    close_time: str | None
    fee_info: dict | None


@dataclass
class Bet:
    """
    What a contract is about, in venue neutral terms. Two contracts on
    different venues with the same Bet fields describe the same bet.
    """
    venue: str
    contract_id: str
    kind: str               # 'game_winner', 'spread', 'total', 'player_receiving_yards', and so on.
    season: int | None      # The year the season ends, for example 2027.
    game_date: str | None   # Game date in US Eastern time as YYYY-MM-DD, for game kinds only.
    team_a: str | None      # Away team code for game kinds.
    team_b: str | None      # Home team code for game kinds.
    subject: str | None     # The team the contract is about, when there is one.
    line: float | None      # Spread margin, total points, or wins threshold.
    polarity: str           # 'yes' pays when the bet's statement is true, 'no' pays when it is false.
    pair_id: int | None = None       # The pair this bet belongs to, set when the pairs are stored.


@dataclass
class Pair:
    """
    The contracts on both venues that describe one bet. The label is the
    bet's identity in words, and the id is what the other tables refer to.
    Members are Bets, two or three of them, since a venue may list both
    sides of a game as contracts, or of a nested pair both lines' bets, see
    catalog/nest.py.
    """
    id: int | None = row_id()
    sport: str = field(kw_only=True)    # Our sport key, for example 'nfl'.
    label: str              # The sport and the bet's identity, for example 'nfl spread 2026-09-20 CAR@ATL ATL 4.5'.
    kind: str
    season: int | None
    game_date: str | None
    team_a: str | None
    team_b: str | None
    subject: str | None
    line: float | None
    members: list           # Bets, one per contract.
    flags: list[str]        # Things a human should check before trusting the pair.

    @property
    def venues(self):
        return sorted({m.venue for m in self.members})


@dataclass
class Book:
    """
    One contract's order book at one moment, seen from the Yes side.
    """
    venue: str
    contract_id: str
    ts: str                 # Our clock, ISO 8601 UTC, when the book changed.
    bids: list              # [[price, size], ...] best first.
    asks: list              # [[price, size], ...] best first.
    at: float | None = None     # The venue's clock, seconds since 1970, when it made the change, or None when it did not say.
    halted: str | None = None   # Why the market is not trading, as its venue said or an order it turned away showed, or None
                                # while it trades. A halted book is not priced or traded, see pricing.fresh().


@dataclass
class Opportunity:
    """
    A stretch of time when one pair could be traded for a profit after
    fees, by buying yes exposure on one contract and no exposure on another.
    """
    id: int | None = row_id()
    pair_id: int            # The pair, see pairs.
    trade: str              # The two legs in words.
    yes_venue: str          # Where the yes exposure was cheapest at the peak.
    yes_contract: str
    no_venue: str           # Where the no exposure was cheapest at the peak.
    no_contract: str
    start_ts: str           # When the net edge first went positive.
    end_ts: str             # When it went back to zero, or the last book seen.
    seconds: float
    peak_ts: str
    peak_edge: float        # Net dollars per contract at the top of book, at the peak.
    peak_size: float        # Contracts fillable at a positive net edge, at the peak.
    peak_profit: float      # Net dollars from filling peak_size, at the peak.
    live: int               # 1 when the game had started, 0 otherwise.
    days_held: float | None     # From the peak until the bet pays out, if held to resolution.
    return_pct: float       # Net edge over the capital tied up, as a percent.
    annual_pct: float | None    # return_pct scaled to a year over days_held, without compounding.
    min_edge_seconds: float | None = None   # The longest unbroken stretch of the episode at config.MIN_EDGE or more.
    min_edge_size: float | None = None      # Contracts fillable at that edge through all of that stretch.
    min_edge_profit: float | None = None    # Net dollars from filling them, at the stretch's thinnest moment.
    take_size: float | None = None      # Contracts fillable on the levels at live's least edge or more, see
                                        # pricing.live_min_edge(): what one order could have had at the best moment the scanner
                                        # weighed, see Scanner.weigh_take(), at the peak alone to 2026-10-06. Null before 2026-10-05.
    take_profit: float | None = None    # Net dollars from filling them.
    pm_changed: int | None = None       # 1 when that moment came with a change of the Polymarket US leg's book, which live needed
                                        # to trade a game under way from 2026-10-05 to 10-06, see pricing.polymarket_us_just_changed(),
                                        # 0 otherwise. Null before 2026-10-05.


@dataclass
class Gap:
    """
    A stretch when a venue's feed was down, so its books could not be trusted.
    """
    venue: str
    start_ts: str           # When the connection was lost.
    end_ts: str | None      # When a new connection was subscribed. None while still down.


@dataclass
class Leg:
    """
    One side of a trade: the contract it is held through, the order sent for
    it, and what it holds after any flattening. The executor makes one for
    each side when it sends a trade and keeps it up to date as the orders
    fill, and a stored Trade gives its legs back through Trade.leg().
    """
    side: str               # 'yes' or 'no', the side of the bet this leg holds.
    venue: str
    contract_id: str
    polarity: str           # The side the contract pays on.
    limit: float = 0.0      # The highest cost per contract the order accepts.
    quantity: float = 0     # Contracts the order asks for: whole to open a trade, to the hundredth to flatten one.
    held: float = 0         # Contracts held after the fill and any flattening, to the hundredth, see orders.exact().
    cost: float = 0.0       # Dollars paid for what is held, including fees.
    fee_info: dict | None = None    # The contract's fee schedule, which the executor prices orders with. Not stored with the trade.

    @property
    def key(self):
        return (self.venue, self.contract_id)

    @property
    def outcome(self):
        """
        The outcome of its contract the leg holds: 'yes' when it holds the side
        the contract pays on, 'no' when it holds the other. It is what the
        leg's orders trade, and the leg wins when the contract resolves to it.
        """
        return "yes" if self.side == self.polarity else "no"


@dataclass
class Trade:
    """
    One trade, paper or live: two legs sent on a scanner signal, what each filled,
    the profit locked in on the matched contracts, and how any mismatch
    was flattened. How its legs paid out is its Settlement, once it has one.
    """
    id: int | None = row_id()
    mode: str = field(kw_only=True)     # 'paper' or 'live', the executor that made the trade.
    pair_id: int            # The pair, see pairs.
    trade: str              # The two legs in words.
    signal_ts: str          # When the scanner signalled.
    edge: float             # Net dollars per contract at the signal.
    quantity: float         # Contracts wanted on each leg: whole until 2026-10-07, to the hundredth live since.
    yes_venue: str
    yes_contract: str
    yes_polarity: str       # The side the contract pays on, so settlement knows whether the leg won.
    yes_limit: float        # The cost per contract seen at the signal, used as the limit.
    no_venue: str
    no_contract: str
    no_polarity: str
    no_limit: float
    pays_at: str            # When the slower leg pays out.
    yes_filled: float = 0   # Contracts filled, to the hundredth the venues fill in, see orders.exact(). So are held and matched.
    yes_cost: float = 0.0   # Dollars paid including fees.
    yes_latency_ms: int = 0
    yes_fill_ts: str | None = None
    no_filled: float = 0
    no_cost: float = 0.0
    no_latency_ms: int = 0
    no_fill_ts: str | None = None
    yes_held: float = 0     # Contracts still held on the yes leg after any flattening.
    no_held: float = 0
    matched: float = 0      # Contracts held on both sides, after any flattening.
    profit: float = 0.0     # Dollars locked in on the matched contracts, after fees.
    hedge: str = "none"     # How the mismatch was flattened, in words.
    hedge_pnl: float = 0.0  # Dollars gained or lost by flattening, after fees.
    status: str = "sent"    # 'sent' while in flight, then 'filled', 'partial', or 'failed'.
    in_play: int | None = None  # 1 for a trade on a game under way at the signal, else 0. None for trades before 2026-10-05, which lack it.
    label: str | None = None    # The pair's label for log lines, read from the pairs table rather than stored here.
    starts_at: str | None = None    # Kickoff of the game behind the trade, read from the contracts table, for the settler.

    def leg(self, side):
        """
        One leg, 'yes' or 'no', from the trade's fields for that side, without its fee schedule.
        """
        venue, contract_id, polarity, limit, held, cost = (getattr(self, f"{side}_{name}") for name in ("venue", "contract", "polarity", "limit", "held", "cost"))
        return Leg(side, venue, contract_id, polarity, limit, self.quantity, held, cost)

    def legs(self):
        """
        Both legs, yes first.
        """
        return [self.leg("yes"), self.leg("no")]


@dataclass
class Settlement:
    """
    How a trade's legs paid out once both had resolved. A leg that held
    nothing has no result.
    """
    trade_id: int           # The trade settled, see trades. A trade has at most one settlement.
    mode: str = field(kw_only=True)     # 'paper' or 'live', the trade's mode.
    settled_at: str         # When both legs had resolved and the payouts were paid in: the later leg's settlement.
    yes_result: str | None = None       # How the yes leg's contract resolved, 'yes' or 'no', or the decimal a yes contract paid
                                        # when it paid part of a dollar, '0.6' for 60 cents, see settle.written().
    yes_payout: float | None = None     # Dollars received on the yes leg: per contract held, what a yes contract paid when the leg
                                        # holds yes and the rest of the dollar when it holds no.
    yes_settled_at: str | None = None   # The venue's settlement time for the yes leg.
    no_result: str | None = None
    no_payout: float | None = None
    no_settled_at: str | None = None

    def record(self, side, result, payout, settled_at):
        """
        Set how one leg, 'yes' or 'no', resolved, what it paid, and when the venue settled it.
        """
        setattr(self, f"{side}_result", result)
        setattr(self, f"{side}_payout", payout)
        setattr(self, f"{side}_settled_at", settled_at)


@dataclass
class Order:
    """
    One real order the live executor sent to a venue, and what came back.
    """
    id: int | None = row_id()
    trade_id: int           # The live trade the order was sent for, see trades.
    venue: str
    contract_id: str
    purpose: str            # 'open' for one of the trade's two legs, 'flatten' for an order that evens them.
    action: str             # 'buy' or 'sell'.
    outcome: str            # 'yes' for the contract itself, 'no' for its other side.
    quantity: float         # Contracts asked for, to the hundredth.
    limit_price: float      # The worst price per contract accepted for the outcome, before fees.
    client_id: str          # Our id for the order, sent with it, so an order whose answer was lost can be found at the venue.
    sent_at: str
    status: str = "sent"    # 'sent' until the venue answers, then 'filled', 'partial', 'unfilled', 'unfunded', 'closed', 'rejected', or 'error'.
    venue_order_id: str | None = None   # The venue's id for the order, once it answered.
    answered_at: str | None = None
    latency_ms: int | None = None       # From sending the order to its answer.
    filled: float = 0       # Contracts bought or sold, to the hundredth, see orders.exact().
    dollars: float = 0.0    # Paid for a buy or received for a sale, fees included.
    fees: float = 0.0
    note: str | None = None             # Why the venue rejected the order, or the error.
    response: str | None = None         # The venue's answer as JSON, for reconciling.
    book_at: str | None = None          # The venue's time for the contract's newest book when the order went out, or None when it gave
                                        # none, as for the first book on a connection: when the venue last changed the market.
    book_ts: str | None = None          # When that book reached us, by our clock. sent_at less it is how long the market had sent nothing.


@dataclass
class Ledger:
    """
    One cash movement on a venue's paper balance. Live money is read from the venues and has no ledger.
    """
    id: int | None = row_id()
    ts: str
    venue: str
    amount: float           # Dollars in or out, positive when money arrives.
    reason: str             # 'buy', 'sell', 'payout', or 'transfer_in', each venue's opening balance.
    trade_id: int | None = None     # The trade behind a buy, sell, or payout.
    balance: float | None = None    # The venue's balance after this entry, so the newest entry gives the balance.


@dataclass
class Alert:
    """
    Something the live process told a human by email.
    """
    id: int | None = row_id()
    ts: str
    kind: str               # 'halt' when live trading stopped, 'low_cash' when a live venue's cash fell under config.LIVE_LOW_CASH, or
                            # 'attestation' when the Kalshi key's location attestation is about to lapse or has.
    subject: str
    body: str
    sent_at: str | None = None      # When the email went out, None until it has.
    error: str | None = None        # Why it could not be sent.
