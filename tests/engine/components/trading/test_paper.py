"""
Tests for the paper executor, with fixed latency and no randomness unless a test asks for it.
"""

import asyncio
import dataclasses
import random
import types
import pytest
from common.timeutil import at_seconds, epoch
from common.venues import VENUES
from db import database
from db.models import Book, Leg
from engine.components.market import scan
from engine.components.market.tape import Tapes
from engine.components.money import settle
from engine.components.money.paper import PaperBalances
from engine.components.trading.executor import Executor
from engine.components.trading.footprints import Footprints
from engine.components.trading.paper import PaperExecutor
from engine.helper import config
from trade_setup import CLOSE, FEES, KICKOFF, NO, NO_K_FEES, NO_PM_FEES, NOW, PAIR, PAYS_AT, YES, books, stored


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(config, "PAPER_ORDER_MS", {venue: {"open": (1, 1), "flatten": (1, 1), "back": (1, 1)} for venue in VENUES})
    monkeypatch.setattr(config, "MAX_PAYOUT_HOURS", 24 * 7)     # The pair pays two days after NOW.
    monkeypatch.setattr(config, "PAPER_REJECT_PROBABILITY", 0)


def executor(tmp_path, latest, log=lambda m: None, start=config.PAPER_START_BALANCE, clock=lambda: NOW):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = PaperBalances(conn, start)
    return conn, cash, PaperExecutor(conn, cash, lambda: latest, log, random.Random(1), clock=clock)


def run(ex, after_signal=None, signals=1):
    """
    Send the signal inside a loop, optionally change the books before the orders arrive, and wait for the trades.
    """
    async def scenario():
        sent = [ex.signal(PAIR, YES, NO, 1 - 0.45 - 0.47, 100, FEES, NOW) for _ in range(signals)]
        if after_signal:
            after_signal()
        if ex.tasks:
            await asyncio.gather(*ex.tasks)
        return sent
    return asyncio.run(scenario())


def test_unchanged_books_fill_both_legs_and_lock_in_the_edge(tmp_path, quick):
    latest = books()
    logs = []
    conn, cash, ex = executor(tmp_path, latest, logs.append)
    assert run(ex) == [True]
    t = stored(conn)[0]
    assert (t["mode"], t["status"], t["quantity"], t["yes_filled"], t["no_filled"], t["matched"]) == ("paper", "filled", 50, 50, 50, 50)   # Half the visible 100.
    assert (t["yes_limit"], t["no_limit"], t["yes_polarity"], t["no_polarity"]) == (0.45, 0.47, "yes", "yes")
    assert t["profit"] == pytest.approx(50 * (1 - 0.45 - 0.47))
    assert (t["hedge"], t["hedge_pnl"], t["yes_held"], t["no_held"]) == ("none", 0, 50, 50)
    assert t["pays_at"] == PAYS_AT
    assert cash.amounts == pytest.approx({"polymarket_us": 10000 - 50 * 0.45, "kalshi": 10000 - 50 * 0.47})
    assert [tuple(r) for r in conn.execute("SELECT venue, amount, reason, trade_id FROM ledger ORDER BY id")][2:] == [
        ("polymarket_us", pytest.approx(-22.5), "buy", 1), ("kalshi", pytest.approx(-23.5), "buy", 1)]      # After the two openings.
    assert logs[0].startswith("paper filled: nfl game_winner 2026-09-22 CAR@ATL CAR")
    assert ex.summary().startswith("paper: 1 trades (1 filled, 0 partial, 0 failed), locked in 4.00$, hedges +0.00$; total 1 trades, 4.00$")


def test_an_order_sweeps_the_levels_that_keep_the_edge_floor_and_skips_a_one_lot_top(tmp_path, quick):
    latest = books()
    latest[("polymarket_us", "pm")] = Book("polymarket_us", "pm", NOW, [[0.44, 100]], [[0.45, 1], [0.46, 100], [0.52, 100]])
    latest[("kalshi", "k")] = Book("kalshi", "k", NOW, [[0.53, 300]], [[0.54, 300]])
    conn, cash, ex = executor(tmp_path, latest)
    assert run(ex) == [True]
    t = stored(conn)[0]
    # The 1-lot at 0.45 fills nothing for us. The 100 at 0.46 still leave 7 cents against 0.47, so the limit reaches it.
    # The 0.52 level would leave 1 cent, under the 2 cent floor, and is left out though Kalshi has more. Half of the
    # 100 contracts inside the limit is the quantity.
    assert (t["yes_limit"], t["no_limit"], t["quantity"]) == (0.46, 0.47, 50)
    assert (t["status"], t["yes_filled"], t["no_filled"], t["matched"]) == ("filled", 50, 50, 50)
    assert t["yes_cost"] == pytest.approx(50 * 0.46) and t["profit"] == pytest.approx(50 * (1 - 0.46 - 0.47))


def test_a_shrunken_leg_is_evened_by_selling_the_other_back_though_buying_the_rest_would_cost_less(tmp_path, quick):
    latest = books()
    conn, cash, ex = executor(tmp_path, latest)

    def kalshi_thins_out():
        latest[("kalshi", "k")] = Book("kalshi", "k", NOW, [[0.53, 40]], [[0.54, 100]])

    run(ex, kalshi_thins_out)
    t = stored(conn)[0]
    assert (t["yes_filled"], t["no_filled"]) == (50, 20)
    # Buying 30 more no on Kalshi at 0.47 would still earn 8 cents each, but it would hold the money until the game pays. Selling
    # the 30 yes back at the 0.44 bid loses a cent each and frees the money at once.
    assert t["hedge"] == "sold back 30 of 30 on polymarket_us"
    assert t["hedge_pnl"] == pytest.approx(30 * (0.44 - 0.45))
    assert (t["matched"], t["status"], t["yes_held"], t["no_held"]) == (20, "partial", 20, 20)
    assert cash["polymarket_us"] == pytest.approx(10000 - 50 * 0.45 + 30 * 0.44)
    assert cash["kalshi"] == pytest.approx(10000 - 20 * 0.47)


def test_paper_orders_leave_the_contracts_they_took_out_of_later_books(tmp_path, quick):
    latest = books()
    conn, cash, ex = executor(tmp_path, latest)
    run(ex)
    run(ex)
    first, second = stored(conn)
    assert (first["quantity"], second["quantity"]) == (50, 25)     # The first took 50 of each 100, so the second saw 50 and took half.
    assert ex.book(("polymarket_us", "pm")).asks == [[0.45, 25.0]]
    # A new book with the level still there keeps what we took off it. One where the level shrank below it keeps only that much,
    # and once the level is gone, what we took is forgotten, so the level coming back is whole again.
    latest[("polymarket_us", "pm")] = Book("polymarket_us", "pm", NOW, [[0.44, 100]], [[0.45, 100]])
    assert ex.book(("polymarket_us", "pm")).asks == [[0.45, 25.0]]
    latest[("polymarket_us", "pm")] = Book("polymarket_us", "pm", NOW, [[0.44, 100]], [[0.45, 30]])
    assert ex.book(("polymarket_us", "pm")).asks == []
    latest[("polymarket_us", "pm")] = Book("polymarket_us", "pm", NOW, [[0.44, 100]], [[0.46, 100]])
    assert ex.book(("polymarket_us", "pm")).asks == [[0.46, 100]]
    latest[("polymarket_us", "pm")] = Book("polymarket_us", "pm", NOW, [[0.44, 100]], [[0.45, 100]])
    assert ex.book(("polymarket_us", "pm")).asks == [[0.45, 100]]


@pytest.mark.parametrize("gone, sold, side, left", [
    (("kalshi", "k"), ("polymarket_us", "pm"), "bids", [[0.44, 50]]),     # Yes, the side its contract pays on, sold into the bid.
    (("polymarket_us", "pm"), ("kalshi", "k"), "asks", [[0.54, 50]]),     # No, the other side, sold as yes bought at the ask.
])
def test_a_sale_back_leaves_the_contracts_it_took_out_of_later_books(tmp_path, quick, gone, sold, side, left):
    latest = books()
    conn, cash, ex = executor(tmp_path, latest)
    run(ex, lambda: latest.pop(gone))
    assert stored(conn)[0]["hedge"].startswith(f"sold back 50 of 50 on {sold[0]}")
    assert getattr(ex.book(sold), side) == left         # The sale took 50 of the 100 at the level it sold into.


def test_a_leg_with_no_book_is_flattened_by_selling_the_other_back(tmp_path, quick):
    latest = books()
    conn, cash, ex = executor(tmp_path, latest)
    run(ex, lambda: latest.pop(("kalshi", "k")))
    t = stored(conn)[0]
    assert (t["yes_filled"], t["no_filled"], t["matched"], t["status"]) == (50, 0, 0, "failed")
    assert t["hedge"] == "sold back 50 of 50 on polymarket_us, no leg no book"
    assert t["hedge_pnl"] == pytest.approx(50 * (0.44 - 0.45))
    assert (t["yes_held"], t["no_held"]) == (0, 0)
    assert cash["polymarket_us"] == pytest.approx(10000 - 50 * 0.45 + 50 * 0.44)
    assert cash["kalshi"] == pytest.approx(10000)


def test_exposure_is_flattened_on_a_later_tick_once_a_book_allows_it(tmp_path, quick):
    latest = books()
    logs = []
    conn, cash, ex = executor(tmp_path, latest, logs.append)

    def kalshi_vanishes_and_polymarket_loses_its_bids():
        latest.pop(("kalshi", "k"))
        latest[("polymarket_us", "pm")] = Book("polymarket_us", "pm", NOW, [], [[0.45, 100]])

    run(ex, kalshi_vanishes_and_polymarket_loses_its_bids)
    t = stored(conn)[0]
    assert (t["yes_held"], t["no_held"], t["hedge"]) == (50, 0, "50 exposed, nothing to sell it into, no leg no book")
    assert list(ex.exposed) == [t["id"]]

    async def later():
        ex.tick(NOW)                                            # Still no bids, nothing to do.
        await asyncio.gather(*ex.tasks)
        latest.update(books(k_bid=0.53, k_ask=0.54))            # Kalshi is back, but buying no there would hold the money.
        latest[("polymarket_us", "pm")] = Book("polymarket_us", "pm", NOW, [[0.43, 40]], [[0.45, 100]])     # 40 bid, 20 for us.
        ex.tick("2026-09-20T17:31:00+00:00")
        await asyncio.gather(*ex.tasks)
        ex.tick("2026-09-22T21:30:00+00:00")                    # Past the payout, the rest is left to settle.
        await asyncio.gather(*ex.tasks)
    asyncio.run(later())
    t = stored(conn)[0]
    assert (t["yes_held"], t["no_held"], t["matched"], t["status"]) == (30, 0, 0, "failed")
    assert t["hedge"] == "50 exposed, nothing to sell it into, no leg no book, then sold back 20 of 50 on polymarket_us, 30 exposed at 17:31:00"
    assert t["hedge_pnl"] == pytest.approx(20 * (0.43 - 0.45))
    assert logs[-1] == ("paper flattened nfl game_winner 2026-09-22 CAR@ATL CAR: sold back 20 of 50 on polymarket_us, 30 exposed, "
                        "30 still exposed, hedge -0.40$")
    assert ex.exposed == {}
    assert cash["polymarket_us"] == pytest.approx(10000 - 50 * 0.45 + 20 * 0.43) and cash["kalshi"] == pytest.approx(10000)


def test_a_settled_trade_is_not_flattened_any_more(tmp_path, quick):
    from db.models import Contract
    latest = books()
    conn, cash, ex = executor(tmp_path, latest)
    database.upsert_contracts(conn, [Contract(venue=v, contract_id=c, market_id=c, event_id="e", series_id=None, sport="nfl", event_title=None,
                                              title="t", outcome="Yes", market_type=None, line=None, rules=None, start_time=KICKOFF,
                                              close_time=CLOSE, fee_info=None) for v, c in (("kalshi", "k"), ("polymarket_us", "pm"))], NOW)
    conn.execute("INSERT INTO pairs (id, label, kind, venues, contracts, flags, matched_at) VALUES (1, ?, 'game_winner', '', 2, '[]', ?)", (PAIR["label"], NOW))

    def kalshi_vanishes_and_polymarket_loses_its_bids():
        latest.pop(("kalshi", "k"))
        latest[("polymarket_us", "pm")] = Book("polymarket_us", "pm", NOW, [], [[0.45, 100]])

    run(ex, kalshi_vanishes_and_polymarket_loses_its_bids)
    assert list(ex.exposed) == [stored(conn)[0]["id"]]                  # 50 yes held, with nothing to flatten against.
    s = settle.Settler(conn, cash, lambda m: None, executor=ex)
    s.results = {"polymarket_us": lambda events: {"pm": (1.0, "2026-09-22T17:40:00+00:00")}}
    # Polymarket US has bids again, which could sell the rest back.
    latest[("polymarket_us", "pm")] = Book("polymarket_us", "pm", "2026-09-22T17:45:30+00:00", [[0.44, 100]], [[0.45, 100]])

    async def later():
        await s.settle("2026-09-22T17:45:00+00:00")                     # The contract was decided during the game and has settled.
        ex.tick("2026-09-22T17:46:00+00:00")
        await asyncio.gather(*ex.tasks)
    ex.clock = lambda: "2026-09-22T17:46:00+00:00"
    asyncio.run(later())
    t = stored(conn)[0]
    settlement = conn.execute("SELECT yes_payout, settled_at FROM settlements WHERE trade_id = ?", (t["id"],)).fetchone()
    assert ex.exposed == {} and ex.tasks == set()
    assert (t["yes_held"], t["no_held"], *settlement) == (50, 0, 50, "2026-09-22T17:40:00+00:00")
    assert [r[0] for r in conn.execute("SELECT reason FROM ledger ORDER BY id")][2:] == ["buy", "payout"]     # No sale after the payout.


def test_a_stale_book_is_not_flattened_against(tmp_path, quick):
    latest = books()
    clock = [NOW]
    conn, cash, ex = executor(tmp_path, latest, clock=lambda: clock[0])

    def kalshi_vanishes_and_polymarket_loses_its_bids():
        latest.pop(("kalshi", "k"))
        latest[("polymarket_us", "pm")] = Book("polymarket_us", "pm", NOW, [], [[0.45, 100]])

    run(ex, kalshi_vanishes_and_polymarket_loses_its_bids)
    assert list(ex.exposed) == [stored(conn)[0]["id"]]              # 50 yes held, no book to flatten against.

    async def at(now):
        clock[0] = now
        ex.tick(now)
        await asyncio.gather(*ex.tasks)

    # Once the game is on, a book goes stale when it stops changing. Polymarket US's bids come back at 17:30, but by 17:32 the
    # book has not changed for two minutes, as a closed market's would not.
    latest[("polymarket_us", "pm")] = Book("polymarket_us", "pm", "2026-09-22T17:30:00+00:00", [[0.44, 100]], [[0.45, 100]])
    asyncio.run(at("2026-09-22T17:32:00+00:00"))
    assert stored(conn)[0]["yes_held"] == 50 and list(ex.exposed) == [stored(conn)[0]["id"]]
    # Once the book changes again it is fresh, and the rest is sold back into it.
    latest[("polymarket_us", "pm")] = Book("polymarket_us", "pm", "2026-09-22T17:32:00+00:00", [[0.44, 100]], [[0.45, 100]])
    asyncio.run(at("2026-09-22T17:32:00+00:00"))
    t = stored(conn)[0]
    assert (t["yes_held"], t["no_held"], t["matched"]) == (0, 0, 0) and ex.exposed == {}


def test_rejected_orders_fail_without_a_hedge(tmp_path, quick, monkeypatch):
    monkeypatch.setattr(config, "PAPER_REJECT_PROBABILITY", 1)
    latest = books()
    conn, cash, ex = executor(tmp_path, latest)
    run(ex)
    t = stored(conn)[0]
    assert (t["status"], t["matched"], t["hedge"]) == ("failed", 0, "yes leg rejected, no leg rejected")
    assert cash.amounts == pytest.approx({"polymarket_us": 10000, "kalshi": 10000})


FUTURE = dict(PAIR, label="nfl champion 2027 CAR", kind="champion", game_date=None, team_a=None, team_b=None)   # A future, no game.
SEASON_END = "2027-02-14T00:00:00+00:00"            # When the future's contracts close and pay.
FUTURE_YES, FUTURE_NO = (dict(m, start_time=None, close_time=SEASON_END) for m in (YES, NO))


def as_live(ex):
    """
    Judge signals on a paper executor by live's rules, no game once kicked off and nothing paying within a day.
    """
    ex.in_play = False
    ex.pays_in_time = types.MethodType(Executor.pays_in_time, ex)


def test_signal_is_refused_for_thin_edges_poor_returns_payouts_within_a_day_and_games_under_way(tmp_path, quick):
    latest = books()
    conn, cash, ex = executor(tmp_path, latest)
    as_live(ex)                         # See test_paper_trades_bets_paying_within_a_day_in_play_too_and_nothing_further_out.
    assert ex.signal(PAIR, YES, NO, 0.015, 100, FEES, NOW) is False
    assert ex.signal(FUTURE, FUTURE_YES, FUTURE_NO, 0.08, 100, FEES, NOW) is False     # 8.7% until February is 22% a year, under 30.
    assert ex.signal(PAIR, YES, NO, 0.50, 100, FEES, "2026-09-21T21:00:00+00:00") is False        # Pays out in under 24 hours.
    assert ex.signal(PAIR, YES, NO, 0.50, 100, FEES, "2026-09-22T17:30:00+00:00") is False        # Under way.
    # A game none of the members gives the kickoff of pays at its close time, but could be under way, so it is not traded.
    unknown = [dict(m, start_time=None, close_time="2026-10-06T21:00:00+00:00") for m in (YES, NO)]
    assert ex.signal(PAIR, *unknown, 0.50, 100, FEES, NOW) is False
    assert ex.tasks == set() and stored(conn) == []


def at(ex, pair, yes, no, edge, now, fees=FEES):
    """
    Send one signal at now inside a loop and wait for its trade.
    """
    async def scenario():
        sent = ex.signal(pair, yes, no, edge, 100, fees, now)
        await asyncio.gather(*ex.tasks)
        return sent
    return asyncio.run(scenario())


def test_paper_trades_bets_paying_within_a_day_in_play_too_and_nothing_further_out(tmp_path, quick, monkeypatch):
    # Paper trades the bets on one event, a game here, before and while it is played, once it pays within 24 hours.
    monkeypatch.setattr(config, "MAX_PAYOUT_HOURS", 24)
    _, _, ex = executor(tmp_path / "soon", books())
    assert ex.signal(PAIR, YES, NO, 0.50, 100, FEES, NOW) is False                     # Pays out two days on.
    assert ex.signal(FUTURE, FUTURE_YES, FUTURE_NO, 0.50, 100, FEES, NOW) is False     # Pays out in February.
    assert ex.signal(PAIR, YES, NO, 0.015, 100, FEES, "2026-09-21T21:00:00+00:00") is False     # A thin edge is still refused.
    assert at(ex, PAIR, YES, NO, 0.50, "2026-09-21T21:00:00+00:00") is True           # Pays out in under 24 hours.
    _, _, ex = executor(tmp_path / "under way", books())
    assert at(ex, PAIR, YES, NO, 0.50, "2026-09-22T17:30:00+00:00") is True           # Under way.


def test_edges_before_kickoff_and_on_futures_that_pay_enough_are_traded(tmp_path, quick):
    conn, cash, ex = executor(tmp_path, books())
    as_live(ex)
    assert at(ex, PAIR, YES, NO, 0.08, "2026-09-21T20:00:00+00:00") is True      # The day before, paying 24.75 hours on.
    ex.books = lambda: books(pm_bid=0.24, pm_ask=0.25, k_bid=0.60, k_ask=0.61)   # 35 cents: 54% until February, 134% a year.
    assert at(ex, FUTURE, FUTURE_YES, FUTURE_NO, 0.35, NOW) is True
    before_game, future = stored(conn)
    assert (before_game["status"], before_game["quantity"], before_game["pays_at"]) == ("filled", 50, PAYS_AT)
    assert (future["status"], future["quantity"], future["pays_at"]) == ("filled", 50, SEASON_END)
    assert ex.games == {}                   # Both filled evenly, so neither is kept for flattening.


def test_a_trade_takes_half_the_book_with_no_cap(tmp_path, quick):
    conn, cash, ex = executor(tmp_path, books(size=5000))
    assert at(ex, PAIR, YES, NO, 0.08, NOW) is True and stored(conn)[0]["quantity"] == 2500       # Half the 5,000 shown.


def test_paper_opens_whole_contracts_only(tmp_path, quick):
    # Half of the 3 shown is 1.5, cut to 1, and half of 1.5 is 0.75, which paper, filling whole contracts, does not trade.
    conn, cash, ex = executor(tmp_path, books(size=3))
    assert at(ex, PAIR, YES, NO, 0.08, NOW) is True and stored(conn)[0]["quantity"] == 1
    conn, cash, ex = executor(tmp_path / "under", books(size=1.5))
    assert at(ex, PAIR, YES, NO, 0.08, NOW) is False and stored(conn) == []


@pytest.mark.full_share
def test_a_trade_takes_all_the_book_shows_by_default(tmp_path, quick):
    conn, cash, ex = executor(tmp_path, books(size=5000))
    assert at(ex, PAIR, YES, NO, 0.08, NOW) is True and stored(conn)[0]["quantity"] == 5000       # All of the 5,000 shown.


def test_legs_on_one_venue_share_its_cash(tmp_path, quick):
    other = dict(NO, venue="polymarket_us", contract_id="pm2")         # No through the other side of a second Polymarket US market.
    latest = {("polymarket_us", "pm"): books()[("polymarket_us", "pm")],
              ("polymarket_us", "pm2"): Book("polymarket_us", "pm2", NOW, [[0.53, 100]], [[0.54, 100]])}
    conn, cash, ex = executor(tmp_path, latest, start=10.0)            # 10 dollars on each venue.
    fees = {("polymarket_us", "pm"): NO_PM_FEES, ("polymarket_us", "pm2"): NO_PM_FEES}
    assert at(ex, PAIR, YES, other, 0.08, NOW, fees) is True
    # A contract of both legs costs 0.45 and 0.47 from the one venue's 10 dollars: 10 of them, not 22 of each.
    assert stored(conn)[0]["quantity"] == 10


def test_two_signals_at_once_share_the_balance_instead_of_both_spending_it(tmp_path, quick):
    latest = books()
    conn, cash, ex = executor(tmp_path, latest, start=30.0)      # Room for 50 contracts at 0.45 once, not twice.
    assert run(ex, signals=2) == [True, True]
    first, second = stored(conn)
    # The second saw what the first had reserved on both venues: 7.5 left on Polymarket US and 6.5 on Kalshi, so 13 at 0.47.
    assert (first["quantity"], second["quantity"]) == (50, 13)
    assert cash.amounts == pytest.approx({"polymarket_us": 30 - 63 * 0.45, "kalshi": 30 - 63 * 0.47})
    assert min(cash.amounts.values()) >= 0


def test_paper_trades_spend_the_cash_down_to_nothing(tmp_path, quick):
    latest = books()
    logs = []
    conn, cash, ex = executor(tmp_path, latest, logs.append, start=10.0)
    assert run(ex) == [True] and stored(conn)[0]["quantity"] == 21      # 10 to spend: 22 at 0.45 but 21 at 0.47.
    assert cash.amounts == pytest.approx({"polymarket_us": 10 - 21 * 0.45, "kalshi": 10 - 21 * 0.47})
    ex.tick(NOW)
    assert run(ex) == [False] and len(stored(conn)) == 1               # The 0.13 left on Kalshi pays for no more.
    assert not any("cash" in line for line in logs[1:])                 # Paper sends no alert however low it runs.


def test_a_trade_is_stored_as_sent_before_it_fills(tmp_path, quick):
    latest = books()
    conn, cash, ex = executor(tmp_path, latest)

    async def scenario():
        ex.signal(PAIR, YES, NO, 0.08, 100, FEES, NOW)
        return stored(conn)[0]["status"]

    assert asyncio.run(scenario()) == "sent"


def test_trips_are_drawn_around_the_live_orders_median_and_90th_percentile(tmp_path):
    conn, cash, ex = executor(tmp_path, {})
    ex.rng = random.Random(7)
    trips = [ex.trip("polymarket_us", "open") for _ in range(2000)]
    there, back = (sorted(1000 * trip[i] for trip in trips) for i in (0, 1))
    assert 55 < there[1000] < 63 and 122 < there[1800] < 150         # 59 ms at the median, 136 at the 90th percentile.
    assert 28 < back[1000] < 34 and 74 < back[1800] < 92             # 31 and 83.
    assert [round(1000 * t, 1) for t in ex.trip("kalshi", "flatten")] == [pytest.approx(12, abs=4), pytest.approx(8, abs=1)]


def test_scanner_signals_once_per_episode(tmp_path):
    from db.models import Bet, Contract, Pair
    conn = database.connect(tmp_path / "t.sqlite")
    members = [("kalshi", "k", NO_K_FEES), ("polymarket_us", "pm", NO_PM_FEES)]
    database.upsert_contracts(conn, [Contract(venue=v, contract_id=c, market_id=c, event_id="e", series_id=None, sport="nfl", event_title=None,
                                              title="t", outcome="Yes", market_type=None, line=None, rules=None, start_time=KICKOFF,
                                              close_time=CLOSE, fee_info=f) for v, c, f in members], NOW)
    bets = [Bet(v, c, "game_winner", 2027, PAIR["game_date"], "CAR", "ATL", "CAR", None, "yes") for v, c, _ in members]
    database.replace_bets(conn, "nfl", bets)
    database.replace_pairs(conn, "nfl", [Pair(PAIR["label"], "game_winner", 2027, PAIR["game_date"], "CAR", "ATL", "CAR", None, bets, [], sport="nfl")], NOW)
    calls = []
    s = scan.Scanner(conn, ("nfl",), lambda m: None,
                     on_signals=[lambda pair, yes, no, edge, size, fee_infos, now: calls.append((pair["label"], round(edge, 2), now)) or True])
    latest = books()
    s.on_book("polymarket_us", "pm", latest, NOW)
    latest[("polymarket_us", "pm")] = Book("polymarket_us", "pm", NOW, [[0.40, 100]], [[0.41, 100]])
    s.on_book("polymarket_us", "pm", latest, "2026-09-20T17:30:01+00:00")     # A bigger edge in the same episode brings no second signal.
    assert calls == [(PAIR["label"], 0.08, NOW)]


def test_a_paper_executor_refuses_money_of_another_mode(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = PaperBalances(conn)
    cash.mode = "live"
    with pytest.raises(ValueError, match="a paper executor cannot trade live money"):
        PaperExecutor(conn, cash, lambda: {})


def test_each_executor_is_offered_the_episode_until_it_takes_a_trade(tmp_path):
    from db.models import Bet, Contract, Pair
    conn = database.connect(tmp_path / "t.sqlite")
    members = [("kalshi", "k", NO_K_FEES), ("polymarket_us", "pm", NO_PM_FEES)]
    database.upsert_contracts(conn, [Contract(venue=v, contract_id=c, market_id=c, event_id="e", series_id=None, sport="nfl", event_title=None,
                                              title="t", outcome="Yes", market_type=None, line=None, rules=None, start_time=KICKOFF,
                                              close_time=CLOSE, fee_info=f) for v, c, f in members], NOW)
    bets = [Bet(v, c, "game_winner", 2027, PAIR["game_date"], "CAR", "ATL", "CAR", None, "yes") for v, c, _ in members]
    database.replace_bets(conn, "nfl", bets)
    database.replace_pairs(conn, "nfl", [Pair(PAIR["label"], "game_winner", 2027, PAIR["game_date"], "CAR", "ATL", "CAR", None, bets, [], sport="nfl")], NOW)
    live, paper = [], []
    busy = [True]           # The live executor turns the first moment down, say while its balance has not been read.
    s = scan.Scanner(conn, ("nfl",), lambda m: None, on_signals=[lambda *args: live.append(args[-1]) or not busy[0],
                                                             lambda *args: paper.append(args[-1]) or True])
    latest = books()
    s.on_book("polymarket_us", "pm", latest, NOW)
    busy[0] = False
    s.on_book("polymarket_us", "pm", latest, "2026-09-20T17:30:01+00:00")
    s.on_book("polymarket_us", "pm", latest, "2026-09-20T17:30:02+00:00")
    assert live == [NOW, "2026-09-20T17:30:01+00:00"] and paper == [NOW]


def moved(latest, venue, seconds_ago):
    """
    Stamp a book with the venue's time for its last change, this many seconds before NOW.
    """
    key = next(k for k in latest if k[0] == venue)
    latest[key] = dataclasses.replace(latest[key], at=epoch(NOW) - seconds_ago)


def test_an_edge_waits_for_the_polymarket_us_book_to_catch_up_with_a_kalshi_move(tmp_path, quick):
    latest = books()
    moved(latest, "kalshi", 0.1)                            # Kalshi just moved.
    moved(latest, "polymarket_us", 2.0)                     # Polymarket US's newest book is from before, so it may not show its reaction yet.
    conn, cash, ex = executor(tmp_path, latest)
    ex.edge_books = lambda pair_id: {key: epoch(NOW) - 5.0 for key in latest}   # Live counts from when the edge began, paper not.
    assert run(ex) == [False] and stored(conn) == []
    assert ex.summary().endswith("; 1 pairs' edges waited for a book to catch up")
    moved(latest, "polymarket_us", 0.05)                    # A newer Polymarket US book still shows the price.
    assert run(ex) == [True]


def test_an_edge_that_waits_asks_to_be_priced_again_when_the_wait_ends(tmp_path, quick):
    latest = books()
    moved(latest, "kalshi", 0.1)                            # Kalshi moved a tenth of a second ago, so the wait ends in two tenths.
    moved(latest, "polymarket_us", 2.0)
    conn, cash, ex = executor(tmp_path, latest)
    asked = []
    ex.recheck = lambda pair_id, seconds: asked.append((pair_id, round(seconds, 3)))
    assert run(ex) == [False]
    assert asked == [(PAIR["id"], round(config.CONFIRM_SECONDS["polymarket_us"] - 0.1, 3))]


def test_an_edge_trades_once_the_other_venues_move_is_older_than_the_wait_or_when_only_kalshi_is_behind(tmp_path, quick):
    latest = books()
    moved(latest, "kalshi", config.CONFIRM_SECONDS["polymarket_us"] + 0.01)     # Long enough ago that a reaction would have come.
    moved(latest, "polymarket_us", 5.0)
    conn, cash, ex = executor(tmp_path, latest)
    assert run(ex) == [True]
    moved(latest, "polymarket_us", 0.01)                    # Polymarket US just moved, Kalshi's book is older: Kalshi has no wait.
    moved(latest, "kalshi", 3.0)
    assert run(ex) == [True]


def test_a_sale_under_half_of_what_it_cost_is_not_made_and_the_contracts_are_kept(tmp_path, quick):
    latest = books()
    conn, cash, ex = executor(tmp_path, latest)

    def kalshi_vanishes_and_polymarket_bids_a_dime():
        latest.pop(("kalshi", "k"))
        latest[("polymarket_us", "pm")] = Book("polymarket_us", "pm", NOW, [[0.10, 100]], [[0.45, 100]])

    run(ex, kalshi_vanishes_and_polymarket_bids_a_dime)
    # 50 yes bought at 0.45: a dime would give back under half of that, so they are kept, a bet that may still pay out.
    t = stored(conn)[0]
    assert (t["yes_held"], t["no_held"], t["hedge"]) == (50, 0, "50 exposed, nothing to sell it into, no leg no book")
    latest[("polymarket_us", "pm")] = Book("polymarket_us", "pm", NOW, [[0.30, 100]], [[0.45, 100]])
    asyncio.run(ex.retry(NOW))                                          # 0.30 is two thirds of the cost.
    assert stored(conn)[0]["yes_held"] == 0 and ex.exposed == {}


def in_maintenance(*venues):
    """
    A stand in for the venues' maintenance windows, with the venues in theirs.
    """
    return lambda venue, now: venue in venues


def test_no_trade_opens_and_no_sale_goes_out_on_a_venue_that_is_not_trading(tmp_path, quick):
    latest = books()
    conn, cash, ex = executor(tmp_path, latest)
    ex.is_maintenance = in_maintenance("kalshi")                                      # Kalshi's Thursday maintenance, say.
    assert run(ex) == [False] and stored(conn) == []
    ex.is_maintenance = in_maintenance()

    def kalshi_vanishes():
        latest.pop(("kalshi", "k"))
        ex.is_maintenance = in_maintenance("polymarket_us")                           # And Polymarket US stops before the yes over is sold.

    run(ex, kalshi_vanishes)
    assert stored(conn)[0]["yes_held"] == 50 and list(ex.exposed) == [1]
    asyncio.run(ex.retry(NOW))
    assert stored(conn)[0]["yes_held"] == 50                            # Nothing is sent while it is stopped.
    ex.is_maintenance = in_maintenance()
    asyncio.run(ex.retry(NOW))
    assert stored(conn)[0]["yes_held"] == 0 and ex.exposed == {}


def test_no_trade_opens_and_no_sale_goes_out_where_the_exchange_says_it_has_stopped(tmp_path, quick):
    latest = books()
    conn, cash, ex = executor(tmp_path, latest)
    asked = []

    def trading(venue, shard):
        asked.append((venue, shard))
        return venue != "kalshi"
    ex.venue_trading = trading                                          # Kalshi found an issue and stopped, say.
    assert run(ex) == [False] and stored(conn) == [] and ("kalshi", None) in asked
    ex.venue_trading = lambda venue, shard: True
    assert run(ex) == [True]


# TIMED AS LIVE ORDERS ARE, against the books the recorder tapes

T = epoch(NOW)


@pytest.fixture
def timed(monkeypatch):
    """
    Trips of fixed length, in ms: Kalshi 12 there and 8 back, Polymarket US 60 there, 24 for a sale, and 30 back.
    """
    monkeypatch.setattr(config, "PAPER_ORDER_MS", {"kalshi": {"open": (12, 12), "flatten": (12, 12), "back": (8, 8)},
                                                   "polymarket_us": {"open": (60, 60), "flatten": (24, 24), "back": (30, 30)}})
    monkeypatch.setattr(config, "PAPER_FEED_SECONDS", {"kalshi": 0.05, "polymarket_us": 0.05})
    monkeypatch.setattr(config, "PAPER_REJECT_PROBABILITY", 0)
    monkeypatch.setattr(config, "MAX_PAYOUT_HOURS", 24 * 7)     # The pair pays two days after NOW.


def stamped(venue, bid, ask, made, received):
    """
    A book of the pair's contract on a venue, made at made by the venue's clock and reaching us at received by ours, both seconds after NOW.
    """
    contract = "pm" if venue == "polymarket_us" else "k"
    return Book(venue, contract, at_seconds(T + received), [[bid, 100]], [[ask, 100]], T + made)


def taped(tmp_path, changes):
    """
    Signal the pair at NOW against books(), then, once the trade's tapes are open, let each of changes reach us as the
    recorder would, all at once, and wait for the trade. Returns the database and the trade.
    """
    latest, tapes = books(), Tapes()
    conn = database.connect(tmp_path / "t.sqlite")
    ex = PaperExecutor(conn, PaperBalances(conn), lambda: latest, lambda m: None, random.Random(1), clock=lambda: NOW, tapes=tapes)

    async def scenario():
        assert ex.signal(PAIR, YES, NO, 0.08, 100, FEES, NOW)
        await asyncio.sleep(0)                  # The trade starts, and tapes its books from here.
        assert set(tapes.open) == {("polymarket_us", "pm"), ("kalshi", "k")}
        for book in changes:
            latest[(book.venue, book.contract_id)] = book
            tapes.add((book.venue, book.contract_id), book)
        await asyncio.gather(*ex.tasks)
        assert tapes.open == {}                 # Done with, once the trade is.
    asyncio.run(scenario())
    return conn, stored(conn)[0]


def test_an_order_meets_the_venues_book_as_it_was_when_the_order_arrived(tmp_path, timed):
    # Polymarket US took its 0.45 ask away 30 ms after the signal, by its clock, and the change reached us at 110 ms.
    # Our yes order got there at 60 ms, after the change, so it buys nothing, though our newest book when it got there
    # was still the old one. Kalshi's next change came after our no order got there at 12 ms, so that fills.
    conn, t = taped(tmp_path, [stamped("kalshi", 0.53, 0.54, 0.020, 0.032),
                               stamped("polymarket_us", 0.49, 0.50, 0.030, 0.110),
                               stamped("polymarket_us", 0.49, 0.50, 0.090, 0.170)])
    assert (t["status"], t["yes_filled"], t["no_filled"]) == ("failed", 0, 50)
    # Each answer came after a trip there and back, the later at 90 ms. The no leg was sold back then on the Kalshi book
    # we had seen by 90 ms, and the sale reached Kalshi at 102 ms and answered at 110.
    assert (t["yes_latency_ms"], t["no_latency_ms"], t["yes_fill_ts"], t["no_fill_ts"]) == (90, 20, at_seconds(T + 0.09), at_seconds(T + 0.02))
    assert (t["no_held"], t["hedge"]) == (0, "sold back 50 of 50 on kalshi")
    assert [r["ts"] for r in stored(conn, "ledger")][-1] == at_seconds(T + 0.11)


def test_a_change_the_venue_made_after_the_order_arrived_does_not_stop_it(tmp_path, timed):
    # The same change made at 70 ms, after our yes order got there at 60, though it reached us before that order's
    # fill was worked out.
    conn, t = taped(tmp_path, [stamped("kalshi", 0.53, 0.54, 0.020, 0.032),
                               stamped("polymarket_us", 0.49, 0.50, 0.070, 0.150)])
    assert (t["status"], t["yes_filled"], t["no_filled"], t["yes_cost"]) == ("filled", 50, 50, pytest.approx(50 * 0.45))


# IN PLAY BESIDE LIVE, Polymarket US's leg first and given back what our live orders took

STARTED = tuple(dict(m, start_time="2026-09-20T17:00:00+00:00") for m in (YES, NO))     # The game kicked off half an hour before NOW.


def beside_live(tmp_path, footprints):
    """
    Signal the pair half an hour into its game at NOW, on books of 5 a level, while live trades it too. Live's own yes
    order reached Polymarket US 30 ms after the signal and bought all 5 at 0.45, so the venue's books from then show the
    level gone. footprints, when given, is where that live order left what it took. Returns the database and paper's trade.
    """
    latest, tapes = {}, Tapes()
    for key, book in books(size=5).items():
        latest[key] = dataclasses.replace(book, at=T - 1)
    conn = database.connect(tmp_path / "t.sqlite")
    ex = PaperExecutor(conn, PaperBalances(conn), lambda: latest, lambda m: None, random.Random(1), clock=lambda: NOW,
                       tapes=tapes, footprints=footprints)
    live_leg = Leg("yes", "polymarket_us", "pm", "yes", limit=0.45, quantity=5, fee_info=NO_PM_FEES)

    async def scenario():
        sent = footprints.sent(live_leg, False, 0.45) if footprints else None
        assert ex.signal(PAIR, *STARTED, 0.08, 100, FEES, NOW)
        await asyncio.sleep(0)                  # The trade starts, and tapes its books from here.
        for book in (stamped("polymarket_us", 0.44, 0.46, 0.030, 0.110), stamped("polymarket_us", 0.44, 0.46, 0.100, 0.180),
                     stamped("kalshi", 0.53, 0.54, 0.020, 0.032)):
            latest[(book.venue, book.contract_id)] = book
            tapes.add((book.venue, book.contract_id), book)
        if sent:
            footprints.answer(sent, "polymarket_us", {"executions": [{"transactTime": at_seconds(T + 0.030)}]}, 5)
        await asyncio.gather(*ex.tasks)
    asyncio.run(scenario())
    return conn, stored(conn)[0]


@pytest.mark.full_share
def test_in_play_paper_sends_polymarket_us_first_and_gets_back_what_our_live_order_took(tmp_path, timed):
    conn, t = beside_live(tmp_path, Footprints(clock=lambda: T))
    # Its yes order reached Polymarket US at 60 ms, after our live order had bought the 5 at 0.45 there, which paper gives
    # back, so it is judged on the book as it would have been without our own order. Its answer came at 90 ms, 60 there
    # and 30 back, and only then went Kalshi's order: 12 there and 8 back.
    assert (t["status"], t["quantity"], t["yes_filled"], t["no_filled"]) == ("filled", 5, 5, 5)
    assert (t["yes_fill_ts"], t["no_fill_ts"]) == (at_seconds(T + 0.09), at_seconds(T + 0.11))


@pytest.mark.full_share
def test_in_play_without_giving_back_paper_finds_what_our_live_order_took_gone_and_sends_no_kalshi_order(tmp_path, timed):
    conn, t = beside_live(tmp_path, None)
    assert (t["status"], t["yes_filled"], t["no_filled"], t["hedge"]) == ("failed", 0, 0, "no leg not sent, as polymarket_us filled nothing first")
    assert stored(conn, "ledger")[-1]["reason"] == "transfer_in"        # No buy, so the money never moved.
