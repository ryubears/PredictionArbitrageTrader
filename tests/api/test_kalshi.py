"""
Tests for the Kalshi client's frames and book handling that need no network.
"""

import asyncio
import json
import pytest
import random
from api import bookstream, kalshi


def test_close_time_takes_the_earlier_of_close_and_expected_expiration():
    assert kalshi.close_time({"close_time": "2029-02-13T23:30:00Z", "expected_expiration_time": "2027-02-14T23:30:00Z"}) == "2027-02-14T23:30:00+00:00"
    assert kalshi.close_time({"close_time": "2026-09-24T00:15:00Z"}) == "2026-09-24T00:15:00+00:00"
    assert kalshi.close_time({}) is None


def test_series_are_read_once_in_a_while_and_picked_by_ticker_or_shape(monkeypatch):
    import re
    calls = []

    def paged(path, params, key):
        calls.append((path, params))
        return [{"ticker": t} for t in ("KXSB", "SENATEGA", "SENATEXXX", "CONTROLH", "KXNFLGAME")]

    clock = [1000.0]
    monkeypatch.setattr(kalshi, "paged", paged)
    monkeypatch.setattr(kalshi, "_series", {"at": None, "list": []})
    monkeypatch.setattr(kalshi.time, "time", lambda: clock[0])
    picked = kalshi.fetch_series(["KXSB", "CONTROLH"], [re.compile(r"^SENATE([A-Z]{2})$")])
    assert [s["ticker"] for s in picked] == ["KXSB", "SENATEGA", "CONTROLH"]
    kalshi.fetch_series(["KXSB"])                                  # Within SERIES_SECONDS, from the list already read.
    clock[0] += kalshi.SERIES_SECONDS + 1
    kalshi.fetch_series(["KXSB"])                                  # Read again once it is old.
    assert calls == [("/series", {"limit": 200})] * 2               # Every category, not only Sports, for the elections.


def test_update_frame():
    frame = kalshi.update_frame(7, 3, ["A", "B"], "add_markets")
    assert frame == {"id": 7, "cmd": "update_subscription", "params": {"sids": [3], "market_tickers": ["A", "B"], "action": "add_markets"}}


def test_stream_restates_no_side_as_yes_asks_and_tracks_sid():
    seen = []
    stream = kalshi.KalshiBookStream(["T"], lambda ticker, bids, asks, sent: seen.append((ticker, bids, asks)))
    stream.reset()
    stream.handle('{"type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 4}}')
    assert stream.sid == 4 and stream.subscribed.is_set()
    # With use_yes_price, a resting No order comes priced as the Yes ask it is.
    stream.apply({"type": "orderbook_snapshot", "msg": {"market_ticker": "T", "yes_dollars_fp": [["0.28", "10"], ["0.30", "4"]],
                                                        "no_dollars_fp": [["0.35", "7"], ["0.32", "5"]]}})
    stream.apply({"type": "orderbook_delta", "msg": {"market_ticker": "T", "price_dollars": "0.32", "delta_fp": "-5", "side": "no"}})
    assert seen[0] == ("T", [[0.30, 4.0], [0.28, 10.0]], [[0.32, 5.0], [0.35, 7.0]])
    assert seen[1] == ("T", [[0.30, 4.0], [0.28, 10.0]], [[0.35, 7.0]])


def test_the_levels_passed_on_are_the_best_of_the_whole_book_after_every_delta():
    rng = random.Random(3)
    seen = []
    stream = kalshi.KalshiBookStream(["T"], lambda ticker, bids, asks, sent: seen.append((bids, asks)))
    stream.reset()
    stream.depth = 3
    stream.apply({"type": "orderbook_snapshot", "msg": {"market_ticker": "T", "yes_dollars_fp": [["0.40", "5"], ["0.39", "0"]],
                                                        "no_dollars_fp": [["0.45", "5"]]}})
    for _ in range(3000):
        stream.apply({"type": "orderbook_delta", "msg": {"market_ticker": "T", "price_dollars": f"{rng.randint(1, 99) / 100:.4f}",
                                                         "delta_fp": str(rng.randint(-20, 20)), "side": rng.choice(["yes", "no"])}})
        book = stream.books["T"]
        assert all(size > 0 for side in ("yes", "no") for size in book[side].values())
        assert seen[-1] == ([[p, s] for p, s in sorted(book["yes"].items(), reverse=True)][:3],
                            [[p, s] for p, s in sorted(book["no"].items())][:3])


def test_a_delta_carries_the_matching_engine_time_and_a_snapshot_none():
    sent = []
    stream = kalshi.KalshiBookStream(["T"], lambda ticker, bids, asks, at: sent.append(at))
    stream.reset()
    stream.apply({"type": "orderbook_snapshot", "msg": {"market_ticker": "T", "yes_dollars_fp": [["0.28", "10"]], "no_dollars_fp": []}})
    stream.apply({"type": "orderbook_delta", "msg": {"market_ticker": "T", "price_dollars": "0.30", "delta_fp": "4", "side": "yes",
                                                     "ts_ms": 1790651367684}})
    stream.apply({"type": "orderbook_delta", "msg": {"market_ticker": "T", "price_dollars": "0.30", "delta_fp": "1", "side": "yes"}})
    assert sent == [None, 1790651367.684, None]


def test_stream_asks_to_reconnect_on_a_sequence_gap():
    stream = kalshi.KalshiBookStream(["T"], lambda *args: None)
    stream.reset()
    stream.handle('{"type": "ok", "seq": 1, "msg": {}}')
    stream.handle('{"type": "ok", "seq": 2, "msg": {}}')
    with pytest.raises(bookstream.Reconnect):
        stream.handle('{"type": "ok", "seq": 4, "msg": {}}')


def test_each_subscription_counts_its_own_sequence_and_only_the_books_subscription_takes_update_commands():
    stream = kalshi.KalshiBookStream(["T"], lambda *args: None)
    stream.reset()
    stream.handle('{"type": "subscribed", "id": 1, "msg": {"channel": "orderbook_delta", "sid": 1}}')
    stream.handle('{"type": "subscribed", "id": 2, "msg": {"channel": "market_lifecycle_v2", "sid": 2}}')
    assert stream.sid == 1
    for sid, seq in ((1, 1), (2, 1), (1, 2), (2, 2), (2, 3)):
        stream.handle(json.dumps({"type": "ok", "sid": sid, "seq": seq, "msg": {}}))
    with pytest.raises(bookstream.Reconnect):
        stream.handle('{"type": "ok", "sid": 1, "seq": 4, "msg": {}}')


def lifecycle(ticker, event, **fields):
    return json.dumps({"type": "market_lifecycle_v2", "sid": 2, "msg": {"market_ticker": ticker, "event_type": event, **fields}})


def test_the_lifecycle_says_when_a_wanted_market_is_paused_closed_or_decided(monkeypatch):
    monkeypatch.setattr(kalshi.time, "time", lambda: 1791382560.0)
    said = []
    stream = kalshi.KalshiBookStream(["T", "U", "V"], lambda *args: None)
    stream.on_state = lambda ticker, why: said.append((ticker, why))
    stream.reset()
    stream.handle(lifecycle("OTHER", "deactivated", is_deactivated=True))       # Not a market we follow.
    stream.handle(lifecycle("T", "deactivated", is_deactivated=True))
    stream.handle(lifecycle("T", "deactivated", is_deactivated=False))
    stream.handle(lifecycle("T", "deactivated", is_deactivated=True))
    stream.handle(lifecycle("T", "activated"))
    stream.handle(lifecycle("U", "close_date_updated", close_ts=1791382501))    # Closed early, as a game ends.
    stream.handle(lifecycle("U", "close_date_updated", close_ts=1791400000))    # Its close moved later again.
    stream.handle(lifecycle("V", "close_date_updated", close_ts=1791400000))    # Later, while it trades: nothing to say.
    stream.handle(lifecycle("V", "determined", result="yes"))
    stream.handle(lifecycle("V", "activated"))                                  # Decided stays decided.
    assert said == [("T", "paused"), ("T", None), ("T", "paused"), ("T", None), ("U", "closed"), ("U", None), ("V", "decided")]


def test_a_new_connection_reads_whether_the_markets_said_not_to_trade_trade_again(monkeypatch):
    asked = []

    def market_states(tickers):
        asked.append(tickers)
        return {"T": None, "U": "paused"}

    class FakeSocket:
        async def send(self, text):
            pass

    monkeypatch.setattr(kalshi, "market_states", market_states)
    said = []
    stream = kalshi.KalshiBookStream(["T", "U", "V"], lambda *args: None)
    stream.on_state = lambda ticker, why: said.append((ticker, why))

    async def scenario():
        stream.reset()
        await stream.subscribe(FakeSocket())        # The first connection, with nothing said yet, reads nothing.
        assert stream.rechecking is None
        for ticker, why in (("T", "paused"), ("U", "paused"), ("V", "decided")):
            stream.set_state(ticker, why)
        stream.reset()
        await stream.subscribe(FakeSocket())
        await stream.rechecking
    asyncio.run(scenario())
    assert asked == [["T", "U"]]                     # Not the decided one, which never trades again.
    assert said[-1] == ("T", None) and stream.states == {"U": "paused", "V": "decided"}


def test_market_states_say_why_each_market_is_not_trading_in_batches(monkeypatch):
    calls = []

    def fake_get_json(url, params=None, retries=3):
        calls.append(params["tickers"])
        statuses = {"A": "active", "B": "inactive", "C": "closed", "D": "finalized", "E": "initialized"}
        return {"markets": [{"ticker": t, "status": statuses[t]} for t in params["tickers"].split(",")]}

    monkeypatch.setattr(kalshi, "get_json", fake_get_json)
    monkeypatch.setattr(kalshi, "MARKETS_BATCH", 3)
    monkeypatch.setattr(kalshi, "SLEEP", 0)
    assert kalshi.market_states(["E", "D", "C", "B", "A"]) == {"A": None, "B": "paused", "C": "closed", "D": "decided", "E": "unopened"}
    assert calls == ["A,B,C", "D,E"]


def test_the_exchange_says_whether_each_shard_trades(monkeypatch):
    answer = {"exchange_active": True, "trading_active": True, "exchange_index_statuses": [
        {"exchange_index": 0, "exchange_active": True, "trading_active": True},
        {"exchange_index": 3, "exchange_active": True, "trading_active": False}]}
    monkeypatch.setattr(kalshi, "get_json", lambda url, params=None: answer)
    assert kalshi.exchange_trading() == {0: True, 3: False}
    monkeypatch.setattr(kalshi, "get_json", lambda url, params=None: {"exchange_active": False, "trading_active": True})
    assert kalshi.exchange_trading() == {None: False}         # Without a shard by shard answer, the whole exchange.


def test_results_are_looked_up_in_batches_and_only_finalized_markets_count(monkeypatch):
    calls = []

    def fake_get_json(url, params=None, retries=3):
        calls.append(params["tickers"])
        markets = [{"ticker": t, "status": "finalized", "result": "yes", "settlement_ts": "2026-09-25T03:23:29.0724Z"} for t in params["tickers"].split(",")]
        markets[0]["status"] = "active"
        return {"markets": markets}

    monkeypatch.setattr(kalshi, "get_json", fake_get_json)
    monkeypatch.setattr(kalshi, "MARKETS_BATCH", 2)
    monkeypatch.setattr(kalshi.time, "sleep", lambda s: None)
    out = kalshi.results(["c", "a", "b", "a"])
    assert calls == ["a,b", "c"]                                         # Sorted, deduplicated, two per call.
    assert out == {"b": (1.0, "2026-09-25T03:23:29.072400+00:00")}      # 'a' and 'c' were first in their batch and still active.


def test_results_give_what_a_yes_contract_paid_part_of_a_dollar_for_a_scalar_result(monkeypatch):
    at = "2026-10-09T19:37:23.645455Z"
    markets = [{"ticker": "KXCS2GAME-26OCT081430BGAIM-BG", "status": "finalized", "result": "scalar", "settlement_value_dollars": "0.6000",
                "settlement_ts": at},                                    # A drawn CS2 match, settled at the last fair price.
               {"ticker": "KXMLBNLCSQUAL-26-SD", "status": "finalized", "result": "no", "settlement_value_dollars": "0.0000", "settlement_ts": at},
               {"ticker": "W", "status": "finalized", "result": "yes", "settlement_ts": at},
               {"ticker": "V", "status": "finalized", "result": "", "settlement_ts": at},                  # No result, as when voided.
               {"ticker": "S", "status": "finalized", "result": "scalar", "settlement_ts": at}]            # A scalar result without its value.
    monkeypatch.setattr(kalshi, "get_json", lambda url, params=None, retries=3: {"markets": markets})
    out = kalshi.results([m["ticker"] for m in markets])
    assert out == {"KXCS2GAME-26OCT081430BGAIM-BG": (0.6, "2026-10-09T19:37:23.645455+00:00"),
                   "KXMLBNLCSQUAL-26-SD": (0.0, "2026-10-09T19:37:23.645455+00:00"), "W": (1.0, "2026-10-09T19:37:23.645455+00:00")}


# TRADING

from api.http import RequestFailed


def fake_api(monkeypatch, answers):
    """
    Answer signed calls from a table of {(method, path without query): answer or exception}, recording each call.
    """
    calls = []

    def send_json(method, url, headers=None, body=None, timeout=10):
        path = url.removeprefix(kalshi.BASE)
        calls.append((method, path, headers["signed"], body))
        answer = answers[(method, path.split("?")[0])]
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(kalshi, "send_json", send_json)
    monkeypatch.setattr(kalshi, "signed_headers", lambda method, path: {"signed": f"{method} {path}"})
    return calls


def test_the_balance_and_each_shards_are_read_in_dollars_from_one_call_signed_on_the_full_path(monkeypatch):
    breakdown = [{"exchange_index": 0, "balance": "1000.0000"}, {"exchange_index": 3, "balance": "234.5600"}]
    calls = fake_api(monkeypatch, {("GET", "/portfolio/balance"): {"balance": 123456, "balance_dollars": "1234.5600", "portfolio_value": 0,
                                                                   "balance_breakdown": breakdown}})
    assert kalshi.balances() == (1234.56, {0: 1000.0, 3: 234.56})
    assert calls == [("GET", "/portfolio/balance", "GET /trade-api/v2/portfolio/balance", None)]
    fake_api(monkeypatch, {("GET", "/portfolio/balance"): {"balance": 123456}})       # Before the dollar field and the shards, cents.
    assert kalshi.balances() == (1234.56, {})


def test_the_book_stream_asks_for_yes_side_prices():
    sent = []

    class FakeSocket:
        async def send(self, text):
            sent.append(json.loads(text))
    stream = kalshi.KalshiBookStream(["B", "A"], lambda *args: None)
    asyncio.run(stream.subscribe(FakeSocket()))
    assert sent == [{"id": 1, "cmd": "subscribe", "params": {"channels": ["orderbook_delta"], "market_tickers": ["A", "B"], "use_yes_price": True}},
                    {"id": 2, "cmd": "subscribe", "params": {"channels": ["market_lifecycle_v2"]}}]     # Every market's, untold.


def test_an_order_is_quoted_on_the_yes_side_and_a_sale_may_open_the_other_side():
    assert kalshi.order_body("T", "buy", "no", 5, 0.47, "c1") == {
        "ticker": "T", "client_order_id": "c1", "side": "ask", "count": "5", "price": "0.5300",
        "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross"}     # Buying no at 0.47 is selling yes at 0.53.
    assert kalshi.order_body("T", "buy", "yes", 5, 0.45, "c1")[("side")] == "bid"
    sale = kalshi.order_body("T", "sell", "no", 3, 0.44, "c2")
    assert (sale["side"], sale["price"]) == ("bid", "0.5600")                                  # Selling no at 0.44 is buying yes at 0.56.
    # Not reduce only: where one trade's no nets against more yes held by others, selling it is buying yes, which reduce only cancels.
    assert "reduce_only" not in sale
    assert kalshi.order_body("T", "sell", "yes", 3, 0.44, "c2")["side"] == "ask"


def test_an_order_is_costed_from_its_average_price_and_fee(monkeypatch):
    answer = {"order_id": "o1", "client_order_id": "c1", "fill_count": "4.00", "remaining_count": "0.00",
              "average_fill_price": "0.5350", "average_fee_paid": "0.0175", "ts_ms": 1}
    calls = fake_api(monkeypatch, {("POST", "/portfolio/events/orders"): answer})
    placed = kalshi.place_order("T", "buy", "no", 5, 0.47, "c1")
    # Bought 4 no at a yes price of 0.535 on average, so 0.465 each, and 0.0175 a contract in fees.
    assert (placed.order_id, placed.status, placed.filled, placed.note) == ("o1", "partial", 4, None)
    assert placed.fees == pytest.approx(0.07) and placed.dollars == pytest.approx(4 * 0.465 + 0.07)
    assert calls[0][2] == "POST /trade-api/v2/portfolio/events/orders" and len(calls) == 1


def test_a_sale_nets_the_fee_and_a_fill_is_counted_to_the_hundredth(monkeypatch):
    answer = {"order_id": "o2", "fill_count": "3.50", "remaining_count": "0.00", "average_fill_price": "0.4400", "average_fee_paid": "0.0200"}
    fake_api(monkeypatch, {("POST", "/portfolio/events/orders"): answer})
    placed = kalshi.place_order("T", "sell", "yes", 4, 0.44, "c2")
    assert (placed.status, placed.filled, placed.note) == ("partial", 3.5, None)
    assert placed.dollars == pytest.approx(3.5 * 0.44 - 3.5 * 0.02)
    assert kalshi.order_body("T", "sell", "no", 0.58, 0.46, "c4")["count"] == "0.58"     # Hundredths of a contract, whole ones as before.
    assert kalshi.order_body("T", "buy", "yes", 5, 0.45, "c5")["count"] == "5"


def test_an_order_that_did_not_fill_costs_nothing(monkeypatch):
    fake_api(monkeypatch, {("POST", "/portfolio/events/orders"): {"order_id": "o3", "fill_count": "0.00", "remaining_count": "0.00", "ts_ms": 1}})
    assert kalshi.place_order("T", "buy", "yes", 5, 0.45, "c3")[:5] == ("o3", "unfilled", 0, 0.0, 0.0)


def test_a_sale_turned_away_for_lack_of_cash_is_unfunded_where_a_buy_is_refused(monkeypatch):
    fake_api(monkeypatch, {("POST", "/portfolio/events/orders"): RequestFailed(400, '{"error":{"code":"insufficient_balance"}}')})
    assert kalshi.place_order("T", "sell", "yes", 5, 0.57, "c5").status == "unfunded"
    assert kalshi.place_order("T", "buy", "yes", 5, 0.57, "c6").status == "rejected"


@pytest.mark.parametrize("error, status", [
    (RequestFailed(400, '{"code": "insufficient_balance", "message": "insufficient balance"}'), "rejected"),     # Refused, nothing traded.
    (RequestFailed(400, '{"error":{"code":"trading_is_paused","message":"trading is paused"}}'), "closed"),      # Kalshi stopped: no refusal.
    (RequestFailed(400, '{"error":{"code":"MARKET_NOT_ACTIVE","message":"MARKET_NOT_ACTIVE"}}'), "closed"),     # A paused market.
    (RequestFailed(503, "unavailable"), "error"),                                                                   # Failed on its side, it may have traded.
    # The market's exchange shard lacked the cash, as Kalshi answers it. Nothing traded, and no refusal to halt on.
    (RequestFailed(404, '{"error":{"code":"insufficient_shard_balance","message":"insufficient shard balance","details":'
                        '"Exchange user not found. For Predictions: reference documentation Exchange Sharding documentation."}}'), "unfilled"),
    (TimeoutError("timed out"), "error"),
])
def test_a_refused_order_is_told_apart_from_one_whose_fate_is_unknown(monkeypatch, error, status):
    fake_api(monkeypatch, {("POST", "/portfolio/events/orders"): error})
    answer = kalshi.place_order("T", "buy", "yes", 5, 0.45, "c4")
    assert (answer.status, answer.filled, answer.order_id) == (status, 0, None)
    assert answer.note
