"""
Kalshi API client.

Three jobs. The query half walks series to events to markets on the public
API, sports' and elections', turns every open market into a Contract, and
reads the account's balance and whether the exchange is trading. The
streaming half opens one websocket with a signed API key, subscribes to
order book updates, and keeps a live book for each ticker restated from
the Yes side so it matches Polymarket US's shape, and to the markets'
lifecycle, to say when one is paused, closed, or decided. Tickers can be
added and removed while the connection runs. The trading half sends signed
orders for the live executor. This is the only file that knows Kalshi's
field names and message formats.
"""

import asyncio
import base64
import functools
import json
import time
import urllib.parse
from api import orders
from api.bookstream import BookStream, Reconnect
from api.http import RequestFailed, get_json, send_json
from common.jsonutil import float_or_none, float_or_zero
from common.paths import DATA_DIR
from common.timeutil import iso
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from db.models import Contract

# The hosts Kalshi dedicates to API traders, in place of the legacy api.elections.kalshi.com. A signed call there takes
# 17 ms against 27 on the legacy host, measured from the instance, and the feed is as fast.
BASE = "https://external-api.kalshi.com/trade-api/v2"
BASE_PATH = "/trade-api/v2"     # BASE's path, which a signed request's signature covers.
SLEEP = 0.12   # Seconds between paged calls, to stay under the public rate limit.
WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
WS_PATH = "/trade-api/ws/v2"
KEY_ID_FILE = DATA_DIR / "kalshi_key_id.txt"
PRIVATE_KEY_FILE = DATA_DIR / "kalshi_private_key.pem"
MARKETS_BATCH = 50   # Tickers per markets call, looking up results or whether markets trade, see markets().
SERIES_SECONDS = 600    # How long the list of every series is kept, so one catalog refresh of many sports reads it once.
# The error of an order whose market's exchange shard lacks the cash, which Kalshi moving cash between shards may cause
# between two of our readings. Nothing traded, so it counts as unfilled rather than refused.
SHORT_SHARD = "insufficient_shard_balance"
# The error of an order the account lacks the cash for. A sale can need cash: Kalshi keeps one position per market, so
# selling the Yes one trade holds where others hold more No is buying No. Such a sale is unfunded, as on Polymarket US,
# and waits for cash. A buy turned away for it is refused, since trades are sized by the cash.
NO_FUNDS = "insufficient_balance"
# Words in the error of an order turned away because its market or the exchange is not trading, which counts as closed
# rather than refused: MARKET_NOT_ACTIVE, as a paused match answers, and trading_is_paused, as every order in the Thursday
# maintenance gets, and a closed or inactive market.
NOT_TRADING = ("not_active", "trading_is_paused", "closed", "inactive")
LIFECYCLE = "market_lifecycle_v2"   # The channel saying when any market opens, pauses, closes, or is decided. It takes no tickers.
# Why a market with each status the markets call gives is not trading, None when it trades. Any other status is a market
# decided: determined, disputed, amended, or finalized.
STATUS_WHY = {"active": None, "open": None, "inactive": "paused", "initialized": "unopened", "unopened": "unopened", "closed": "closed"}


# SIGNING

@functools.cache
def credentials():
    """
    The account's key id and RSA private key, read from the data folder once.
    """
    return KEY_ID_FILE.read_text().strip(), serialization.load_pem_private_key(PRIVATE_KEY_FILE.read_bytes(), password=None)


def signed_headers(method, path):
    """
    The three headers that authenticate a request. Kalshi wants the timestamp,
    the method, and the path signed with the account's RSA key. The websocket
    handshake and the trading endpoints use the same scheme.
    """
    key_id, key = credentials()
    ts = str(int(time.time() * 1000))
    message = (ts + method + path).encode()
    signature = key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
    }


def signed_request(method, path, body=None, params=None):
    """
    A signed call to the API at a path under BASE, for example
    '/portfolio/balance'. The signature covers the path without its query.
    """
    url = BASE + path + (f"?{urllib.parse.urlencode(params)}" if params else "")
    return send_json(method, url, signed_headers(method, BASE_PATH + path), body)


# QUERY

def paged(path, params, key):
    """
    Follow Kalshi's cursor pagination and return every item under key.
    """
    items, cursor = [], None
    while True:
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        data = get_json(f"{BASE}{path}", p)
        items.extend(data.get(key, []))
        cursor = data.get("cursor")
        if not cursor:
            return items
        time.sleep(SLEEP)


_series = {"at": None, "list": []}    # Every series, and when it was read.


def all_series():
    """
    Every series on the exchange, of every category, read again once it is SERIES_SECONDS old. Some 15,000, read in 75
    pages, so a refresh of twenty sports reads them once rather than twenty times.
    """
    if _series["at"] is None or time.time() - _series["at"] > SERIES_SECONDS:
        _series["list"], _series["at"] = paged("/series", {"limit": 200}, "series"), time.time()
    return _series["list"]


def fetch_series(tickers, patterns=()):
    """
    The series with the tickers, or whose tickers match one of the compiled patterns, with their fee settings.
    """
    return [s for s in all_series() if s["ticker"] in tickers or any(p.match(s["ticker"]) for p in patterns)]


def fetch_events(series_ticker):
    """
    Open events for a series, with their markets nested inside.
    """
    return paged("/events", {
        "series_ticker": series_ticker, "status": "open",
        "with_nested_markets": "true", "limit": 200,
    }, "events")


def close_time(m):
    """
    When the contract stops trading, or settles if the venue expects that
    sooner. A game's close_time is days after it, while its
    expected_expiration_time is Kalshi's guess at the final whistle, three
    hours after kickoff, which many games outlast.
    """
    times = [t for t in (iso(m.get("close_time")), iso(m.get("expected_expiration_time"))) if t]
    return min(times) if times else None


def strict_line(m):
    """
    The market's line, restated so Yes always means strictly more than the line.
    Kalshi marks 'at least N' markets as greater_or_equal with a whole number
    strike, and at least N is the same as more than N minus a half.
    """
    line = float_or_none(m.get("floor_strike"))
    if line is None:
        line = float_or_none(m.get("cap_strike"))
    if line is not None and m.get("strike_type") == "greater_or_equal":
        line -= 0.5
    return line


def contracts(sport, tickers, patterns=()):
    """
    One Contract per open market of the series with the tickers or matching the patterns, meaning its Yes side.
    """
    result = []
    for series in fetch_series(tickers, patterns):
        fee_info = {
            "fee_type": series.get("fee_type"),
            "fee_multiplier": series.get("fee_multiplier"),
            "exchange_index": series.get("exchange_index", 0),     # The exchange shard its markets trade on, whose cash is its own.
        }
        for event in fetch_events(series["ticker"]):
            for m in event.get("markets", []):
                if m.get("status") not in (None, "open", "active"):
                    continue
                rules = " ".join(filter(None, [m.get("rules_primary"), m.get("rules_secondary")])) or None
                line = strict_line(m)
                result.append(Contract(
                    venue="kalshi",
                    contract_id=m["ticker"],
                    market_id=m["ticker"],
                    event_id=m.get("event_ticker") or event["event_ticker"],
                    series_id=series["ticker"],
                    sport=sport,
                    event_title=event.get("title"),
                    title=m.get("title") or "",
                    outcome=m.get("yes_sub_title") or "Yes",
                    market_type=None,
                    line=line,
                    rules=rules,
                    start_time=None,
                    close_time=close_time(m),
                    fee_info=fee_info,
                ))
    return result


def markets(tickers):
    """
    The markets of the tickers, sorted and each once, as the markets call
    gives them. The call takes a list of tickers, so this costs one call per
    MARKETS_BATCH tickers. A ticker the call does not return is left out.
    """
    tickers = sorted(set(tickers))
    for i in range(0, len(tickers), MARKETS_BATCH):
        batch = tickers[i:i + MARKETS_BATCH]
        yield from get_json(f"{BASE}/markets", {"tickers": ",".join(batch), "limit": len(batch)}).get("markets", [])
        if i + MARKETS_BATCH < len(tickers):
            time.sleep(SLEEP)


def yes_paid(market):
    """
    What a yes contract of a finalized market, as the markets call gives it,
    paid, from 0 to 1: a dollar for a result of 'yes', nothing for 'no', and
    for 'scalar' its settlement value, part of a dollar, as a drawn CS2
    match pays 0.60. None for a market not finalized, or with no result or
    one not read here.
    """
    if market.get("status") != "finalized":
        return None
    result = market.get("result")
    if result in ("yes", "no"):
        return 1.0 if result == "yes" else 0.0
    value = float_or_none(market.get("settlement_value_dollars")) if result == "scalar" else None
    return value if value is not None and 0 <= value <= 1 else None


def results(tickers):
    """
    Settlement results for the tickers, as {ticker: (paid, settled_at)} with
    paid what a yes contract paid, from 0 to 1, see yes_paid() and markets().
    Markets not yet finalized are left out.
    """
    out = {}
    for m in markets(tickers):
        value = yes_paid(m)
        if value is not None:
            out[m["ticker"]] = (value, iso(m.get("settlement_ts")))
    return out


def not_trading(status):
    """
    Why a market with this status, as the markets call gives it, is not trading, in a word, or None when it trades.
    """
    return STATUS_WHY.get(status, "decided")


def market_states(tickers):
    """
    Why each market is not trading, None for one that trades, as {ticker: why}, see markets().
    """
    return {m["ticker"]: not_trading(m.get("status")) for m in markets(tickers)}


def exchange_trading():
    """
    Whether the exchange takes orders now, on each shard, as {shard: bool},
    from the public status call. Kalshi stops trading every Thursday from 3
    to 5 AM Eastern, see common/venues.py, and at any other time it finds
    an issue. Without a shard by shard answer, the whole exchange's comes
    as {None: bool}.
    """
    answer = get_json(f"{BASE}/exchange/status")
    shards = answer.get("exchange_index_statuses") or []
    if not shards:
        return {None: bool(answer.get("trading_active") and answer.get("exchange_active"))}
    return {int(s["exchange_index"]): bool(s.get("trading_active") and s.get("exchange_active")) for s in shards}


def attestation_lapses():
    """
    When the account's location attestation for API keys lapses, in seconds
    since 1970, or None when Kalshi gives no date. Past it, Kalshi refuses
    the keys for Sports, Elections, and Entertainment markets until the
    attestation is renewed on Kalshi.
    """
    return signed_request("GET", "/api_keys").get("api_key_region_expiration_ts")


def balances():
    """
    Dollars available for trading on the account, every exchange shard's
    together, and on each shard, as (dollars, {exchange index: dollars}),
    from one call. Kalshi runs some sports on shards of their own, baseball
    on 3, and an order spends only the cash on its market's shard.
    """
    answer = signed_request("GET", "/portfolio/balance")
    dollars = float_or_zero(answer["balance_dollars"]) if "balance_dollars" in answer else answer["balance"] / 100
    return dollars, {int(entry["exchange_index"]): float_or_zero(entry["balance"]) for entry in answer.get("balance_breakdown") or []}


def positions():
    """
    The contracts the account holds, as {ticker: contracts}, positive for
    yes and negative for no, followed through every page.
    """
    held, cursor = {}, None
    while True:
        answer = signed_request("GET", "/portfolio/positions", params={"limit": 1000, **({"cursor": cursor} if cursor else {})})
        for p in answer.get("market_positions") or []:
            contracts = float_or_zero(p.get("position_fp", p.get("position")))
            if contracts:
                held[p["ticker"]] = contracts
        cursor = answer.get("cursor")
        if not cursor:
            return held


def move_between_shards(dollars, source, destination):
    """
    Move dollars of the account's cash from one exchange shard to another.
    Kalshi counts the amount in hundredths of a cent. Returns its answer.
    """
    return signed_request("POST", "/portfolio/intra_exchange_instance_transfer", {
        "source": "event_contract", "destination": "event_contract", "amount": round(dollars * 10000),
        "source_exchange_shard": source, "destination_exchange_shard": destination})


def set_shard_split(percents):
    """
    Have Kalshi keep the account's cash split across exchange shards by
    whole percents adding to 100, {shard: percent}, which it rebalances to
    every 10 seconds, payouts included. Returns its answer.
    """
    return signed_request("POST", "/portfolio/target_balance_allocation", {
        "allocations": [{"exchange_index": shard, "percent": percent} for shard, percent in percents.items()]})


# STREAMING

def update_frame(message_id, sid, tickers, action):
    """
    The command that adds or removes tickers on a live subscription. action is
    'add_markets' or 'delete_markets'.
    """
    return {"id": message_id, "cmd": "update_subscription",
            "params": {"sids": [sid], "market_tickers": tickers, "action": action}}


class KalshiBookStream(BookStream):
    """
    Kalshi's order book channel over a signed connection. Books are given
    from the Yes side, best first, so they look the same as Polymarket US's.
    The subscription asks for use_yes_price, so a resting No order arrives
    already priced as the Yes ask it is, rather than at the No price, which
    Kalshi's default did until it announced it would flip. Every message
    counts as data because the feed has no keepalive replies. A skipped
    sequence number, each subscription counting its own, forces a
    reconnect. Each side's best levels are kept with its book and sorted
    again only when a delta reaches them, so a delta deeper in the book
    costs no sort.

    The same connection follows every market's lifecycle, see LIFECYCLE,
    and says when one of the wanted markets is paused, closed, or decided,
    and when a pause ends, see lifecycle(). A connection lost may miss
    the end of a pause, so the next one reads the markets still said not to
    trade from the markets call, see recheck().
    """

    name = "kalshi"

    def reset(self):
        self.sid = None                     # The order book subscription's id, needed for update commands.
        self.last_seq = {}                  # Each subscription's last sequence number, by its id.
        self.subscribed = asyncio.Event()   # Set once the order book subscription's acknowledgement arrives.
        self.message_id = 3
        self.rechecking = None              # The read of the markets said not to trade, while it runs.

    async def recheck(self):
        """
        Read from the markets call whether the markets said not to trade,
        bar the decided ones, which never trade again, trade now, since a
        lifecycle message saying so may have come while no connection was
        there to hear it. One that cannot be read keeps what was said.
        """
        tickers = sorted(t for t, why in self.states.items() if why != "decided")
        try:
            states = await asyncio.to_thread(market_states, tickers)
        except Exception as e:
            self.log(f"kalshi could not read whether {len(tickers)} markets not trading trade again ({e!r}), keeping them so")
            return
        for ticker, why in states.items():
            if ticker in self.states:
                self.set_state(ticker, why)

    def connect(self):
        return self.open_connection(WS_URL, signed_headers("GET", WS_PATH))

    async def subscribe(self, ws):
        await ws.send(json.dumps({"id": 1, "cmd": "subscribe",
                                  "params": {"channels": ["orderbook_delta"], "market_tickers": sorted(self.wanted), "use_yes_price": True}}))
        await ws.send(json.dumps({"id": 2, "cmd": "subscribe", "params": {"channels": [LIFECYCLE]}}))
        if any(why != "decided" for why in self.states.values()):
            self.rechecking = asyncio.create_task(self.recheck())

    def lifecycle(self, body):
        """
        Note what one lifecycle message says of a wanted market: paused, or
        trading again, when it is deactivated or activated, closed when its
        close moves to now or earlier, trading again when a close moves
        later, and decided when it is determined or settled, which no later
        message changes.
        """
        ticker, event = body.get("market_ticker"), body.get("event_type")
        if ticker not in self.wanted or self.states.get(ticker) == "decided":
            return
        if event == "deactivated":
            self.set_state(ticker, "paused" if body.get("is_deactivated") else None)
        elif event == "activated":
            self.set_state(ticker, None)
        elif event == "close_date_updated" and body.get("close_ts") is not None:
            if body["close_ts"] <= time.time():
                self.set_state(ticker, "closed")
            elif self.states.get(ticker) == "closed":
                self.set_state(ticker, None)
        elif event in ("determined", "settled"):
            self.set_state(ticker, "decided")

    async def send_command(self, ws, action, tickers):
        await self.subscribed.wait()
        kalshi_action = "add_markets" if action == "add" else "delete_markets"
        await ws.send(json.dumps(update_frame(self.message_id, self.sid, tickers, kalshi_action)))
        self.message_id += 1

    def best(self, book, side):
        """
        The best self.depth levels of one side of a book as [price, size]
        lists: the yes bids highest first, the asks, on the no side, lowest first.
        """
        return [[p, s] for p, s in sorted(book[side].items(), reverse=side == "yes")[:self.depth]]

    def apply(self, m):
        """
        Update the local books from one feed message and pass the ticker's
        best levels on, with the matching engine's time of a delta. A delta
        deeper than the levels shown leaves them as they were.
        """
        kind, body = m.get("type"), m.get("msg") or {}
        ticker = body.get("market_ticker")
        if kind == "subscribed":
            if body.get("channel") != LIFECYCLE:
                self.sid = body.get("sid")
                self.subscribed.set()
            return
        if kind == "error":
            self.log(f"kalshi stream error {body}")
            return
        if kind == LIFECYCLE:
            self.lifecycle(body)
            return
        if kind == "orderbook_snapshot" and ticker in self.wanted:
            book = self.books[ticker] = {
                "yes": {float(p): float(s) for p, s in body.get("yes_dollars_fp") or [] if float(s) > 0},
                "no": {float(p): float(s) for p, s in body.get("no_dollars_fp") or [] if float(s) > 0},
            }
            book["shown"] = {side: self.best(book, side) for side in ("yes", "no")}    # The levels last passed on.
        elif kind == "orderbook_delta" and ticker in self.books:
            book = self.books[ticker]
            side = body["side"]
            levels = book[side]
            price = float(body["price_dollars"])
            # Round to cents so summing many deltas does not leave floating point residue.
            size = round(levels.get(price, 0.0) + float(body["delta_fp"]), 2)
            if size > 0:
                levels[price] = size
            else:
                levels.pop(price, None)
            shown = book["shown"][side]
            if len(shown) < self.depth or (price >= shown[-1][0] if side == "yes" else price <= shown[-1][0]):
                book["shown"][side] = self.best(book, side)
        else:
            return
        sent = body.get("ts_ms") if kind == "orderbook_delta" else None      # A snapshot carries no time.
        self.on_book(ticker, book["shown"]["yes"], book["shown"]["no"], sent / 1000 if sent else None)

    def handle(self, raw):
        m = json.loads(raw)
        seq, sid = m.get("seq"), m.get("sid")
        if seq is not None:
            last = self.last_seq.get(sid)
            if last is not None and seq != last + 1:
                raise Reconnect(f"skipped from seq {last} to {seq}")
            self.last_seq[sid] = seq
        self.apply(m)
        return True


# TRADING

# The book side of an order for each action on each outcome. Orders are quoted on the yes side:
# buying no is selling yes, and selling no back is buying yes.
BOOK_SIDES = {("buy", "yes"): "bid", ("sell", "no"): "bid", ("buy", "no"): "ask", ("sell", "yes"): "ask"}


def order_body(ticker, action, outcome, quantity, price, client_id):
    """
    An immediate or cancel limit order for quantity contracts of one side of
    a market, to the hundredth Kalshi counts in, at price or better for that
    side. The order endpoint quotes every order on the yes side, so a no
    side price p is sent as 1 - p. A sale is not reduce only. Kalshi keeps
    one position per market, so where one trade's Yes nets against more No
    held by others, selling it is buying No, which reduce only would cancel
    unfilled every time, the sale sent again and again. An order that would
    trade against one of our own is cancelled rather than filled.
    """
    yes_price = price if outcome == "yes" else 1 - price
    body = {"ticker": ticker, "client_order_id": client_id, "side": BOOK_SIDES[(action, outcome)], "count": str(orders.size(quantity)),
            "price": f"{yes_price:.4f}", "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross"}
    return body


def place_order(ticker, action, outcome, quantity, price, client_id):
    """
    Send an immediate or cancel limit order and return what came back as an
    Answer. action is 'buy' or 'sell', outcome 'yes' or 'no', and
    price the worst price per contract accepted for that outcome. The answer
    gives the contracts filled, their average price on the yes side, so a no
    side fill at p cost 1 - p, and the average fee per contract. The fill is
    counted to the hundredth, as Kalshi does, see orders.exact(). An order
    turned away for lack of cash on its market's exchange shard is
    unfilled, not refused, a sale turned away for lack of cash is
    unfunded, and an order turned away because its market or the exchange
    is not trading is closed, see NOT_TRADING.
    """
    try:
        answer = signed_request("POST", "/portfolio/events/orders", order_body(ticker, action, outcome, quantity, price, client_id))
    except RequestFailed as e:
        if SHORT_SHARD in e.body:
            return orders.unfilled(e, "insufficient shard balance")
        if action == "sell" and NO_FUNDS in e.body:
            return orders.unfunded(e)
        if e.status < 500 and any(word in e.body.lower() for word in NOT_TRADING):
            return orders.closed(e)
        return orders.refused(e) if e.status < 500 else orders.unknown(e)
    except Exception as e:
        return orders.unknown(e)
    filled = orders.exact(float_or_zero(answer.get("fill_count")))
    yes_price = float_or_zero(answer.get("average_fill_price"))
    traded = filled * (yes_price if outcome == "yes" else 1 - yes_price)
    fees = filled * float_or_zero(answer.get("average_fee_paid"))
    paid = traded + fees if action == "buy" else traded - fees
    return orders.Answer(answer.get("order_id"), orders.status(filled, quantity), filled, paid, fees, None, answer)
