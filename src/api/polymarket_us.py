"""
Polymarket US API client.

Three jobs. The query half reads the public events listing on the gateway
host, filtered by sport tag, turns every open market into a Contract, one
per market, for the market's long side, and reads the account's balance.
The streaming half opens the signed markets websocket and keeps a live
book per market slug, replacing the whole book on every message because
the feed sends full snapshots, and says when a market is not open for
trading, from the state each message gives. The trading half sends
signed orders for the live executor. This is the only file that knows
Polymarket US field names and message formats.

Requests to the API host must be signed with the account's key. The key
id and secret live in the data folder, see KEY_ID_FILE and SECRET_KEY_FILE.
"""

import base64
import functools
import json
import math
import time
from api import orders
from api.bookstream import BookStream
from api.http import RequestFailed, get_json, send_json
from common.jsonutil import float_or_none, float_or_zero
from common.paths import DATA_DIR
from common.timeutil import days_between, epoch, iso
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from db.models import Contract

GATEWAY = "https://gateway.polymarket.us/v1"    # Public catalog of events and markets.
RACE_DAYS = 2           # The longest a race's event runs, start to end, so that a longer one with a sports data id is a season's award.
# Seconds between the calls of one results lookup, one an event, so a settlement pass never bursts: Cloudflare answered HTTP 429
# at about 6 calls a second on 2026-10-11, and the live orders share that limit.
RESULTS_SLEEP = 0.5
API = "https://api.polymarket.us/v1"            # Signed requests for books and trading.
API_PATH = "/v1"                                # API's path, which a signed request's signature covers.
WS_URL = "wss://api.polymarket.us/v1/ws/markets"
WS_PATH = "/v1/ws/markets"
WS_CHUNK = 100          # Market slugs per subscription request, the documented maximum.
WS_SUBSCRIPTIONS = 10   # Subscription requests per connection, however few slugs each carries.
# What each chunk of slugs subscribes to: the whole book. The trade feed was dropped on 2026-09-30. Trades came about 4% as
# often as book changes, arrived no sooner than the book with the same time, and can only take quotes away, never add them.
WS_TYPE = "SUBSCRIPTION_TYPE_MARKET_DATA"
WS_FULL = "max subscriptions per connection reached"    # The error refusing a request past WS_SUBSCRIPTIONS.
WS_DEBOUNCE = False     # Whether to ask the feed to batch updates. Batching cuts bandwidth by a third, but it can hold our view of the
                        # book behind the venue's: on the first live game, 2026-09-28, only 1 of 11 orders opening a trade here filled.
OPEN_STATE = "MARKET_STATE_OPEN"    # The state a book message gives a market that trades. Others are suspended, halted, preopen,
                                    # expired, terminated, and match_and_close_auction. On 2026-10-07, 109 of 3,000 markets the
                                    # recorder followed came expired.
KEY_ID_FILE = DATA_DIR / "polymarket_us_key_id.txt"
SECRET_KEY_FILE = DATA_DIR / "polymarket_us_secret_key.txt"


# SIGNING

@functools.cache
def credentials():
    """
    The account's key id and Ed25519 private key, read from the data folder once.
    """
    secret = base64.b64decode(SECRET_KEY_FILE.read_text().strip())
    return KEY_ID_FILE.read_text().strip(), Ed25519PrivateKey.from_private_bytes(secret[:32])


def signed_headers(method, path):
    """
    The three headers that authenticate a request. The signature is the
    account's Ed25519 key over the timestamp, method, and path.
    """
    key_id, key = credentials()
    ts = str(int(time.time() * 1000))
    signature = base64.b64encode(key.sign(f"{ts}{method}{path}".encode())).decode()
    return {"X-PM-Access-Key": key_id, "X-PM-Timestamp": ts, "X-PM-Signature": signature}


def signed_request(method, path, body=None):
    """
    A signed call to the API at a path under API, for example '/account/balances'.
    """
    return send_json(method, API + path, signed_headers(method, API_PATH + path), body)


# QUERY

def fetch_events(tag_slug, page_size=500):
    """
    Every open event carrying the tag, with its markets nested inside, following offset pagination.
    """
    events, offset = [], 0
    while True:
        page = get_json(f"{GATEWAY}/events", {"tag_slug": tag_slug, "limit": page_size, "offset": offset, "closed": "false"})["events"]
        events.extend(page)
        if len(page) < page_size:
            return events
        offset += page_size


def is_game(event):
    """
    Whether an event is a game, whose start time is its kickoff, rather than
    a future. The venue gives a game the id of its sports data feed, and
    Sportradar's, though not always its own: NHL preseason games and many
    small college ones come with only Sportradar's. An award's event has a
    Sportradar id too, 'type_national_league_mvp-sport_baseball-league_mlb',
    so a futures market never takes its event's start time as a kickoff.
    """
    return bool(event.get("gameId") or event.get("sportradarGameId"))


def is_race(event):
    """
    Whether a game event's markets, typed as futures, are on one race, 'f1-gabgpim-2026-10-04-w', which starts at the
    event's start time. An award's event, also with a sports data id, runs for months.
    """
    start, end = iso(event.get("startTime")), iso(event.get("endDate"))
    return bool(is_game(event) and start and end and days_between(start, end) <= RACE_DAYS)


def start_time(event, future):
    """
    When a market's event starts, the kickoff of a game or the start of a race, or None for a future, whose event may
    carry a sports data id like a game's.
    """
    if is_game(event) and (not future or is_race(event)):
        return iso(event.get("startTime"))
    return None


def contracts(sport, tags):
    """
    One Contract per open market on events carrying one of the tag slugs.
    The contract is the market's long side, which is Yes, Over, or the first side named.
    """
    result, seen_events = [], set()
    for event in (e for tag in tags for e in fetch_events(tag)):
        if event["slug"] in seen_events:
            continue
        seen_events.add(event["slug"])
        for m in event.get("markets", []):
            future = m.get("sportsMarketType") == "futures"
            if m.get("closed"):
                continue
            long_side = next((s for s in m.get("marketSides", []) if s.get("long")), {})
            result.append(Contract(
                venue="polymarket_us",
                contract_id=m["slug"],
                market_id=str(m.get("id")),
                event_id=event["slug"],
                series_id=event.get("seriesSlug"),
                sport=sport,
                event_title=event.get("title"),
                # A future's question is its event's, 'National League MVP', and its title the player or line, 'Pete Crow-Armstrong'.
                title=(m.get("title") if future else m.get("question")) or m.get("question") or m.get("title") or "",
                outcome=long_side.get("description") or "Yes",
                market_type=m.get("sportsMarketType"),
                line=float_or_none(m.get("line")),
                rules=m.get("description"),
                start_time=start_time(event, future),
                # A future's market stays open two weeks past its event in case the event moves. The event's end is when it
                # is expected to settle, which the payout time and how long it is recorded go by.
                close_time=iso((event.get("endDate") if future else None) or m.get("endDate")),
                fee_info={"feeCoefficient": m.get("feeCoefficient")},
            ))
    return result


def results(event_slugs):
    """
    Settlement results for every market on the events, as {market slug:
    (paid, settled_at)} with paid what a contract of the long side, the
    market's yes, paid, from 0 to 1: the long side's price once the market
    has resolved, 1 when it won and 0 when it lost, and part of a dollar
    when the rules pay one, as the last fair price of 0.41 for a drawn CS2
    match on 2026-10-09, or a share of the dollar for a tie. Markets not yet
    resolved are left out. Each event costs a call, with RESULTS_SLEEP
    between them.
    """
    out = {}
    for i, slug in enumerate(sorted(set(event_slugs))):
        if i:
            time.sleep(RESULTS_SLEEP)
        for event in get_json(f"{GATEWAY}/events", {"slug": slug}).get("events", []):
            for m in event.get("markets", []):
                long_side = next((s for s in m.get("marketSides", []) if s.get("long")), None)
                paid = float_or_none(long_side.get("price")) if long_side else None
                if m.get("status") == "MARKET_STATUS_RESOLVED" and paid is not None and 0 <= paid <= 1:
                    out[m["slug"]] = (paid, iso(m.get("endDate")))
    return out


def positions():
    """
    The contracts the account holds, as {market slug: contracts}, positive
    for the long side, the market's yes, and negative for the short, its
    no, leaving out those of markets that have expired. One page holds them
    all so far. Raises ValueError when the venue says there are more, rather
    than miss them.
    """
    answer = signed_request("GET", "/portfolio/positions")
    if not answer.get("eof", True):
        raise ValueError("the positions run past one page, which is not read yet")
    held = {slug: float_or_zero(p.get("netPositionDecimal", p.get("netPosition")))
            for slug, p in (answer.get("positions") or {}).items() if not p.get("expired")}
    return {slug: contracts for slug, contracts in held.items() if contracts}


def balance():
    """
    Dollars available for trading on the account: the buying power of its dollar balance.
    """
    for b in signed_request("GET", "/account/balances").get("balances", []):
        if b.get("currency", "USD") == "USD":
            return float(b.get("buyingPower", b.get("currentBalance", 0.0)))
    return 0.0


# STREAMING

def levels(entries, reverse):
    """
    Turn the feed's price levels into [price, size] lists, best first.
    """
    parsed = [[float(e["px"]["value"]), float(e["qty"])] for e in entries or []]
    return sorted((lv for lv in parsed if lv[1] > 0), key=lambda lv: lv[0], reverse=reverse)


def not_trading(state):
    """
    Why a market whose book message gives this state is not trading, in a
    word, 'suspended' for MARKET_STATE_SUSPENDED, or None when it trades. A
    message without a state counts as trading.
    """
    if not state or state == OPEN_STATE:
        return None
    return state.removeprefix("MARKET_STATE_").lower()


class PolymarketUSBookStream(BookStream):
    """
    The signed markets websocket. Each book message carries a market's
    whole book, so the local copy is replaced rather than patched, and the
    market's state, so one that is not open is said to be not trading, see
    not_trading().

    The feed allows WS_SUBSCRIPTIONS subscription requests on a connection,
    of up to WS_CHUNK slugs each. It documents no unsubscribe, so removed
    slugs are simply ignored until the next connect and never give their
    requests back. A fresh connection subscribes its slugs, capacity at
    most, in full requests, but every later add spends requests of its own,
    even for a single slug. So room() counts the requests left, and the feed
    opens another connection once none has any. A request refused anyway,
    as past the limit, has its slugs handed back through on_refused to go on
    another connection.
    """

    name = "polymarket_us"
    capacity = WS_CHUNK * WS_SUBSCRIPTIONS

    def __init__(self, contract_ids, on_book, on_gap=None, log=print):
        super().__init__(contract_ids, on_book, on_gap, log)
        self.reset()            # The request count is read before the first connect, to place adds.

    def reset(self):
        self.request_id = 0
        self.requests = 0       # Subscription requests this connection has spent, adds still queued included.
        self.asked = {}         # Each request id sent maps to its slugs, to hand them back if it is refused.
        self.seen = set()       # Slugs whose first book has come on this connection. That one is a snapshot, whose transactTime is its last change, maybe long ago.

    def room(self):
        """
        The slugs one add may bring: as many as fit in the requests left.
        Before the connection subscribes, the requests counted are only the
        adds, which the subscription will carry with the rest, so the slug
        capacity bounds it too.
        """
        return max(0, min((WS_SUBSCRIPTIONS - self.requests) * WS_CHUNK, self.capacity - len(self.wanted)))

    def add(self, contract_ids):
        new = set(contract_ids) - self.wanted
        super().add(new)
        self.requests += math.ceil(len(new) / WS_CHUNK)     # What send_command will spend on them.

    def connect(self):
        return self.open_connection(WS_URL, signed_headers("GET", WS_PATH))

    async def send_subscriptions(self, ws, slugs):
        """
        Subscribe to the slugs' books, WS_CHUNK at a time.
        """
        for i in range(0, len(slugs), WS_CHUNK):
            self.request_id += 1
            request_id = f"md-{self.request_id}"
            self.asked[request_id] = slugs[i:i + WS_CHUNK]
            await ws.send(json.dumps({"subscribe": {"requestId": request_id, "subscriptionType": WS_TYPE,
                                                    "marketSlugs": slugs[i:i + WS_CHUNK], "responsesDebounced": WS_DEBOUNCE}}))

    async def subscribe(self, ws):
        slugs = sorted(self.wanted)
        self.requests = math.ceil(len(slugs) / WS_CHUNK)    # Replaces the count of adds, which these slugs include.
        await self.send_subscriptions(ws, slugs)

    async def send_command(self, ws, action, slugs):
        if action == "add":
            await self.send_subscriptions(ws, slugs)

    def refused(self, request_id):
        """
        The feed refused a request as past the limit. Whatever the count
        said, the connection is full, so it takes nothing more, and the
        request's slugs still wanted go on another connection.
        """
        self.requests = WS_SUBSCRIPTIONS
        slugs = sorted(set(self.asked.pop(request_id)) & self.wanted)
        if not slugs:
            return          # Removed since.
        self.log(f"polymarket_us refused {request_id} as one subscription too many, moving its {len(slugs)} contracts to another connection")
        self.remove(slugs)
        self.on_refused(slugs)

    def show(self, slug, sent):
        """
        Pass a market's book on, its best self.depth levels a side, with the venue's time for it.
        """
        book = self.books[slug]
        self.on_book(slug, book["bids"][:self.depth], book["asks"][:self.depth], sent)

    def handle(self, raw):
        m = json.loads(raw)
        data = m.get("marketData")
        if not data:
            error = m.get("error")
            if error and WS_FULL in str(error) and m.get("requestId") in self.asked:
                self.refused(m["requestId"])
            elif error:
                self.log(f"polymarket_us stream error {error} on {m.get('requestId')}")
            return False
        slug = data.get("marketSlug")
        if slug not in self.wanted:
            return True
        at = epoch(data.get("transactTime"))
        self.books[slug] = {"bids": levels(data.get("bids"), reverse=True), "asks": levels(data.get("offers"), reverse=False), "at": at}
        self.set_state(slug, not_trading(data.get("state")))
        self.show(slug, at if slug in self.seen else None)
        self.seen.add(slug)
        return True


# TRADING

# The order intent for each action on each outcome. The long side is the contract itself, the short side its other side.
INTENTS = {("buy", "yes"): "ORDER_INTENT_BUY_LONG", ("sell", "yes"): "ORDER_INTENT_SELL_LONG",
           ("buy", "no"): "ORDER_INTENT_BUY_SHORT", ("sell", "no"): "ORDER_INTENT_SELL_SHORT"}
FILL_TYPES = ("EXECUTION_TYPE_FILL", "EXECUTION_TYPE_PARTIAL_FILL")
# The executions after which an order is done: filled in full, the rest canceled, rejected, or expired.
FINAL_TYPES = ("EXECUTION_TYPE_FILL", "EXECUTION_TYPE_CANCELED", "EXECUTION_TYPE_REJECTED", "EXECUTION_TYPE_EXPIRED")
FINAL_STATES = ("ORDER_STATE_FILLED", "ORDER_STATE_CANCELED", "ORDER_STATE_REJECTED", "ORDER_STATE_EXPIRED")
# The message of an order rejected by the latency stopgap: one not processed within 5 seconds, when the exchange
# is slow, to spare a fill at a stale price. It reads like a rate limit but is not one, so it counts as unfilled.
STOPGAP = "Global Rate Limit Exceeded"
# The message of an order turned away for lack of cash, which counts as unfunded rather than refused. A sale can need cash too:
# the venue keeps one position per market, so selling the No one trade holds where others hold more Yes is buying Yes.
NO_FUNDS = "You don't have enough funds"
NO_LIQUIDITY = "ORD_REJECT_REASON_NO_LIQUIDITY"     # A rejection for finding nothing to trade, which is also unfilled rather than refused.
# What the venue says, in any case, of an order turned away because its market is not trading, which counts as closed rather
# than refused: ORD_REJECT_REASON_EXCHANGE_CLOSED, the docs' "market is closed", and a market halted or suspended.
NOT_TRADING = ("exchange_closed", "market is closed", "market_closed", "halted", "suspended")
MAX_BLOCK_SECONDS = 5   # How long an order call waits for its order to end, as long as the latency stopgap gives it.


def says_not_trading(text):
    """
    Whether what the venue said of an order means its market was not trading, see NOT_TRADING.
    """
    return any(word in (text or "").lower() for word in NOT_TRADING)


def amount(value):
    """
    An Amount from the API, {'value': '0.55', 'currency': 'USD'}, as a float, zero when missing.
    """
    return float_or_zero((value or {}).get("value"))


def price_text(price):
    """
    A price as the API takes it, a decimal string without trailing zeros.
    """
    return f"{price:.4f}".rstrip("0").rstrip(".")


def order_body(slug, action, outcome, quantity, price):
    """
    An immediate or cancel limit order for quantity contracts of one side of
    a market, to the hundredth the market takes, at price or better for that
    side, answered once it has run. The API prices every order on the long
    side, so a short side price p is sent as 1 - p.
    """
    long_price = price if outcome == "yes" else 1 - price
    return {"marketSlug": slug, "intent": INTENTS[(action, outcome)], "type": "ORDER_TYPE_LIMIT",
            "price": {"value": price_text(long_price), "currency": "USD"}, "quantity": orders.size(quantity),
            "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL", "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
            "synchronousExecution": True, "maxBlockTime": str(MAX_BLOCK_SECONDS)}


def look_up(answer, action, outcome, quantity):
    """
    What became of an order whose answer did not say how it ended, read from
    the order itself. Its fate is unknown when it has no id, cannot be read,
    or has still not ended.
    """
    order_id = answer.get("id")
    if not order_id:
        return orders.unknown(ValueError("the answer has neither an order id nor a final execution"), response={"answer": answer})
    try:
        order = signed_request("GET", f"/order/{order_id}")["order"]
    except Exception as e:
        return orders.unknown(e, order_id, {"answer": answer})
    response = {"answer": answer, "order": order}
    state = order.get("state")
    if state not in FINAL_STATES:
        return orders.unknown(ValueError(f"order {order_id} is still {state}"), order_id, response)
    filled = orders.exact(float_or_zero(order.get("cumQuantity")))
    if state == "ORDER_STATE_REJECTED" and not filled:
        return orders.Answer(order_id, "rejected", 0, 0.0, 0.0, state, response)
    long_price = amount(order.get("avgPx"))
    traded = filled * (long_price if outcome == "yes" else 1 - long_price)
    fees = amount(order.get("commissionNotionalTotalCollected"))
    paid = traded + fees if action == "buy" else traded - fees
    return orders.Answer(order_id, orders.status(filled, quantity), filled, paid, fees, None, response)


def read_answer(answer, action, outcome, quantity):
    """
    What an order's synchronous answer says came of it, as an Answer, from
    the executions it lists, or None when none of them says how the order
    ended. Execution prices are the long side's, since the market's one
    instrument is its yes side, so a short side fill at p cost 1 - p. The
    venue fills an order in pieces as small as a hundredth of a contract,
    0.1 then 0.89 then 0.01 for one, so the shares of every fill are added
    up and counted to the hundredth, see orders.exact(). An order turned
    away for having no liquidity or by the latency stopgap is unfilled, not
    refused, and one turned away because its market is not trading is
    closed, see orders.closed().
    """
    executions = answer.get("executions") or []
    if not any(e.get("type") in FINAL_TYPES for e in executions):
        return None
    shares, traded, fees = 0.0, 0.0, 0.0
    for e in executions:
        if e.get("type") not in FILL_TYPES:
            continue
        some, long_price = float_or_zero(e.get("lastShares")), amount(e.get("lastPx"))
        shares += some
        traded += some * (long_price if outcome == "yes" else 1 - long_price)
        fees += amount(e.get("commissionNotionalCollected"))
    filled = orders.exact(shares)
    rejected = next((e for e in executions if e.get("type") == "EXECUTION_TYPE_REJECTED"), None)
    if rejected and not filled:
        reason = rejected.get("orderRejectReason") or rejected.get("text")
        said = f"{rejected.get('text')} {rejected.get('orderRejectReason')}"
        if STOPGAP in said:
            return orders.Answer(answer.get("id"), "unfilled", 0, 0.0, 0.0, f"latency stopgap: {reason}", answer)
        if NO_LIQUIDITY in said:
            return orders.Answer(answer.get("id"), "unfilled", 0, 0.0, 0.0, f"no liquidity: {reason}", answer)
        if says_not_trading(said):
            return orders.Answer(answer.get("id"), "closed", 0, 0.0, 0.0, f"{orders.NOT_TRADING}: {reason}", answer)
        return orders.Answer(answer.get("id"), "rejected", 0, 0.0, 0.0, reason, answer)
    paid = traded + fees if action == "buy" else traded - fees
    return orders.Answer(answer.get("id"), orders.status(filled, quantity), filled, paid, fees, None, answer)


def place_order(slug, action, outcome, quantity, price, client_id):
    """
    Send an immediate or cancel limit order and return what came back as an
    Answer, read from its synchronous answer by read_answer(). action is
    'buy' or 'sell', outcome 'yes' or 'no', and price the worst price per
    contract accepted for that outcome. An answer that does not say how the
    order ended is followed by a look at the order itself, since a returned
    order id does not mean the order is done. The API takes no id of ours,
    so client_id is only kept in our own orders table.
    """
    try:
        answer = signed_request("POST", "/orders", order_body(slug, action, outcome, quantity, price))
    except RequestFailed as e:
        if STOPGAP in e.body:
            return orders.unfilled(e, "latency stopgap")
        if NO_FUNDS in e.body:
            return orders.unfunded(e)
        if e.status < 500 and says_not_trading(e.body):
            return orders.closed(e)
        return orders.refused(e) if e.status < 500 else orders.unknown(e)
    except Exception as e:
        return orders.unknown(e)
    return read_answer(answer, action, outcome, quantity) or look_up(answer, action, outcome, quantity)
