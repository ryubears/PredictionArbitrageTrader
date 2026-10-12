"""
Tests for the live executor, with the venues' answers to its orders scripted by each test.
"""

import asyncio
import dataclasses
import threading
import pytest
from api import orders
from common.timeutil import epoch, shift
from db import database
from db.models import Book, Order
from engine.components.money.live import LiveBalances
from engine.components.money.paper import PaperBalances
from engine.components.trading.live import LiveExecutor
from engine.components.trading.paper import PaperExecutor
from engine.helper import config
from trade_setup import FEES, NO, NO_K_FEES, NO_PM_FEES, NOW, PAIR, PAYS_AT, YES, stored
from trade_setup import books as shared_books


def fills(n=None):
    """
    A scripted answer that fills n contracts at the limit, or the whole order when n is None, with no fees.
    """
    def answer(quantity, price):
        got = quantity if n is None else min(n, quantity)
        return orders.Answer(f"venue-{got}", orders.status(got, quantity), got, got * price, 0.0, None, {"filled": got})
    return answer


REFUSED = orders.Answer(None, "rejected", 0, 0.0, 0.0, "insufficient balance", {})
UNKNOWN = orders.unknown(TimeoutError("timed out"))


class Venues:
    """
    Stands in for the venues' place_order: each venue answers from its own script, and every order is recorded.
    """

    def __init__(self, **scripts):
        self.scripts = {"kalshi": [], "polymarket_us": [], **scripts}
        self.orders = []

    def place(self):
        def for_venue(venue):
            def place_order(contract_id, action, outcome, quantity, price, client_id):
                self.orders.append((venue, action, outcome, quantity, price))
                answer = self.scripts[venue].pop(0)
                return answer(quantity, price) if callable(answer) else answer
            return place_order
        return {venue: for_venue(venue) for venue in self.scripts}


def books(size=20, **prices):
    """
    The shared books with 20 contracts a level, so the trades here ask for half of them, 10 contracts.
    """
    return shared_books(size=size, **prices)


class FakeNotifier:
    """
    Stands in for the Notifier, keeping what it was asked to send as (kind, subject, body).
    """

    def __init__(self):
        self.sent = []

    def send(self, kind, subject, body, now=None):
        self.sent.append((kind, subject, body))


def executor(tmp_path, venues, latest=None, notifier=None, logs=None, read=True, balance=1000.0, positions=None, now=NOW, in_play=False):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = LiveBalances(lambda m: None, {"kalshi": lambda: (balance, {}), "polymarket_us": lambda: (balance, {})})
    if read:
        asyncio.run(cash.refresh(now))
    latest = books() if latest is None else latest
    ex = LiveExecutor(conn, cash, lambda: latest, (logs.append if logs is not None else lambda m: None), clock=lambda: now,
                      place=venues.place(), notifier=notifier, positions=positions or {"polymarket_us": lambda: {}}, in_play=in_play)
    return conn, cash, ex


def trade(ex, times=1):
    """
    Send the signal inside a loop as many times as asked, one trade after the other, and wait for each to finish.
    """
    async def scenario():
        sent = []
        for _ in range(times):
            sent.append(ex.signal(PAIR, YES, NO, 1 - 0.45 - 0.47, 100, FEES, NOW))
            while ex.tasks:
                await asyncio.gather(*ex.tasks)
        return sent
    return asyncio.run(scenario())


def test_both_legs_are_sent_as_real_orders_and_every_order_is_stored(tmp_path):
    venues = Venues(polymarket_us=[fills()], kalshi=[fills()])
    conn, cash, ex = executor(tmp_path, venues, books(size=100))
    assert trade(ex) == [True]
    t = stored(conn, "trades")[0]
    # Half the visible 100, with no cap, as on paper.
    assert (t["mode"], t["status"], t["quantity"], t["yes_filled"], t["no_filled"], t["matched"]) == ("live", "filled", 50, 50, 50, 50)
    assert t["profit"] == pytest.approx(50 * (1 - 0.45 - 0.47))
    # Yes is the Polymarket US contract itself. No is the other side of the Kalshi contract, bought at no more than 1 - 0.53.
    assert sorted(venues.orders) == [("kalshi", "buy", "no", 50, 0.47), ("polymarket_us", "buy", "yes", 50, 0.45)]
    stored_orders = stored(conn, "orders")
    assert {(o["trade_id"], o["purpose"], o["status"], o["filled"], o["venue_order_id"]) for o in stored_orders} == {(t["id"], "open", "filled", 50, "venue-50")}
    assert all(o["client_id"] and o["answered_at"] == NOW and o["response"] == '{"filled": 50}' for o in stored_orders)
    assert cash.amounts == pytest.approx({"polymarket_us": 1000 - 22.5, "kalshi": 1000 - 23.5})
    assert stored(conn, "ledger") == []                                 # Live money keeps no ledger of ours.


def test_a_trade_opens_with_a_fraction_of_a_contract_down_to_a_tenth(tmp_path):
    # Half of the 0.74 shown is 0.37, which live opens in hundredths, on futures and games alike.
    venues = Venues(polymarket_us=[fills()], kalshi=[fills()])
    conn, cash, ex = executor(tmp_path, venues, books(size=0.74))
    assert trade(ex) == [True]
    assert sorted(venues.orders) == [("kalshi", "buy", "no", 0.37, 0.47), ("polymarket_us", "buy", "yes", 0.37, 0.45)]
    t = stored(conn, "trades")[0]
    assert (t["quantity"], t["status"], t["matched"]) == (0.37, "filled", 0.37)
    # Half of 0.18 is 0.09, under config.LIVE_MIN_CONTRACTS: no trade.
    venues = Venues(polymarket_us=[fills()], kalshi=[fills()])
    conn, cash, ex = executor(tmp_path / "under", venues, books(size=0.18))
    assert trade(ex) == [False] and venues.orders == [] and stored(conn, "trades") == []


def test_a_leg_that_filled_short_is_evened_by_selling_the_other_back_no_lower_than_the_books_said(tmp_path):
    venues = Venues(polymarket_us=[fills(), fills()], kalshi=[fills(4)])
    logs = []
    conn, cash, ex = executor(tmp_path, venues, logs=logs)
    trade(ex)
    t = stored(conn, "trades")[0]
    # Buying the 6 missing on Kalshi at 0.47 would earn 8 cents each but hold the money until the game pays, so the 6 extra yes
    # are sold back at the 0.44 bid, a cent under what they cost.
    assert venues.orders[-1] == ("polymarket_us", "sell", "yes", 6, 0.44)
    assert (t["yes_held"], t["no_held"], t["matched"], t["status"], t["hedge"]) == (4, 4, 4, "partial", "sold back 6 of 6 on polymarket_us")
    assert [(o["purpose"], o["quantity"], o["limit_price"]) for o in stored(conn, "orders")][-1] == ("flatten", 6, 0.44)
    assert logs[-1].startswith("live partial: nfl game_winner 2026-09-22 CAR@ATL CAR")


def bids_gone(latest):
    """
    A scripted Kalshi answer: its bids were taken before our order arrived, so nothing filled and the book shows none.
    """
    def answer(quantity, price):
        latest[("kalshi", "k")] = Book("kalshi", "k", NOW, [], [[0.54, 100]])
        return fills(0)(quantity, price)
    return answer


def test_a_sale_back_is_sent_no_lower_than_the_books_said(tmp_path):
    latest = books()
    venues = Venues(polymarket_us=[fills(), fills()], kalshi=[bids_gone(latest)])
    conn, cash, ex = executor(tmp_path, venues, latest)
    trade(ex)
    t = stored(conn, "trades")[0]
    assert venues.orders[-1] == ("polymarket_us", "sell", "yes", 10, 0.44)
    assert (t["yes_held"], t["no_held"], t["hedge"]) == (0, 0, "sold back 10 of 10 on polymarket_us")
    assert t["hedge_pnl"] == pytest.approx(10 * (0.44 - 0.45))


def test_an_order_of_unknown_outcome_sets_its_trade_aside_and_trading_goes_on(tmp_path):
    venues = Venues(polymarket_us=[fills(), fills()], kalshi=[UNKNOWN, fills()])
    notifier, logs = FakeNotifier(), []
    conn, cash, ex = executor(tmp_path, venues, notifier=notifier, logs=logs)
    trade(ex)
    assert len(venues.orders) == 2                                      # Nothing is sent to flatten, since what the trade holds is unknown.
    t = stored(conn, "trades")[0]
    kalshi_order = next(o for o in stored(conn, "orders") if o["venue"] == "kalshi")
    assert (kalshi_order["status"], kalshi_order["filled"]) == ("error", 0)
    assert (t["yes_held"], t["no_held"]) == (10, 0) and ex.exposed == {}
    assert t["hedge"] == f"10 exposed, set aside, order {kalshi_order['id']} has an unknown outcome, no leg error: TimeoutError('timed out')"
    assert notifier.sent == []                                         # Only a halt is emailed.
    (line,) = [line for line in logs if line.startswith(f"live trade {t['id']} set aside")]
    assert f"order {kalshi_order['id']}, buy 10 no of kalshi k" in line and f"client id {kalshi_order['client_id']}" in line
    assert ex.halted is None and trade(ex) == [True]                    # The next trade goes ahead.
    assert stored(conn, "trades")[1]["status"] == "filled"
    assert executor(tmp_path, Venues())[2].exposed == {}                # A restart leaves it to a human too.


def test_live_trading_halts_at_three_unknown_outcomes_in_twenty_orders(tmp_path):
    venues = Venues(polymarket_us=[UNKNOWN, fills(), fills(), fills(), UNKNOWN] + [fills()] * 5, kalshi=[fills(), fills(), UNKNOWN] + [fills()] * 7)
    notifier = FakeNotifier()
    conn, cash, ex = executor(tmp_path, venues, notifier=notifier)
    assert trade(ex, 2) == [True, True] and ex.halted is None           # One unknown in four orders.
    assert trade(ex, 2) == [True, True] and ex.halted is None           # Two in eight, not in a row.
    assert trade(ex, 1) == [True] and ex.halted.startswith("3 of the last ")      # The third, in the tenth order, halts.
    unknown = ", ".join(str(o["id"]) for o in stored(conn, "orders") if o["status"] == "error")
    assert f"orders had an unknown outcome, orders {unknown}, the last on " in ex.halted and trade(ex) == [False]
    assert ex.brakes.stopped == ex.halted                               # Flattening stops too.
    assert [kind for kind, _, _ in notifier.sent] == ["halt"]           # One email, naming every order to look up.


def test_a_venue_refusing_orders_in_a_row_halts_live_trading(tmp_path):
    venues = Venues(polymarket_us=[REFUSED, fills(0), REFUSED, REFUSED, REFUSED])
    conn, cash, ex = executor(tmp_path, venues)
    assert trade(ex, 2) == [True, True] and not ex.halted               # An order Polymarket US took, though it filled nothing, starts it over.
    assert trade(ex, 4) == [True, True, True, False]
    assert ex.halted == "polymarket_us refused 3 orders in a row, the last with: insufficient balance"
    assert ex.brakes.stopped == ex.halted                               # Flattening stops too.
    assert {venue for venue, *_ in venues.orders} == {"polymarket_us"}  # Kalshi's leg is never sent after a miss.


def test_flattening_losses_over_the_limit_halt_live_trading(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LIVE_MAX_LOSS_SHARE", 0.00004)          # A tenth of a dollar lost on 2,000 is 0.005%.
    latest = books()
    venues = Venues(polymarket_us=[fills(), fills()], kalshi=[bids_gone(latest)])
    conn, cash, ex = executor(tmp_path, venues, latest)
    trade(ex)                                                           # Sold back at a cent under cost, 10 cents in all, and flat.
    assert ex.halted == "the live trades decided in the last 6 hours lost 0.10$, 0% of the live money, over the 0% limit"
    assert ex.brakes.stopped is None                                    # Flattening goes on.


def at(ex, now):
    """
    Move the executor's clock, and its brakes', to now.
    """
    ex.clock = ex.brakes.clock = lambda: now


def test_after_a_loss_halt_flattening_goes_on_until_its_orders_fail(tmp_path):
    latest = books()
    venues = Venues(polymarket_us=[fills(), fills(0), fills(4)] + [REFUSED] * 3, kalshi=[bids_gone(latest)])
    notifier = FakeNotifier()
    conn, cash, ex = executor(tmp_path, venues, latest, notifier=notifier)
    trade(ex)                                                           # Kalshi filled nothing, and the sale back found nothing.
    assert list(ex.exposed) == [1]
    ex.brakes.halt("the live trades decided in the last 6 hours lost too much")
    assert trade(ex) == [False] and len(venues.orders) == 3             # No new trades.
    assert "; HALTED, still flattening: the live trades decided" in ex.summary()
    at(ex, "2026-09-20T17:31:00+00:00")                                  # A minute on, when a sale that sold nothing is tried again.
    asyncio.run(ex.retry(ex.clock()))
    assert venues.orders[-1] == ("polymarket_us", "sell", "yes", 10, 0.44) and stored(conn, "trades")[0]["yes_held"] == 6
    for minute in (32, 33, 34):
        at(ex, f"2026-09-20T17:{minute}:00+00:00")
        asyncio.run(ex.retry(ex.clock()))                               # Refused each time, a minute apart.
    assert ex.brakes.stopped == "polymarket_us refused 3 orders in a row, the last with: insufficient balance"
    at(ex, "2026-09-20T17:35:00+00:00")
    asyncio.run(ex.retry(ex.clock()))
    assert len(venues.orders) == 7 and list(ex.exposed) == [1]          # Nothing more is sent.
    assert "; HALTED, no orders at all: polymarket_us refused 3 orders in a row" in ex.summary()
    (_, first, halted), (_, second, stopped) = notifier.sent
    assert (first, second) == ("PredictionArbitrageTrader live trading halted", "PredictionArbitrageTrader live flattening stopped")
    assert "Exposed trades are still flattened." in halted and "No more orders are sent, flattening included." in stopped


def test_nothing_is_traded_before_the_first_balance_reading(tmp_path):
    venues = Venues()
    conn, cash, ex = executor(tmp_path, venues, read=False)
    assert trade(ex) == [False] and venues.orders == []


def test_each_executor_trades_only_its_own_money(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    with pytest.raises(ValueError, match="a live executor cannot trade paper money"):
        LiveExecutor(conn, PaperBalances(conn), lambda: {})
    with pytest.raises(ValueError, match="a paper executor cannot trade live money"):
        PaperExecutor(conn, LiveBalances(), lambda: {})


def test_an_answer_that_cannot_be_read_counts_as_an_unknown_outcome(tmp_path):
    def unreadable(quantity, price):
        raise KeyError("executions")
    venues = Venues(polymarket_us=[fills()], kalshi=[unreadable])
    conn, cash, ex = executor(tmp_path, venues)
    trade(ex)
    assert "KeyError('executions')" in ex.set_aside[1] or "KeyError('executions')" in stored(conn, "orders")[1]["note"]
    assert 1 in ex.set_aside and ex.halted is None
    assert cash.reserved == {"kalshi": 0, "polymarket_us": 0}          # The reservation came back all the same.


def test_a_halt_outlasts_a_restart_until_a_human_removes_the_file_and_then_starts_over(tmp_path, halt_file):
    from engine.components.trading import notify
    conn = database.connect(tmp_path / "t.sqlite")
    notifier = notify.Notifier(conn, lambda m: None, sender=lambda *args: None)     # Halts are stored as alerts, as the session wires them.
    no_bids = {**books(), ("polymarket_us", "pm"): Book("polymarket_us", "pm", NOW, [], [[0.45, 100]])}     # Nothing to sell into.
    venues = Venues(polymarket_us=[fills()] * 3, kalshi=[REFUSED] * 3)
    conn, cash, ex = executor(tmp_path, venues, no_bids, notifier=notifier)
    trade(ex, 3)
    assert halt_file.read_text().startswith("2026-09-20T17:30:00 UTC every order stopped: kalshi refused 3 orders in a row")
    logs = []
    venues = Venues(polymarket_us=[fills(), fills()], kalshi=[REFUSED, fills()])
    conn, cash, again = executor(tmp_path, venues, logs=logs)           # A crash or a deploy restarts the process.
    assert again.halted.startswith(f"halted before this start, remove {halt_file} to resume: ")
    assert logs[0] == "live trades left exposed before this start, flattening again: 1, 2, 3"
    assert logs[1].startswith("live trading halted before this start") and trade(again) == [False] and venues.orders == []
    halt_file.unlink()                                                  # Checked and cleared by a human.
    conn, cash, resumed = executor(tmp_path, venues)
    assert resumed.halted is None and trade(resumed) == [True]
    assert resumed.halted is None                                       # Its refusal is the first since the halt, not the fourth in a row.


def test_a_restart_takes_back_what_was_left_exposed_and_flattens_it_again(tmp_path):
    latest = books()
    venues = Venues(polymarket_us=[fills(), fills(0)], kalshi=[bids_gone(latest)])
    conn, cash, ex = executor(tmp_path, venues, latest)
    trade(ex)                                                           # Kalshi filled nothing, and the sale back found nothing.
    ex.brakes.halt("the live trades decided in the last 6 hours lost too much")
    logs = []
    venues = Venues(polymarket_us=[fills()])
    conn, cash, again = executor(tmp_path, venues, latest, logs=logs)   # A crash or a deploy restarts the process.
    assert list(again.exposed) == [1] and "live trades left exposed before this start, flattening again: 1" in logs
    assert again.halted and again.brakes.stopped is None                # Still halted on results, so still flattening.
    asyncio.run(again.retry(NOW))
    assert venues.orders == [("polymarket_us", "sell", "yes", 10, 0.44)]
    assert again.exposed == {} and stored(conn, "trades")[0]["yes_held"] == 0


LATE = "2026-09-22T21:30:00+00:00"                                       # 45 minutes past PAYS_AT, the game still under way.


def changed(latest, now):
    """
    Each book in latest as just changed at now, so a game under way trades on it.
    """
    latest.update({key: dataclasses.replace(book, ts=now) for key, book in latest.items()})


def test_a_trade_still_exposed_past_its_payout_time_is_flattened_again(tmp_path):
    venues = Venues(polymarket_us=[fills(), fills(0), fills(0), fills()], kalshi=[fills(4)])
    latest = books()
    conn, cash, ex = executor(tmp_path, venues, latest)
    trade(ex)                                                           # The 6 yes over found no taker, though the book showed one.
    assert list(ex.exposed) == [1]
    # The payout time is kickoff plus the game and the venues settling, an estimate, which a game can outlast with its
    # market trading.
    changed(latest, LATE)
    at(ex, LATE)
    asyncio.run(ex.retry(LATE))
    assert len(venues.orders) == 4 and venues.orders[-1] == ("polymarket_us", "sell", "yes", 6, 0.44)
    assert list(ex.exposed) == [1]                                      # Sold nothing again, so tried a minute on.
    later = "2026-09-22T21:31:00+00:00"                                 # A minute on.
    changed(latest, later)
    at(ex, later)
    asyncio.run(ex.retry(later))
    assert len(venues.orders) == 5 and ex.exposed == {} and stored(conn, "trades")[0]["yes_held"] == 4
    assert stored(conn, "trades")[0]["hedge"].endswith("then sold back 6 of 6 on polymarket_us at 21:31:00")


def test_a_trade_that_never_settles_is_let_go_a_day_past_its_payout_time(tmp_path):
    venues = Venues(polymarket_us=[fills(), fills(0)], kalshi=[fills(4)])
    latest = books()
    conn, cash, ex = executor(tmp_path, venues, latest)
    trade(ex)
    given_up = shift(PAYS_AT, hours=config.FLATTEN_PAST_PAYOUT_HOURS)
    changed(latest, given_up)
    at(ex, given_up)
    asyncio.run(ex.retry(given_up))
    assert len(venues.orders) == 3 and ex.exposed == {}                 # Left to settle as it stands.
    assert executor(tmp_path, Venues(), latest, now=given_up)[2].exposed == {}      # And not taken back by a restart.


def test_a_restart_past_the_payout_time_takes_back_what_is_still_exposed(tmp_path):
    latest = books()
    venues = Venues(polymarket_us=[fills(), fills(0)], kalshi=[bids_gone(latest)])
    conn, cash, ex = executor(tmp_path, venues, latest)
    trade(ex)                                                           # Kalshi filled nothing, and the sale back found nothing.
    changed(latest, LATE)
    logs = []
    venues = Venues(polymarket_us=[fills()])
    conn, cash, again = executor(tmp_path, venues, latest, logs=logs, now=LATE)    # A deploy, with the game running late.
    assert list(again.exposed) == [1] and "live trades left exposed before this start, flattening again: 1" in logs
    asyncio.run(again.retry(LATE))
    assert venues.orders == [("polymarket_us", "sell", "yes", 10, 0.44)]
    assert again.exposed == {} and stored(conn, "trades")[0]["yes_held"] == 0


def test_orders_the_latency_stopgap_turned_away_do_not_count_as_refusals(tmp_path):
    stopgap = orders.Answer(None, "unfilled", 0, 0.0, 0.0, "latency stopgap: Global Rate Limit Exceeded", {})
    venues = Venues(polymarket_us=[stopgap] * 4, kalshi=[fills(0)] * 4)
    conn, cash, ex = executor(tmp_path, venues)
    assert trade(ex, 4) == [True] * 4 and ex.halted is None
    assert "yes leg unfilled: latency stopgap" in stored(conn, "trades")[0]["hedge"]


def test_a_market_that_turned_an_order_away_as_not_trading_is_left_alone_and_is_no_refusal(tmp_path):
    closed = orders.Answer(None, "closed", 0, 0.0, 0.0, "market not trading: MARKET_NOT_ACTIVE", {})
    venues = Venues(polymarket_us=[fills(), fills()] * 3, kalshi=[closed] * 3)     # Each Polymarket US fill is sold back.
    conn, cash, ex = executor(tmp_path, venues)
    left_alone = []
    ex.market_closed = lambda venue, contract_id, now: left_alone.append((venue, contract_id, now))
    assert trade(ex, 3) == [True] * 3 and ex.halted is None             # Three in a row halt nothing, as refusals would.
    assert left_alone == [("kalshi", "k", NOW)] * 3                     # The recorder leaves it alone after the first, see record.py.
    assert "no leg closed: market not trading: MARKET_NOT_ACTIVE" in stored(conn, "trades")[0]["hedge"]
    assert [o["status"] for o in stored(conn, "orders") if o["venue"] == "kalshi"] == ["closed"] * 3


def test_trades_spend_all_the_cash_and_a_venue_or_kalshi_shard_running_low_emails_once(tmp_path):
    shards = {0: 8.0, 2: 20.0, 3: 20.0}                 # Football's shard, Bitcoin's, and baseball's.
    cash = LiveBalances(lambda m: None, {"kalshi": lambda: (sum(shards.values()), dict(shards)), "polymarket_us": lambda: (8.0, {})})
    asyncio.run(cash.refresh(NOW))
    notifier, logs = FakeNotifier(), []
    venues = Venues(polymarket_us=[fills()], kalshi=[fills()])
    conn = database.connect(tmp_path / "t.sqlite")
    ex = LiveExecutor(conn, cash, lambda: books(size=100), logs.append, clock=lambda: NOW, place=venues.place(), notifier=notifier)
    football = {("polymarket_us", "pm"): NO_PM_FEES, ("kalshi", "k"): dict(NO_K_FEES, exchange_index=0)}

    async def scenario():
        sent = ex.signal(PAIR, YES, NO, 1 - 0.45 - 0.47, 100, football, NOW)
        await asyncio.gather(*ex.tasks)
        return sent
    # All 8 dollars on Polymarket US and on Kalshi's shard 0 may be spent: half the 100 shown is 50, but the 8 dollars pay for
    # 17.02 contracts at 0.47, cut to the hundredth live trades in.
    assert cash.spendable("kalshi", 0) == pytest.approx(8.0)
    assert asyncio.run(scenario()) is True and stored(conn, "trades")[0]["quantity"] == 17.02
    ex.tick(NOW)
    ex.tick(NOW)
    # Once each: Kalshi's shard 0, whose 8 dollars less the 7.9994 bought is 0.0006, and Polymarket US, 8 less 7.659. Shards 2
    # and 3 have plenty.
    assert [(kind, subject) for kind, subject, _ in notifier.sent] == [("low_cash", "PredictionArbitrageTrader live kalshi shard 0 cash low: 0.00$"),
                                                                        ("low_cash", "PredictionArbitrageTrader live polymarket_us cash low: 0.34$")]
    assert "move some to the shard with python3 -m tools.kalshi_shards" in notifier.sent[0][2]
    cash.shard_read[("kalshi", 0)] = 20.0                               # A payout arrives.
    ex.tick(NOW)
    assert logs[-1] == "live kalshi shard 0 has 12.00$, back over 5.00$"
    cash.shard_read[("kalshi", 0)] = 4.0
    cash.shard_read[("kalshi", 3)] = 1.0
    ex.tick(NOW)
    assert [subject for _, subject, _ in notifier.sent][-2:] == ["PredictionArbitrageTrader live kalshi shard 0 cash low: -4.00$",     # 4 read, less 7.9994.
                                                                  "PredictionArbitrageTrader live kalshi shard 3 cash low: 1.00$"]


def test_polymarket_us_goes_first_and_kalshi_only_once_it_has_answered(tmp_path):
    answered = threading.Event()

    def polymarket_us(quantity, price):
        answer = fills()(quantity, price)
        answered.set()
        return answer

    def kalshi(quantity, price):
        # Fills only once Polymarket US has answered, which it would not have yet if both went at once.
        return fills()(quantity, price) if answered.is_set() else REFUSED
    venues = Venues(polymarket_us=[polymarket_us], kalshi=[kalshi])
    conn, cash, ex = executor(tmp_path, venues)
    assert trade(ex) == [True]
    t = stored(conn, "trades")[0]
    assert (t["yes_filled"], t["no_filled"], t["status"]) == (10, 10, "filled")


def test_legs_on_one_venue_go_out_at_once(tmp_path):
    other = dict(NO, venue="polymarket_us", contract_id="pm2")         # No through the other side of a second Polymarket US market.
    latest = {("polymarket_us", "pm"): books()[("polymarket_us", "pm")],
              ("polymarket_us", "pm2"): Book("polymarket_us", "pm2", NOW, [[0.53, 20]], [[0.54, 20]])}
    started = threading.Barrier(2, timeout=5)       # Each order waits for the other: they are both out at once.

    def together(quantity, price):
        started.wait()
        return fills()(quantity, price)
    venues = Venues(polymarket_us=[together, together])
    conn, cash, ex = executor(tmp_path, venues, latest)
    fees = {("polymarket_us", "pm"): NO_PM_FEES, ("polymarket_us", "pm2"): NO_PM_FEES}

    async def scenario():
        sent = ex.signal(PAIR, YES, other, 0.08, 100, fees, NOW)
        await asyncio.gather(*ex.tasks)
        return sent
    assert asyncio.run(scenario())
    assert sorted(venues.orders) == [("polymarket_us", "buy", "no", 10, 0.47), ("polymarket_us", "buy", "yes", 10, 0.45)]
    assert stored(conn, "trades")[0]["status"] == "filled"


def test_a_kalshi_leg_trades_only_with_the_cash_on_its_markets_shard(tmp_path):
    shards = {0: 1000.0, 3: 0.0}                        # Kalshi's cash all on football's shard, none on baseball's.
    cash = LiveBalances(lambda m: None, {"kalshi": lambda: (sum(shards.values()), dict(shards)), "polymarket_us": lambda: (1000.0, {})})
    asyncio.run(cash.refresh(NOW))
    venues = Venues(polymarket_us=[fills()], kalshi=[fills()])
    conn = database.connect(tmp_path / "t.sqlite")
    ex = LiveExecutor(conn, cash, lambda: books(), lambda m: None, clock=lambda: NOW, place=venues.place())
    baseball = {("polymarket_us", "pm"): NO_PM_FEES, ("kalshi", "k"): dict(NO_K_FEES, exchange_index=3)}

    def signal():
        async def scenario():
            sent = ex.signal(dict(PAIR, sport="mlb"), YES, NO, 1 - 0.45 - 0.47, 100, baseball, NOW)
            await asyncio.gather(*ex.tasks)
            return sent
        return asyncio.run(scenario())

    assert signal() is False and venues.orders == []
    shards[3] = 3 * 0.47 + 0.01                     # Room for 3.02 contracts on baseball's shard, to the hundredth.
    asyncio.run(cash.refresh(NOW))
    assert signal() is True
    assert sorted(venues.orders) == [("kalshi", "buy", "no", 3.02, 0.47), ("polymarket_us", "buy", "yes", 3.02, 0.45)]  # Sent together.
    assert cash.available("kalshi", 3) == pytest.approx(1.42 - 3.02 * 0.47) and cash.available("kalshi", 0) == 1000.0


def test_a_trade_the_cash_sizes_leaves_room_for_the_fees(tmp_path):
    shards = {0: 1000.0, 3: 10.0}                       # Ten dollars on baseball's shard, where the books show 50 to take.
    cash = LiveBalances(lambda m: None, {"kalshi": lambda: (sum(shards.values()), dict(shards)), "polymarket_us": lambda: (1000.0, {})})
    asyncio.run(cash.refresh(NOW))
    venues = Venues(polymarket_us=[fills()], kalshi=[fills()])
    conn = database.connect(tmp_path / "t.sqlite")
    ex = LiveExecutor(conn, cash, lambda: books(size=100), lambda m: None, clock=lambda: NOW, place=venues.place())
    charged = {("polymarket_us", "pm"): {"feeCoefficient": 0.0695},
               ("kalshi", "k"): {"fee_type": "quadratic", "fee_multiplier": 1, "exchange_index": 3}}

    async def scenario():
        sent = ex.signal(dict(PAIR, sport="mlb"), YES, NO, 1 - 0.45 - 0.47, 100, charged, NOW)
        await asyncio.gather(*ex.tasks)
        return sent
    assert asyncio.run(scenario()) is True
    # 21.27 contracts at 0.47 cost 9.9969$, but Kalshi's fee on them, 0.3709$, takes the order past the 10 dollars, and
    # Kalshi turns it away. 20.51 cost 9.6397$ and a 0.3577$ fee, 9.9974$, and 20.52 would cost 10.0023$.
    assert sorted(venues.orders) == [("kalshi", "buy", "no", 20.51, 0.47), ("polymarket_us", "buy", "yes", 20.51, 0.45)]
    # What was held back for the fee is given back once the order answers, its fill charging none here.
    assert cash.available("kalshi", 3) == pytest.approx(10.0 - 20.51 * 0.47)


def test_a_leg_filled_in_hundredths_is_flattened_to_the_hundredth(tmp_path):
    venues = Venues(polymarket_us=[fills(), fills()], kalshi=[fills(6.42)])
    conn, cash, ex = executor(tmp_path, venues)
    trade(ex)
    t = stored(conn, "trades")[0]
    # Kalshi filled 6.42 of the 10, so the 3.58 yes over on Polymarket US are sold back, at its 0.44 bid, to the hundredth.
    assert venues.orders[-1] == ("polymarket_us", "sell", "yes", 3.58, 0.44)
    assert (t["yes_filled"], t["no_filled"], t["matched"], t["yes_held"], t["no_held"], t["status"]) == (10, 6.42, 6.42, 6.42, 6.42, "partial")
    assert t["profit"] == pytest.approx(6.42 * (1 - 0.45 - 0.47)) and t["hedge"] == "sold back 3.58 of 3.58 on polymarket_us"
    assert t["hedge_pnl"] == pytest.approx(3.58 * (0.44 - 0.45))
    assert ex.exposed == {}


def test_kalshi_is_sent_what_polymarket_us_filled_to_the_hundredth(tmp_path):
    venues = Venues(polymarket_us=[fills(6.42)], kalshi=[fills()])
    conn, cash, ex = executor(tmp_path, venues)
    trade(ex)
    t = stored(conn, "trades")[0]
    assert venues.orders == [("polymarket_us", "buy", "yes", 10, 0.45), ("kalshi", "buy", "no", 6.42, 0.47)]
    assert (t["yes_filled"], t["no_filled"], t["matched"], t["status"], t["hedge"]) == (6.42, 6.42, 6.42, "partial", "none")
    assert ex.exposed == {}


def test_a_sale_that_sold_nothing_is_tried_again_only_a_minute_later(tmp_path):
    venues = Venues(polymarket_us=[fills(), fills(0), fills(0), fills()], kalshi=[fills(4)])
    conn, cash, ex = executor(tmp_path, venues)
    trade(ex)                                                           # The 6 yes over found no taker, though the book showed one.
    assert len(venues.orders) == 3 and list(ex.exposed) == [1]
    for second in range(1, 60):
        asyncio.run(ex.retry(f"2026-09-20T17:30:{second:02d}+00:00"))    # Sold nothing, so not tried again within the minute.
    assert len(venues.orders) == 3
    at(ex, "2026-09-20T17:31:00+00:00")
    asyncio.run(ex.retry(ex.clock()))                                   # Sold nothing again, so a minute more.
    asyncio.run(ex.retry("2026-09-20T17:31:30+00:00"))
    assert len(venues.orders) == 4
    at(ex, "2026-09-20T17:32:00+00:00")
    asyncio.run(ex.retry(ex.clock()))
    assert len(venues.orders) == 5 and ex.exposed == {} and stored(conn, "trades")[0]["yes_held"] == 4


def test_a_retry_checks_the_results_only_when_it_sold_something(tmp_path, monkeypatch):
    venues = Venues(polymarket_us=[fills(), fills(0), fills()], kalshi=[fills(4)])
    conn, cash, ex = executor(tmp_path, venues)
    trade(ex)                                                           # The 6 yes over found no taker.
    checks = []
    monkeypatch.setattr(database, "load_trade_cash", lambda *args: checks.append(args) or [])
    for second in range(1, 60):
        assert asyncio.run(ex.retry(f"2026-09-20T17:30:{second:02d}+00:00")) is False
    # The retry runs every tick while a trade is exposed, and a check each time would hold up the orders queued behind it.
    assert checks == []
    at(ex, "2026-09-20T17:31:00+00:00")
    assert asyncio.run(ex.retry(ex.clock())) is True and len(checks) == 1 and ex.exposed == {}


def test_a_fraction_sold_back_on_a_later_try_is_recorded(tmp_path):
    venues = Venues(polymarket_us=[fills(), fills(0), fills()], kalshi=[fills(9.58)])
    logs = []
    conn, cash, ex = executor(tmp_path, venues, logs=logs)
    trade(ex)                                                           # The 0.42 yes over found no taker at first.
    assert list(ex.exposed) == [1] and stored(conn, "trades")[0]["yes_held"] == 10
    at(ex, "2026-09-20T17:31:00+00:00")                                  # A minute on, when a sale that sold nothing is tried again.
    asyncio.run(ex.retry(ex.clock()))
    # A sale of 0.42 is not read as one that sold nothing, "sold back 0...".
    t = stored(conn, "trades")[0]
    assert (t["yes_held"], t["no_held"]) == (9.58, 9.58) and ex.exposed == {}
    assert t["hedge"].endswith("then sold back 0.42 of 0.42 on polymarket_us at 17:31:00")
    assert logs[-1].startswith("live flattened nfl game_winner 2026-09-22 CAR@ATL CAR: sold back 0.42 of 0.42 on polymarket_us, 0 still exposed")


UNFUNDED = orders.Answer(None, "unfunded", 0, 0.0, 0.0, "not enough funds: You don't have enough funds for this order.", {})


def test_a_sale_turned_away_for_lack_of_cash_waits_until_the_cash_grows_and_halts_nothing(tmp_path):
    venues = Venues(polymarket_us=[fills(), UNFUNDED, UNFUNDED, UNFUNDED, fills()], kalshi=[fills(4)])
    logs = []
    conn, cash, ex = executor(tmp_path, venues, logs=logs)
    trade(ex)                                                           # Kalshi filled 4 of 10, and the 6 yes over could not be sold.
    assert venues.orders[-1] == ("polymarket_us", "sell", "yes", 6, 0.44) and list(ex.exposed) == [1]
    assert stored(conn, "trades")[0]["hedge"] == "sold back 0 of 6 on polymarket_us, which lacked the cash, 6 exposed"
    assert logs[-2] == "live nfl game_winner 2026-09-22 CAR@ATL CAR: polymarket_us lacked the cash to sell back 6, which waits until its cash grows"
    for _ in range(5):
        asyncio.run(ex.retry(NOW))                                      # No more cash, so no order every tick.
    assert len(venues.orders) == 3
    for _ in range(3):
        cash.read["polymarket_us"] += 1.0                               # A payout, say: tried again, once.
        asyncio.run(ex.retry(NOW))
        asyncio.run(ex.retry(NOW))
    # Turned away twice more, then sold. Three turned away in a row are no refusals, so live trading goes on.
    assert len(venues.orders) == 6 and ex.exposed == {} and stored(conn, "trades")[0]["yes_held"] == 4
    assert ex.halted is None


def check(ex):
    asyncio.run(ex.check_positions())


def test_a_venue_holding_other_than_the_trades_is_logged_once(tmp_path):
    venues = Venues(polymarket_us=[fills()], kalshi=[fills()])
    held, logs = {"pm": 10.0}, []           # The trade holds 10 yes of pm, as the venue does.
    conn, cash, ex = executor(tmp_path, venues, logs=logs, positions={"polymarket_us": lambda: held, "kalshi": lambda: {"k": -10.0}})
    trade(ex)
    check(ex)
    assert not any("differ" in line for line in logs)
    held.update(pm=10.42, other=-1.0)
    check(ex)
    check(ex)
    assert [line for line in logs if "differ" in line] == [
        "live polymarket_us holds -1 of other, the trades 0: the records differ from the venue, see tools/repair_fills.py",
        "live polymarket_us holds 10.42 of pm, the trades 10: the records differ from the venue, see tools/repair_fills.py"]
    assert len(venues.orders) == 2                                      # Only logged, nothing sent.


def test_each_order_stores_the_times_of_the_book_it_went_out_on(tmp_path):
    venues = Venues(polymarket_us=[fills()], kalshi=[fills()])
    latest = books()
    pm, k = latest[("polymarket_us", "pm")], latest[("kalshi", "k")]
    venue_time = epoch("2026-09-20T17:29:57.900000+00:00")             # By Polymarket US's clock, 0.1s before the book reached us.
    latest[("polymarket_us", "pm")] = Book(pm.venue, pm.contract_id, "2026-09-20T17:29:58+00:00", pm.bids, pm.asks, venue_time)
    latest[("kalshi", "k")] = Book(k.venue, k.contract_id, "2026-09-20T17:29:50+00:00", k.bids, k.asks)     # No venue time given.
    conn, cash, ex = executor(tmp_path, venues, latest)
    assert trade(ex) == [True]
    assert {o["venue"]: (o["book_at"], o["book_ts"]) for o in stored(conn, "orders")} == {
        "polymarket_us": ("2026-09-20T17:29:57.900000+00:00", "2026-09-20T17:29:58+00:00"),
        "kalshi": (None, "2026-09-20T17:29:50+00:00")}


def test_positions_are_not_compared_while_orders_go_out(tmp_path):
    venues = Venues(polymarket_us=[fills()], kalshi=[fills()])
    logs = []
    conn, cash, ex = executor(tmp_path, venues, logs=logs)
    trade(ex)

    def read_while_a_trade_goes_out():                                  # In a thread of its own, so on a connection of its own.
        database.insert_order(database.connect(tmp_path / "t.sqlite"), Order(
            trade_id=1, venue="kalshi", contract_id="k", purpose="open", action="buy", outcome="no", quantity=1, limit_price=0.47,
            client_id="c", sent_at=NOW))
        return {"pm": 12.0}
    ex.positions = {"polymarket_us": read_while_a_trade_goes_out}
    check(ex)
    ex.positions = {"polymarket_us": lambda: {"pm": 12.0}}
    ex.tasks.add(asyncio.Future)                                        # A trade in flight.
    check(ex)
    assert not any("differ" in line for line in logs)


# IN PLAY, live trading games under way with run.py --live-in-play

UNDER_WAY = "2026-09-22T18:00:00+00:00"     # An hour into the game, which pays within the day.
GAME = {**PAIR, "members": [YES, NO]}       # The pair as the scanner offers it, with every member.
FUTURE = {**PAIR, "game_date": None, "kind": "champion", "label": "nfl champion 2027 CAR"}      # A future, which pays in a month.
SURE = ({**YES, "start_time": None, "close_time": "2026-10-20T00:00:00+00:00"},
        {**NO, "start_time": None, "close_time": "2026-10-20T00:00:00+00:00"})                # Its members.
SOON = tuple({**m, "close_time": "2026-09-27T00:00:00+00:00"} for m in SURE)                # Those of one paying in four days.


def in_play(tmp_path, venues, latest, logs=None):
    """
    A live executor trading games under way, an hour into the game, with books of then.
    """
    for key, book in latest.items():
        latest[key] = Book(book.venue, book.contract_id, UNDER_WAY, book.bids, book.asks)
    conn, _, ex = executor(tmp_path, venues, latest, logs=logs, now=UNDER_WAY, in_play=True)
    return conn, ex


def signal(ex, pair=GAME, members=(YES, NO), now=UNDER_WAY, edge=0.08):
    async def scenario():
        sent = ex.signal(pair, *members, edge, 100, FEES, now)
        while ex.tasks:
            await asyncio.gather(*ex.tasks)
        return sent
    return asyncio.run(scenario())


@pytest.mark.full_share
def test_live_trades_a_game_under_way_at_most_twenty_contracts_polymarket_us_first(tmp_path):
    venues = Venues(polymarket_us=[fills()], kalshi=[fills()])
    latest = books(size=200)
    latest[("polymarket_us", "pm")] = Book("polymarket_us", "pm", NOW, [[0.44, 20]], [[0.45, 3], [0.46, 120], [0.48, 120]])
    conn, ex = in_play(tmp_path, venues, latest)
    assert signal(ex)
    t = stored(conn)[0]
    # Twenty contracts need the second level, so the limit goes no deeper, though the third keeps two cents too.
    assert (t["quantity"], t["yes_limit"], t["no_limit"], t["status"], t["in_play"]) == (20, 0.46, 0.47, "filled", 1)
    # Polymarket US's order goes first, and Kalshi's once it has answered, for what it filled, its limit raised to give up
    # half the edge left: 0.46 paid and Kalshi's 0.47 leave seven cents, so Kalshi may pay 0.505, 0.50 to the cent.
    # The trade keeps the signal's limits.
    assert venues.orders == [("polymarket_us", "buy", "yes", 20, 0.46), ("kalshi", "buy", "no", 20, 0.50)]


def test_in_play_a_game_not_yet_under_way_and_one_paying_too_late_are_not_traded(tmp_path):
    venues = Venues()
    conn, ex = in_play(tmp_path, venues, books())
    assert not signal(ex, now="2026-09-22T16:00:00+00:00")             # An hour before kickoff.
    later = {**GAME, "game_date": "2026-09-22", "members": [{**YES, "start_time": "2026-09-20T12:00:00+00:00"}, NO]}
    assert not signal(ex, later, now="2026-09-20T18:00:00+00:00")       # Under way by its first member, but paying two days out.
    assert venues.orders == [] and stored(conn) == []


def test_without_in_play_live_trades_no_game_under_way(tmp_path):
    venues = Venues()
    conn, ex = in_play(tmp_path, venues, books())
    ex.in_play = False
    assert not signal(ex) and venues.orders == []


def test_in_play_a_future_is_traded_as_ever_polymarket_us_first(tmp_path):
    venues = Venues(polymarket_us=[fills()], kalshi=[fills()])
    conn, ex = in_play(tmp_path, venues, books())
    assert signal(ex, FUTURE, SURE)
    assert stored(conn)[0]["quantity"] == 10                            # Half the 20 the books show.
    # Kalshi's limit is not raised: a future gives up none of its edge, see LiveExecutor.allowance().
    assert venues.orders == [("polymarket_us", "buy", "yes", 10, 0.45), ("kalshi", "buy", "no", 10, 0.47)]


def test_a_futures_trade_that_matched_gives_its_episode_back_once_done_and_no_other_does(tmp_path):
    venues = Venues(polymarket_us=[fills(), fills(), fills(0), fills(), fills(0)], kalshi=[fills(), fills(), fills(3)])
    conn, ex = in_play(tmp_path, venues, books())
    given = []
    ex.offer_again = lambda pair_id, signal_ts, now: given.append((pair_id, signal_ts, now))

    async def first():
        assert ex.signal(FUTURE, *SURE, 0.08, 100, FEES, UNDER_WAY) and given == []      # Not while its orders are out.
        while ex.tasks:
            await asyncio.gather(*ex.tasks)
    asyncio.run(first())
    assert given == [(FUTURE["id"], UNDER_WAY, UNDER_WAY)]
    assert signal(ex, {**GAME, "id": 2})                                # A game's trade matched, but a game's episode makes one trade.
    assert signal(ex, {**FUTURE, "id": 3}, SURE)                        # Polymarket US filled nothing, its quote likely gone.
    assert signal(ex, {**FUTURE, "id": 4}, SURE)                        # Matched 3, but 7 are left to flatten.
    assert [t["status"] for t in stored(conn)] == ["filled", "filled", "failed", "partial"] and list(ex.exposed) == [4]
    assert given == [(FUTURE["id"], UNDER_WAY, UNDER_WAY)]


@pytest.mark.full_share
def test_polymarket_us_first_sends_kalshi_only_what_it_filled_and_nothing_when_it_missed(tmp_path):
    venues = Venues(polymarket_us=[fills(3), fills(0)], kalshi=[fills()])
    conn, ex = in_play(tmp_path, venues, books(size=100))
    assert signal(ex)
    t = stored(conn)[0]
    # Kalshi's limit gives up half the eight cents left past 0.45 paid, see test_in_play_kalshi_may_give_up_half_the_edge_left.
    assert venues.orders == [("polymarket_us", "buy", "yes", 20, 0.45), ("kalshi", "buy", "no", 3, 0.51)]
    assert (t["quantity"], t["yes_filled"], t["no_filled"], t["matched"], t["status"]) == (20, 3, 3, 3, "partial")
    assert signal(ex, {**GAME, "id": 2})
    t = stored(conn)[1]
    # Polymarket US filled nothing, so Kalshi was never sent and nothing is held: there is nothing to sell back.
    assert venues.orders[2:] == [("polymarket_us", "buy", "yes", 20, 0.45)]
    assert (t["yes_filled"], t["no_filled"], t["yes_held"], t["no_held"], t["status"]) == (0, 0, 0, 0, "failed")
    assert t["hedge"] == "no leg not sent, as polymarket_us filled nothing first"
    assert ex.cash.reserved == {} or not any(ex.cash.reserved.values())


CHARGED = {("polymarket_us", "pm"): NO_PM_FEES, ("kalshi", "k"): {"fee_type": "quadratic", "fee_multiplier": 1}}   # Kalshi's fee.


def test_in_play_kalshi_may_give_up_half_the_edge_left_fees_included(tmp_path):
    def dear(quantity, price):          # Filled at 0.53, so nothing is left of the edge past Kalshi's 0.47.
        return orders.Answer("venue-dear", "filled", quantity, quantity * 0.53, 0.0, None, {})
    venues = Venues(polymarket_us=[fills(), dear], kalshi=[fills(), fills()])
    conn, ex = in_play(tmp_path, venues, books())

    def send(pair):
        async def scenario():
            sent = ex.signal(pair, YES, NO, 0.06, 100, CHARGED, UNDER_WAY)
            await asyncio.gather(*ex.tasks)
            return sent
        return asyncio.run(scenario())
    assert send(GAME)
    # Polymarket US's 10 cost 0.45 each, and Kalshi's no at 0.47 costs 0.4874 with its 0.0174 fee, which leaves 6.26 cents.
    # Kalshi may give up half of them, paying up to 0.5187 a contract, fee included: 0.51 would cost 0.5275 with its fee, so
    # its limit is 0.50, which costs 0.5175.
    assert venues.orders == [("polymarket_us", "buy", "yes", 10, 0.45), ("kalshi", "buy", "no", 10, 0.50)]
    t = stored(conn)[0]
    assert (t["yes_limit"], t["no_limit"], t["status"]) == (0.45, 0.47, "filled")    # The trade keeps the signal's limits.
    assert t["profit"] == pytest.approx(10 * (1 - 0.45 - 0.50))
    assert send({**GAME, "id": 2})
    assert venues.orders[-1] == ("kalshi", "buy", "no", 10, 0.47)       # With no edge left Kalshi's limit stays.


def test_in_play_kalshi_gives_up_no_more_than_the_cash_pays_for_held_back_while_its_order_is_out(tmp_path):
    free = []                           # Kalshi's free cash as its order went out.

    def kalshi(quantity, price):
        free.append(cash.available("kalshi"))
        return fills()(quantity, price)
    venues = Venues(polymarket_us=[fills()], kalshi=[kalshi])
    latest = {key: Book(b.venue, b.contract_id, UNDER_WAY, b.bids, b.asks) for key, b in books().items()}
    conn, cash, ex = executor(tmp_path, venues, latest, balance=4.90, now=UNDER_WAY, in_play=True)
    assert signal(ex)
    # 10 contracts at 0.47 hold back 4.70$ of Kalshi's 4.90$. Half the eight cents left would take the limit to 0.51, 5.10$
    # in all, but the 0.20$ still free pays for 0.49 alone. That is held back while the order is out, and given back after.
    assert venues.orders == [("polymarket_us", "buy", "yes", 10, 0.45), ("kalshi", "buy", "no", 10, 0.49)]
    assert free == [pytest.approx(0.0)]
    assert cash.available("kalshi") == pytest.approx(4.90 - 10 * 0.49) and not any(cash.reserved.values())


# FUTURES, live trading a future by its return a year alone

@pytest.mark.full_share
def test_live_trades_a_future_by_its_return_a_year_and_sweeps_only_the_levels_that_return_it(tmp_path):
    venues = Venues(polymarket_us=[fills(), fills()], kalshi=[fills(), fills()])
    latest = books(size=100)
    conn, ex = in_play(tmp_path, venues, latest)
    # Paying in four days a cent returns 87% a year, under 100, so it is not traded, and a cent and a half 131%, so it is,
    # under the two cents paper needs.
    latest[("polymarket_us", "pm")] = Book("polymarket_us", "pm", UNDER_WAY, [[0.51, 20]], [[0.52, 20]])
    assert not signal(ex, FUTURE, SOON, edge=0.01)
    latest[("polymarket_us", "pm")] = Book("polymarket_us", "pm", UNDER_WAY, [[0.505, 20]], [[0.515, 20]])
    assert signal(ex, FUTURE, SOON, edge=0.015)
    # Paying in 27 days 8 cents returns 117% a year and 3 cents 41%, under 100, so only the first level is swept.
    latest[("polymarket_us", "pm")] = Book("polymarket_us", "pm", UNDER_WAY, [[0.44, 20]], [[0.45, 20], [0.50, 20]])
    assert signal(ex, FUTURE, SURE)
    assert [(t["quantity"], t["yes_limit"], t["in_play"]) for t in stored(conn)] == [(20, 0.515, 0), (20, 0.45, 0)]


# IN PLAY RULES, live trading a game under way at two cents once it has lasted a tenth of a second, 20 contracts a trade

def test_in_play_live_takes_an_edge_of_two_cents_or_more(tmp_path):
    venues = Venues(polymarket_us=[fills()], kalshi=[fills()])
    latest = books(pm_ask=0.52)                                         # Yes at 0.52 and no at 0.47, a cent.
    conn, ex = in_play(tmp_path, venues, latest)
    assert not signal(ex, edge=0.01) and venues.orders == []
    latest[("polymarket_us", "pm")] = Book("polymarket_us", "pm", UNDER_WAY, [[0.50, 20]], [[0.51, 20]])    # Two cents.
    assert signal(ex, edge=0.02)
    # Kalshi's order may give up a cent of the two left, see test_in_play_kalshi_may_give_up_half_the_edge_left.
    assert venues.orders == [("polymarket_us", "buy", "yes", 10, 0.51), ("kalshi", "buy", "no", 10, 0.48)]


def test_in_play_live_asks_no_return_a_year_of_a_game_under_way(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MIN_ANNUAL_PCT", 10 ** 6)     # More than any edge returns, even in a day.
    venues = Venues(polymarket_us=[fills()], kalshi=[fills()])
    conn, ex = in_play(tmp_path, venues, books())
    assert not signal(ex, FUTURE, SURE)                                 # A future must still return it.
    assert signal(ex) and [t["in_play"] for t in stored(conn)] == [1]


def test_in_play_live_takes_as_many_trades_on_games_under_way_as_come(tmp_path):
    venues = Venues(polymarket_us=[fills()] * 3, kalshi=[fills()] * 3)
    conn, ex = in_play(tmp_path, venues, books())
    assert signal(ex) and signal(ex) and signal(ex, FUTURE, SURE)
    assert [t["in_play"] for t in stored(conn)] == [1, 1, 0]


def test_live_trades_an_edge_only_once_it_has_lasted_and_asks_to_be_offered_it_again_then(tmp_path):
    venues = Venues(polymarket_us=[fills()] * 2, kalshi=[fills()] * 2)
    conn, ex = in_play(tmp_path, venues, books())
    since, asked = {}, []
    ex.edge_since = since.get
    ex.recheck = lambda pair_id, seconds: asked.append((pair_id, round(seconds, 3)))
    since[GAME["id"]] = "2026-09-22T17:59:59.960000+00:00"             # At 2c or more for four hundredths of a second.
    assert not signal(ex) and venues.orders == [] and stored(conn) == []
    assert asked == [(GAME["id"], 0.06)]                               # Offered again once it has lasted a tenth of a second.
    assert "; 1 pairs' edges held until they lasted" in ex.summary()
    since[GAME["id"]] = "2026-09-22T17:59:59.900000+00:00"             # A tenth of a second.
    assert signal(ex) and len(venues.orders) == 2 and asked == [(GAME["id"], 0.06)]
    future = {**FUTURE, "id": 2}
    since[future["id"]] = UNDER_WAY                                     # A future's edge, just begun, is held too.
    assert not signal(ex, future, SURE) and len(venues.orders) == 2 and asked[-1] == (future["id"], 0.1)
    since[future["id"]] = "2026-09-22T17:59:59.900000+00:00"
    assert signal(ex, future, SURE) and len(venues.orders) == 4


def test_in_play_live_trades_a_lasting_edge_whichever_book_changed_last(tmp_path):
    venues = Venues(polymarket_us=[fills()], kalshi=[fills()])
    latest = books()
    conn, ex = in_play(tmp_path, venues, latest)
    ex.edge_since = lambda pair_id: "2026-09-22T17:59:59+00:00"         # A second at 2c or more.
    pm, k = latest[("polymarket_us", "pm")], latest[("kalshi", "k")]
    latest[("polymarket_us", "pm")] = Book(pm.venue, pm.contract_id, "2026-09-22T17:59:50+00:00", pm.bids, pm.asks)
    latest[("kalshi", "k")] = Book(k.venue, k.contract_id, "2026-09-22T17:59:59.600000+00:00", k.bids, k.asks)
    # Kalshi's change came last, 0.4s ago, longer than a Polymarket US book takes to catch up, see Executor.confirm_wait().
    assert signal(ex)


def test_live_waits_for_polymarket_us_to_catch_up_only_with_the_kalshi_change_from_before_the_edge_began(tmp_path):
    venues = Venues(polymarket_us=[fills()] * 2, kalshi=[fills()] * 2)
    latest = books()
    conn, ex = in_play(tmp_path, venues, latest)
    asked, opened = [], {}
    ex.edge_since = lambda pair_id: "2026-09-22T17:59:59+00:00"         # A second at 2c or more, so no longer held.
    ex.edge_books = lambda pair_id: dict(opened)
    ex.recheck = lambda pair_id, seconds: asked.append((pair_id, round(seconds, 3)))
    pm, k = latest[("polymarket_us", "pm")], latest[("kalshi", "k")]
    latest[("polymarket_us", "pm")] = Book(pm.venue, pm.contract_id, "2026-09-22T17:59:50+00:00", pm.bids, pm.asks)
    latest[("kalshi", "k")] = Book(k.venue, k.contract_id, "2026-09-22T17:59:59.900000+00:00", k.bids, k.asks)
    # Kalshi's change that opened the edge came a tenth of a second ago, after Polymarket US's book, so its leg waits out
    # the rest of 0.3s.
    opened.update({("kalshi", "k"): epoch("2026-09-22T17:59:59.900000+00:00"),
                   ("polymarket_us", "pm"): epoch("2026-09-22T17:59:50+00:00")})
    assert not signal(ex) and venues.orders == [] and asked == [(GAME["id"], 0.2)]
    assert "; 1 pairs' edges waited for a book to catch up" in ex.summary()
    # A Polymarket US book newer than that change, still showing the price, is current, and the edge is taken.
    latest[("polymarket_us", "pm")] = Book(pm.venue, pm.contract_id, "2026-09-22T17:59:59.950000+00:00", pm.bids, pm.asks)
    assert signal(ex) and len(venues.orders) == 2
    # The edge began a second ago, with Kalshi's change of then. Its change a tenth of a second ago left the edge where live
    # takes it, so it does not start the wait again.
    latest[("polymarket_us", "pm")] = Book(pm.venue, pm.contract_id, "2026-09-22T17:59:50+00:00", pm.bids, pm.asks)
    opened[("kalshi", "k")] = epoch("2026-09-22T17:59:59+00:00")
    assert signal(ex, {**GAME, "id": 2}) and len(venues.orders) == 4 and asked == [(GAME["id"], 0.2)]


def test_in_play_live_trades_an_edge_at_once_when_no_scanner_says_when_it_began(tmp_path):
    venues = Venues(polymarket_us=[fills()] * 2, kalshi=[fills()] * 2)
    conn, ex = in_play(tmp_path, venues, books())
    assert ex.edge_since is None and signal(ex)
    ex.edge_since = lambda pair_id: None
    assert signal(ex, {**GAME, "id": 2})
