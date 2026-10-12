"""
Tests for the Polymarket US client that need no network: its books, its catalog, and its orders.
"""

import asyncio
import pytest
from api import polymarket_us
from api.http import RequestFailed
from fake_socket import Socket


def test_stream_replaces_the_book_from_each_message():
    seen = []
    stream = polymarket_us.PolymarketUSBookStream(["s"], lambda slug, bids, asks, sent: seen.append((slug, bids, asks)))
    stream.reset()
    assert stream.handle('{"requestId": "md-1", "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA"}') is False
    stream.handle('{"marketData": {"marketSlug": "s", "bids": [{"px": {"value": "0.30"}, "qty": "5"}, {"px": {"value": "0.31"}, "qty": "2"}], "offers": [{"px": {"value": "0.33"}, "qty": "1"}]}}')
    stream.handle('{"marketData": {"marketSlug": "other", "bids": [], "offers": []}}')
    assert seen == [("s", [[0.31, 2.0], [0.30, 5.0]], [[0.33, 1.0]])]


def test_a_book_carries_its_transact_time_except_the_first_of_each_market_on_a_connection():
    sent = []
    stream = polymarket_us.PolymarketUSBookStream(["s"], lambda slug, bids, asks, at: sent.append(at))
    message = '{"marketData": {"marketSlug": "s", "bids": [], "offers": [], "transactTime": "2026-09-29T04:15:16.288755493Z"}}'
    stream.reset()
    stream.handle(message)          # The snapshot a subscription starts with, whose time is its last change.
    stream.handle(message)
    stream.reset()                  # A new connection starts with a snapshot again.
    stream.handle(message)
    assert sent == [None, 1790655316.288755, None]


def test_a_market_whose_state_is_not_open_is_said_not_to_trade_until_it_is_again():
    said, seen = [], []
    stream = polymarket_us.PolymarketUSBookStream(["s"], lambda slug, bids, asks, sent: seen.append(slug))
    stream.on_state = lambda slug, why: said.append((slug, why))
    stream.reset()
    book = '{"marketData": {"marketSlug": "s", "bids": [], "offers": [], "state": "%s"}}'
    stream.handle(book % "MARKET_STATE_OPEN")
    stream.handle(book % "MARKET_STATE_SUSPENDED")
    stream.handle(book % "MARKET_STATE_SUSPENDED")
    stream.handle('{"marketData": {"marketSlug": "s", "bids": [], "offers": []}}')      # No state, which counts as open.
    stream.handle(book % "MARKET_STATE_EXPIRED")
    assert said == [("s", "suspended"), ("s", None), ("s", "expired")] and len(seen) == 5      # Every book still goes on.
    assert polymarket_us.not_trading("MARKET_STATE_MATCH_AND_CLOSE_AUCTION") == "match_and_close_auction"


def test_levels_drop_empty_sizes_and_sort_best_first():
    entries = [{"px": {"value": "0.40"}, "qty": "0"}, {"px": {"value": "0.42"}, "qty": "3"}, {"px": {"value": "0.41"}, "qty": "1"}]
    assert polymarket_us.levels(entries, reverse=True) == [[0.42, 3.0], [0.41, 1.0]]
    assert polymarket_us.levels(entries, reverse=False) == [[0.41, 1.0], [0.42, 3.0]]
    assert polymarket_us.levels(None, reverse=True) == []


def test_error_frames_are_logged_and_do_not_count_as_data():
    logs = []
    stream = polymarket_us.PolymarketUSBookStream(["s"], lambda *args: None, log=logs.append)
    asyncio.run(stream.subscribe(Socket()))
    assert stream.handle('{"requestId": "md-1", "error": "unknown market"}') is False
    assert stream.handle('{"requestId": "md-11", "error": "max subscriptions per connection reached"}') is False     # Never sent.
    assert logs == ["polymarket_us stream error unknown market on md-1",
                    "polymarket_us stream error max subscriptions per connection reached on md-11"]
    assert stream.wanted == {"s"}


def test_each_chunk_of_slugs_subscribes_to_its_books():
    ws = Socket()
    asyncio.run(polymarket_us.PolymarketUSBookStream([f"s{i:03}" for i in range(150)], lambda *args: None).subscribe(ws))
    assert [(f["subscribe"]["requestId"], f["subscribe"]["subscriptionType"], len(f["subscribe"]["marketSlugs"])) for f in ws.sent] == [
        ("md-1", "SUBSCRIPTION_TYPE_MARKET_DATA", 100), ("md-2", "SUBSCRIPTION_TYPE_MARKET_DATA", 50)]


def test_room_counts_the_subscription_requests_left_rather_than_the_slugs():
    stream = polymarket_us.PolymarketUSBookStream([f"s{i:03}" for i in range(950)], lambda *args: None)
    assert polymarket_us.PolymarketUSBookStream.capacity == 1000    # Ten chunks, each taking a request.
    assert stream.room() == 50                          # Before it subscribes, only the slug capacity bounds it.
    ws = Socket()
    asyncio.run(stream.subscribe(ws))
    assert len(ws.sent) == 10 and stream.room() == 0    # Ten requests, the last carrying 50 slugs, leave none for an add.


def test_every_add_spends_requests_however_few_slugs_it_brings():
    stream = polymarket_us.PolymarketUSBookStream([f"s{i:03}" for i in range(520)], lambda *args: None)
    asyncio.run(stream.subscribe(Socket()))
    assert stream.room() == 400                         # Six requests spent, four left.
    for refresh in range(4):
        stream.add([f"r{refresh}-{i}" for i in range(28)])
    assert len(stream.wanted) == 632 and stream.room() == 0     # Counting slugs, it would take 368 more.
    stream.remove(["s000"])
    assert stream.room() == 0                           # With no unsubscribe, a removal gives no request back.


def test_a_refused_subscription_fills_the_connection_and_hands_back_its_slugs():
    logs, handed = [], []
    stream = polymarket_us.PolymarketUSBookStream(["a", "b"], lambda *args: None, log=logs.append)
    stream.on_refused = handed.append
    ws = Socket()
    asyncio.run(stream.subscribe(ws))
    stream.add(["c", "d", "e"])
    asyncio.run(stream.send_command(ws, *stream.commands.get_nowait()))
    stream.remove(["e"])
    assert [frame["subscribe"]["requestId"] for frame in ws.sent] == ["md-1", "md-2"]
    assert stream.handle('{"requestId": "md-2", "error": "max subscriptions per connection reached"}') is False
    assert handed == [["c", "d"]]                       # e was removed since, so it needs no other connection.
    assert stream.wanted == {"a", "b"} and stream.room() == 0
    assert logs == ["polymarket_us refused md-2 as one subscription too many, moving its 2 contracts to another connection"]


# TRADING

def fake_api(monkeypatch, answers):
    """
    Answer signed calls from a table of {(method, path): answer or exception}, recording each call.
    """
    calls = []

    def send_json(method, url, headers=None, body=None, timeout=10):
        path = url.removeprefix(polymarket_us.API)
        calls.append((method, path, headers["signed"], body))
        answer = answers[(method, path)]
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(polymarket_us, "send_json", send_json)
    monkeypatch.setattr(polymarket_us, "signed_headers", lambda method, path: {"signed": f"{method} {path}"})
    return calls


def test_a_game_and_a_race_start_when_their_event_does_and_a_future_has_no_start_time(monkeypatch):
    def event(slug, **ids):
        return {"slug": slug, "startTime": "2026-09-29T21:00:00Z", "markets": [{"slug": f"aec-{slug}", "id": 1}], **ids}
    events = [event("nfl-pit-cle-2026-10-01", gameId=19503, sportradarGameId="a"),
              event("nhl-fla-car-2026-09-29", sportradarGameId="198c21f5"),       # NHL preseason games carry only Sportradar's.
              event("nhl-champ-2027-06-18-w"),                                    # A future.
              event("nhl-hart-2027-06-09-w", sportradarGameId="type_hart_trophy"),    # An award, with an id like a game's.
              event("f1-sasgp-2026-10-11-w", gameId=51)]                          # A race, its winners typed as futures.
    events[3]["markets"][0] |= {"sportsMarketType": "futures", "question": "NHL Hart Memorial Trophy Winner", "title": "Connor McDavid",
                                "endDate": "2027-06-24T04:00:00Z"}
    events[3]["endDate"] = "2027-06-09T23:59:00Z"
    events[4]["markets"][0] |= {"sportsMarketType": "futures", "title": "Max Verstappen"}
    events[4] |= {"startTime": "2026-10-11T12:00:00Z", "endDate": "2026-10-11T23:59:00Z"}
    monkeypatch.setattr(polymarket_us, "fetch_events", lambda tag: events)
    found = {c.event_id: c for c in polymarket_us.contracts("nhl", ["nhl"])}
    assert {event: c.start_time for event, c in found.items()} == {
        "nfl-pit-cle-2026-10-01": "2026-09-29T21:00:00+00:00", "nhl-fla-car-2026-09-29": "2026-09-29T21:00:00+00:00",
        "nhl-champ-2027-06-18-w": None, "nhl-hart-2027-06-09-w": None, "f1-sasgp-2026-10-11-w": "2026-10-11T12:00:00+00:00"}
    assert found["nhl-hart-2027-06-09-w"].title == "Connor McDavid"         # A future's title is its player, not the event's question.
    assert found["nhl-hart-2027-06-09-w"].close_time == "2027-06-09T23:59:00+00:00"   # The award, not the market's two weeks after.


def test_results_give_what_a_long_contract_paid_one_event_at_a_time(monkeypatch):
    def market(slug, status, long_price):
        return {"slug": slug, "status": status, "endDate": "2026-10-09T14:25:43Z",
                "marketSides": [{"long": True, "price": long_price}, {"long": False, "price": f"{1 - float(long_price):g}"}]}
    events = {"aqc-mlb-2026-10-09-nlcsq": [market("nlcsq-sd", "MARKET_STATUS_RESOLVED", "0"), market("nlcsq-lad", "MARKET_STATUS_RESOLVED", "1"),
                                           market("nlcsq-mil", "MARKET_STATUS_OPEN", "0.55")],
              "cs2-aime-bg-2026-10-08": [market("aec-cs2-aime-bg-2026-10-08", "MARKET_STATUS_RESOLVED", "0.41")]}   # Paid the last fair price.
    calls = []

    def fake_get_json(url, params=None, retries=3):
        calls.append(("get", params["slug"]))
        return {"events": [{"slug": params["slug"], "markets": events[params["slug"]]}]}
    monkeypatch.setattr(polymarket_us, "get_json", fake_get_json)
    monkeypatch.setattr(polymarket_us.time, "sleep", lambda s: calls.append(("sleep", s)))
    out = polymarket_us.results(["cs2-aime-bg-2026-10-08", "aqc-mlb-2026-10-09-nlcsq", "cs2-aime-bg-2026-10-08"])
    at = "2026-10-09T14:25:43+00:00"
    assert out == {"nlcsq-sd": (0.0, at), "nlcsq-lad": (1.0, at), "aec-cs2-aime-bg-2026-10-08": (0.41, at)}     # The open market is left out.
    # Each event once, with a pause between them, so a pass never bursts against the limit the live orders share.
    assert calls == [("get", "aqc-mlb-2026-10-09-nlcsq"), ("sleep", polymarket_us.RESULTS_SLEEP), ("get", "cs2-aime-bg-2026-10-08")]


def test_balance_is_the_buying_power_of_the_dollar_balance(monkeypatch):
    calls = fake_api(monkeypatch, {("GET", "/account/balances"): {"balances": [{"currentBalance": 1500.0, "buyingPower": 1234.5, "currency": "USD"}]}})
    assert polymarket_us.balance() == 1234.5
    assert calls[0][2] == "GET /v1/account/balances"


def test_an_order_prices_the_short_side_on_the_long_side():
    body = polymarket_us.order_body("slug", "buy", "no", 5, 0.47)
    assert body["intent"] == "ORDER_INTENT_BUY_SHORT" and body["price"] == {"value": "0.53", "currency": "USD"}
    assert (body["tif"], body["type"], body["synchronousExecution"], body["quantity"]) == (
        "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL", "ORDER_TYPE_LIMIT", True, 5)
    assert polymarket_us.order_body("slug", "sell", "yes", 3, 0.5)["price"]["value"] == "0.5"
    assert polymarket_us.order_body("slug", "sell", "yes", 3, 0.5)["intent"] == "ORDER_INTENT_SELL_LONG"


def test_an_order_is_costed_from_its_executions(monkeypatch):
    executions = [
        {"type": "EXECUTION_TYPE_PARTIAL_FILL", "lastShares": "3", "lastPx": {"value": "0.54", "currency": "USD"},
         "commissionNotionalCollected": {"value": "0.05", "currency": "USD"}},
        {"type": "EXECUTION_TYPE_PARTIAL_FILL", "lastShares": "1", "lastPx": {"value": "0.53", "currency": "USD"},
         "commissionNotionalCollected": {"value": "0.02", "currency": "USD"}},
        {"type": "EXECUTION_TYPE_CANCELED"}]
    calls = fake_api(monkeypatch, {("POST", "/orders"): {"id": "p1", "executions": executions}})
    answer = polymarket_us.place_order("slug", "buy", "no", 5, 0.47, "c1")
    # Short side fills at long prices 0.54 and 0.53 cost 0.46 and 0.47.
    assert (answer.order_id, answer.status, answer.filled, answer.fees) == ("p1", "partial", 4, pytest.approx(0.07))
    assert answer.dollars == pytest.approx(3 * 0.46 + 0.47 + 0.07)
    assert calls[0][2] == "POST /v1/orders"


def piece(kind, shares, long_price, fee):
    """
    One execution of an answer, as Polymarket US gives it, trimmed to what is read.
    """
    return {"type": f"EXECUTION_TYPE_{kind}", "lastShares": shares, "lastPx": {"value": long_price, "currency": "USD"},
            "commissionNotionalCollected": {"value": fee, "currency": "USD"}}


def test_an_order_filled_in_pieces_of_a_contract_counts_them_together(monkeypatch):
    # A live answer: no on Michigan State 4.5 wins, 1 contract, filled as 0.1, 0.89, and 0.01 of one at a yes price
    # of 0.58. Each piece cut to a whole contract on its own read as none filled, with a fee for it.
    executions = [piece("NEW", "0.0000", "0.0000", "0.0000"), piece("PARTIAL_FILL", "0.1000", "0.5800", "0.0000"),
                  piece("PARTIAL_FILL", "0.8900", "0.5800", "0.0200"), piece("FILL", "0.0100", "0.5800", "0.0000")]
    fake_api(monkeypatch, {("POST", "/orders"): {"id": "CTGB5ATN8YBE", "executions": executions}})
    answer = polymarket_us.place_order("aachc-cfb-wins-ou-2026-11-28-mst", "buy", "no", 1, 0.45, "c11")
    assert (answer.status, answer.filled, answer.note) == ("filled", 1, None)
    assert answer.dollars == pytest.approx(1 - 0.58 + 0.02) and answer.fees == pytest.approx(0.02)


def test_a_fill_is_counted_to_the_hundredth(monkeypatch):
    executions = [piece("PARTIAL_FILL", "0.1000", "0.5000", "0.0000"), piece("PARTIAL_FILL", "2.8000", "0.5000", "0.0300"),
                  {"type": "EXECUTION_TYPE_CANCELED"}]
    fake_api(monkeypatch, {("POST", "/orders"): {"id": "p12", "executions": executions}})
    answer = polymarket_us.place_order("slug", "buy", "yes", 3, 0.5, "c12")
    assert (answer.status, answer.filled, answer.note) == ("partial", 2.9, None)
    assert answer.dollars == pytest.approx(2.9 * 0.5 + 0.03) and answer.fees == pytest.approx(0.03)
    executions = [piece("PARTIAL_FILL", "1.0000", "0.7000", "0.0100"), piece("PARTIAL_FILL", "0.9000", "0.7000", "0.0100"),
                  {"type": "EXECUTION_TYPE_CANCELED"}]
    fake_api(monkeypatch, {("POST", "/orders"): {"id": "p16", "executions": executions}})
    answer = polymarket_us.place_order("slug", "sell", "yes", 2, 0.7, "c16")
    assert (answer.status, answer.filled) == ("partial", 1.9) and answer.dollars == pytest.approx(1.9 * 0.7 - 0.02)


def test_an_order_asks_for_hundredths_of_a_contract_and_whole_ones_as_before():
    assert polymarket_us.order_body("slug", "sell", "no", 0.58, 0.46)["quantity"] == 0.58
    assert polymarket_us.order_body("slug", "sell", "no", 3.0000000001, 0.46)["quantity"] == 3


def test_a_stored_answer_reads_the_same_as_it_did_when_it_came(monkeypatch):
    executions = [piece("PARTIAL_FILL", "0.1000", "0.7100", "0.0000"), piece("FILL", "0.9000", "0.7100", "0.0100")]
    answer = polymarket_us.read_answer({"id": "p13", "executions": executions}, "sell", "yes", 1)
    assert (answer.status, answer.filled) == ("filled", 1) and answer.dollars == pytest.approx(0.71 - 0.01)
    assert polymarket_us.read_answer({"id": "p14", "executions": [piece("PARTIAL_FILL", "1", "0.5", "0")]}, "buy", "yes", 3) is None


def test_a_sale_nets_the_fee(monkeypatch):
    executions = [{"type": "EXECUTION_TYPE_FILL", "lastShares": "3", "lastPx": {"value": "0.44"}, "commissionNotionalCollected": {"value": "0.05"}}]
    fake_api(monkeypatch, {("POST", "/orders"): {"id": "p2", "executions": executions}})
    answer = polymarket_us.place_order("slug", "sell", "yes", 3, 0.44, "c2")
    assert (answer.status, answer.filled) == ("filled", 3) and answer.dollars == pytest.approx(3 * 0.44 - 0.05)


def test_a_rejected_order_says_why(monkeypatch):
    executions = [{"type": "EXECUTION_TYPE_REJECTED", "orderRejectReason": "insufficient buying power"}]
    fake_api(monkeypatch, {("POST", "/orders"): {"id": "p3", "executions": executions}})
    assert polymarket_us.place_order("slug", "buy", "yes", 3, 0.44, "c3")[1:6] == ("rejected", 0, 0.0, 0.0, "insufficient buying power")


@pytest.mark.parametrize("error, status", [(RequestFailed(400, "bad price"), "rejected"), (RequestFailed(502, "bad gateway"), "error"),
                                           (ConnectionResetError("reset"), "error")])
def test_a_refused_order_is_told_apart_from_one_whose_fate_is_unknown(monkeypatch, error, status):
    fake_api(monkeypatch, {("POST", "/orders"): error})
    assert polymarket_us.place_order("slug", "buy", "yes", 3, 0.44, "c4").status == status


def test_the_latency_stopgap_leaves_an_order_unfilled_rather_than_refused(monkeypatch):
    executions = [{"type": "EXECUTION_TYPE_REJECTED", "text": "Global Rate Limit Exceeded"}]
    fake_api(monkeypatch, {("POST", "/orders"): {"id": "p5", "executions": executions}})
    answer = polymarket_us.place_order("slug", "buy", "yes", 3, 0.44, "c5")
    assert (answer.status, answer.filled, answer.note) == ("unfilled", 0, "latency stopgap: Global Rate Limit Exceeded")
    fake_api(monkeypatch, {("POST", "/orders"): RequestFailed(429, '{"status": 429, "message": "Global Rate Limit Exceeded"}')})
    answer = polymarket_us.place_order("slug", "buy", "yes", 3, 0.44, "c6")
    assert (answer.status, answer.note, answer.response["status"]) == (
        "unfilled", 'latency stopgap: {"status": 429, "message": "Global Rate Limit Exceeded"}', 429)


def test_an_order_turned_away_for_lack_of_cash_is_unfunded_rather_than_refused(monkeypatch):
    fake_api(monkeypatch, {("POST", "/orders"): RequestFailed(400, '{"code":9,"message":"You don\'t have enough funds for this order.","details":[]}')})
    answer = polymarket_us.place_order("slug", "sell", "no", 0.42, 0.13, "c17")
    assert (answer.status, answer.filled) == ("unfunded", 0) and answer.note.startswith("not enough funds: ")


def test_an_order_turned_away_because_its_market_is_not_trading_is_closed_rather_than_refused(monkeypatch):
    executions = [{"type": "EXECUTION_TYPE_REJECTED", "orderRejectReason": "ORD_REJECT_REASON_EXCHANGE_CLOSED"}]
    fake_api(monkeypatch, {("POST", "/orders"): {"id": "p8", "executions": executions}})
    answer = polymarket_us.place_order("slug", "buy", "yes", 3, 0.44, "c8")
    assert (answer.status, answer.filled, answer.note) == ("closed", 0, "market not trading: ORD_REJECT_REASON_EXCHANGE_CLOSED")
    fake_api(monkeypatch, {("POST", "/orders"): RequestFailed(400, '{"code": 9, "message": "market is closed"}')})
    assert polymarket_us.place_order("slug", "buy", "yes", 3, 0.44, "c9").status == "closed"


def test_an_order_waits_as_long_as_the_stopgap_for_its_answer():
    assert polymarket_us.order_body("slug", "buy", "yes", 3, 0.44)["maxBlockTime"] == "5"


def test_an_order_turned_away_for_no_liquidity_is_unfilled_rather_than_refused(monkeypatch):
    executions = [{"type": "EXECUTION_TYPE_REJECTED", "orderRejectReason": "ORD_REJECT_REASON_NO_LIQUIDITY"}]
    fake_api(monkeypatch, {("POST", "/orders"): {"id": "p7", "executions": executions}})
    answer = polymarket_us.place_order("slug", "buy", "yes", 3, 0.44, "c7")
    assert (answer.status, answer.filled, answer.note) == ("unfilled", 0, "no liquidity: ORD_REJECT_REASON_NO_LIQUIDITY")


def test_an_answer_that_does_not_say_how_the_order_ended_is_followed_by_a_look_at_the_order(monkeypatch):
    partial = [{"type": "EXECUTION_TYPE_PARTIAL_FILL", "lastShares": "1", "lastPx": {"value": "0.54"}}]
    order = {"id": "p8", "state": "ORDER_STATE_CANCELED", "cumQuantity": 3, "avgPx": {"value": "0.54", "currency": "USD"},
             "commissionNotionalTotalCollected": {"value": "0.05", "currency": "USD"}}
    calls = fake_api(monkeypatch, {("POST", "/orders"): {"id": "p8", "executions": partial}, ("GET", "/order/p8"): {"order": order}})
    answer = polymarket_us.place_order("slug", "buy", "no", 5, 0.47, "c8")
    # The order went on to fill 3 before the rest was canceled, bought as no at a yes price of 0.54.
    assert (answer.order_id, answer.status, answer.filled) == ("p8", "partial", 3)
    assert answer.dollars == pytest.approx(3 * 0.46 + 0.05) and answer.fees == pytest.approx(0.05)
    assert calls[1][:3] == ("GET", "/order/p8", "GET /v1/order/p8")


def test_a_look_at_the_order_counts_its_whole_contracts(monkeypatch):
    partial = [piece("PARTIAL_FILL", "0.1000", "0.5400", "0.0000")]
    order = {"id": "p15", "state": "ORDER_STATE_CANCELED", "cumQuantity": 2.9999999, "avgPx": {"value": "0.54"},
             "commissionNotionalTotalCollected": {"value": "0.05"}}
    fake_api(monkeypatch, {("POST", "/orders"): {"id": "p15", "executions": partial}, ("GET", "/order/p15"): {"order": order}})
    answer = polymarket_us.place_order("slug", "buy", "yes", 3, 0.55, "c15")
    assert (answer.status, answer.filled, answer.note) == ("filled", 3, None)


def test_an_order_that_has_still_not_ended_has_an_unknown_fate(monkeypatch):
    fake_api(monkeypatch, {("POST", "/orders"): {"id": "p9", "executions": []},
                           ("GET", "/order/p9"): {"order": {"id": "p9", "state": "ORDER_STATE_PENDING_NEW"}}})
    answer = polymarket_us.place_order("slug", "buy", "yes", 5, 0.44, "c9")
    assert (answer.status, answer.order_id, answer.filled) == ("error", "p9", 0) and "still ORDER_STATE_PENDING_NEW" in answer.note
    fake_api(monkeypatch, {("POST", "/orders"): {"executions": []}})
    assert polymarket_us.place_order("slug", "buy", "yes", 5, 0.44, "c10").status == "error"      # Not even an order id to look up.
