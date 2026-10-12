"""
Tests for settling paper trades with canned venue results.
"""

import asyncio
import pytest
from db import database
from db.models import Trade
from engine.components.money import settle
from engine.components.money.paper import PaperBalances

KICKOFF = "2026-09-20T17:00:00+00:00"
PAYS_AT = "2026-09-20T21:00:00+00:00"


def filled_trade(conn):
    """
    A done trade: 50 yes on Polymarket US at 0.45 and 50 no on Kalshi at 0.47, held through a yes contract on each venue.
    """
    conn.execute("INSERT INTO pairs (label, kind, venues, contracts, flags, matched_at) VALUES (?, 'game_winner', 'kalshi,polymarket_us', 2, '[]', ?)",
                 ("game_winner 2026-09-20 CAR@ATL CAR", KICKOFF))
    t = Trade(mode="paper", pair_id=1, label="game_winner 2026-09-20 CAR@ATL CAR", trade="yes: PMUS buy, no: K buy other side",
              signal_ts="2026-09-20T17:30:00+00:00", edge=0.08, quantity=50,
              yes_venue="polymarket_us", yes_contract="pm", yes_polarity="yes", yes_limit=0.45,
              no_venue="kalshi", no_contract="k", no_polarity="yes", no_limit=0.47, pays_at=PAYS_AT,
              yes_filled=50, yes_cost=22.5, no_filled=50, no_cost=23.5, yes_held=50, no_held=50, matched=50, profit=4.0, status="filled")
    database.insert_trade(conn, t)
    return t


def settled_at(conn):
    """
    When the trade settled, from its settlement, or None while it has none.
    """
    row = conn.execute("SELECT settled_at FROM settlements").fetchone()
    return row[0] if row else None


def settled(settler, now, results):
    """
    Run a settlement check with canned results, given as {(venue, contract_id): (what a yes contract paid, settled_at)}.
    """
    settler.results = {"kalshi": lambda ids: {c: r for (v, c), r in results.items() if v == "kalshi" and c in ids},
                       "polymarket_us": lambda events: {c: r for (v, c), r in results.items() if v == "polymarket_us"}}
    asyncio.run(settler.settle(now))


def test_settlement_pays_the_winning_leg_only_and_records_each_leg(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = PaperBalances(conn)
    logs = []
    s = settle.Settler(conn, cash, logs.append)
    trade = filled_trade(conn)
    settled(s, "2026-09-20T20:00:00+00:00", {})                 # Not due yet.
    assert settled_at(conn) is None
    settled(s, "2026-09-20T21:05:00+00:00",
            {("polymarket_us", "pm"): (1.0, "2026-09-20T20:10:00+00:00"), ("kalshi", "k"): (1.0, "2026-09-20T20:09:00+00:00")})
    row = conn.execute("SELECT * FROM settlements").fetchone()
    assert (row["trade_id"], row["mode"]) == (trade.id, "paper")
    # The bet resolved yes. The Polymarket US leg held the yes side and is paid a dollar each. The Kalshi leg held no and gets nothing.
    assert (row["yes_result"], row["yes_payout"], row["yes_settled_at"]) == ("yes", 50, "2026-09-20T20:10:00+00:00")
    assert (row["no_result"], row["no_payout"], row["no_settled_at"]) == ("yes", 0, "2026-09-20T20:09:00+00:00")
    assert row["settled_at"] == "2026-09-20T20:10:00+00:00"       # The later leg.
    assert cash.amounts == pytest.approx({"polymarket_us": 10000 + 50, "kalshi": 10000})
    assert [tuple(r) for r in conn.execute("SELECT venue, amount, reason, trade_id FROM ledger WHERE reason != 'transfer_in'")] == [
        ("polymarket_us", 50.0, "payout", trade.id)]                    # After the two opening balances.
    assert logs == [f"paper settled {trade.label}: polymarket_us yes yes pays 50.00$, kalshi no yes pays 0.00$, realized +4.00$"]
    assert s.summary() == "paper settled: 1 trades for +4.00$, 0 still open"


def test_a_trade_is_checked_from_kickoff_when_its_contracts_have_a_start_time(tmp_path):
    from db.models import Contract
    conn = database.connect(tmp_path / "t.sqlite")
    cash = PaperBalances(conn)
    s = settle.Settler(conn, cash, lambda m: None)
    database.upsert_contracts(conn, [Contract(venue=v, contract_id=c, market_id=c, event_id="e", series_id=None, sport="nfl", event_title=None,
                                              title="t", outcome="Yes", market_type=None, line=None, rules=None, start_time=KICKOFF,
                                              close_time=PAYS_AT, fee_info=None) for v, c in (("polymarket_us", "pm"), ("kalshi", "k"))], KICKOFF)
    filled_trade(conn)
    assert database.load_open_trades(conn, "paper")[0].starts_at == KICKOFF
    results = {("polymarket_us", "pm"): (1.0, "2026-09-20T17:20:00+00:00"), ("kalshi", "k"): (1.0, "2026-09-20T17:19:00+00:00")}
    settled(s, "2026-09-20T16:59:00+00:00", results)                  # Before kickoff, not looked at.
    assert settled_at(conn) is None
    settled(s, "2026-09-20T17:30:00+00:00", results)                  # During the game, a decided prop settles at once.
    assert settled_at(conn) == "2026-09-20T17:20:00+00:00"
    assert cash["polymarket_us"] == pytest.approx(10050)


def test_a_trade_waits_until_every_held_leg_has_a_result(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    s = settle.Settler(conn, PaperBalances(conn), lambda m: None)
    filled_trade(conn)
    settled(s, "2026-09-20T21:05:00+00:00", {("kalshi", "k"): (0.0, "2026-09-20T20:09:00+00:00")})
    assert settled_at(conn) is None
    assert s.summary() == "paper settled: 0 trades for +0.00$, 1 still open"


class FakeExecutor:
    """
    The part of the PaperExecutor the settler talks to.
    """

    def __init__(self, flattening=()):
        self.flattening = set(flattening)
        self.told = []

    def settled(self, trade_id):
        self.told.append(trade_id)


def test_a_trade_being_flattened_is_left_alone_and_the_executor_is_told_once_it_settles(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = PaperBalances(conn)
    trade = filled_trade(conn)
    executor = FakeExecutor(flattening=[trade.id])
    s = settle.Settler(conn, cash, lambda m: None, executor=executor)
    results = {("polymarket_us", "pm"): (1.0, "2026-09-20T20:10:00+00:00"), ("kalshi", "k"): (1.0, "2026-09-20T20:09:00+00:00")}
    settled(s, "2026-09-20T21:05:00+00:00", results)            # An order to flatten it is in flight.
    assert settled_at(conn) is None
    assert (cash["polymarket_us"], executor.told) == (10000, [])
    executor.flattening.clear()
    settled(s, "2026-09-20T21:06:00+00:00", results)
    assert settled_at(conn) == "2026-09-20T20:10:00+00:00"
    assert executor.told == [trade.id]


def test_payouts_follow_what_the_trade_holds_once_the_venues_answer(tmp_path, monkeypatch):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = PaperBalances(conn)
    trade = filled_trade(conn)
    s = settle.Settler(conn, cash, lambda m: None)

    def kalshi_results(ids):
        # While the venues are asked, the executor sells 20 of the Polymarket US leg back.
        trade.yes_held, trade.yes_cost = 30, 13.5
        database.update_trade(conn, trade)
        return {"k": (1.0, "2026-09-20T20:09:00+00:00")}

    async def inline(function, *args):
        return function(*args)                                  # In the loop's thread, so the lookup can write to the database.

    monkeypatch.setattr(settle.asyncio, "to_thread", inline)
    s.results = {"kalshi": kalshi_results, "polymarket_us": lambda events: {"pm": (1.0, "2026-09-20T20:10:00+00:00")}}
    asyncio.run(s.settle("2026-09-20T21:05:00+00:00"))
    row = conn.execute("SELECT yes_payout, settled_at FROM settlements").fetchone()
    assert tuple(row) == (30, "2026-09-20T20:10:00+00:00")       # Paid on the 30 still held, not the 50 held when the check began.
    assert cash["polymarket_us"] == pytest.approx(10000 + 30)


FUTURE_PAYS_AT = "2027-01-15T00:00:00+00:00"


def future_trade(conn, n):
    """
    A done future with no game behind it, 10 yes on Polymarket US contract pm{n} and 10 no on Kalshi contract k{n},
    paying out in January.
    """
    conn.execute("INSERT INTO pairs (label, kind, venues, contracts, flags, matched_at) VALUES (?, 'futures', 'kalshi,polymarket_us', 2, '[]', ?)",
                 (f"future {n}", KICKOFF))
    pair_id = conn.execute("SELECT MAX(id) FROM pairs").fetchone()[0]
    t = Trade(mode="paper", pair_id=pair_id, label=f"future {n}", trade="yes: PMUS buy, no: K buy other side", signal_ts=KICKOFF, edge=0.05,
              quantity=10, yes_venue="polymarket_us", yes_contract=f"pm{n}", yes_polarity="yes", yes_limit=0.45,
              no_venue="kalshi", no_contract=f"k{n}", no_polarity="yes", no_limit=0.5, pays_at=FUTURE_PAYS_AT,
              yes_filled=10, yes_cost=4.5, no_filled=10, no_cost=5.0, yes_held=10, no_held=10, matched=10, profit=0.5, status="filled")
    database.insert_trade(conn, t)
    return t


def test_a_future_resolved_before_its_payout_is_settled_on_the_slower_cadence(tmp_path, monkeypatch):
    from engine.helper import config
    monkeypatch.setattr(config, "SETTLE_CHECK_SECONDS", 30)
    monkeypatch.setattr(config, "FUTURES_SETTLE_CHECK_SECONDS", 60)
    conn = database.connect(tmp_path / "t.sqlite")
    cash = PaperBalances(conn)
    s = settle.Settler(conn, cash, lambda m: None)
    trades = [future_trade(conn, n) for n in (1, 2, 3)]
    asked = []
    # Kalshi has settled every leg, as with the eliminated teams' qualifiers in October. Polymarket US only the first two so far.
    kalshi = {f"k{n}": (paid, "2026-10-08T05:23:07+00:00") for n, paid in ((1, 0.0), (2, 1.0), (3, 0.0))}
    polymarket = {"pm1": (0.0, "2026-10-08T05:15:02+00:00"), "pm2": (1.0, "2026-10-08T05:15:02+00:00")}

    def kalshi_results(ids):
        asked.append(sorted(ids))
        return {c: kalshi[c] for c in ids if c in kalshi}
    s.results = {"kalshi": kalshi_results, "polymarket_us": lambda events: dict(polymarket)}

    def check(now):
        asked.clear()
        asyncio.run(s.settle(now))
        return asked[0] if asked else []
    # Three futures over a 60 second cadence with a pass every 30 seconds: two a pass, those checked longest ago first,
    # though each pays out in January.
    assert check("2026-10-11T03:30:00+00:00") == ["k1", "k2"]
    assert [r[0] for r in conn.execute("SELECT trade_id FROM settlements ORDER BY trade_id")] == [trades[0].id, trades[1].id]
    assert check("2026-10-11T03:30:30+00:00") == ["k3"]           # The other one, whose Polymarket US leg has not resolved.
    assert check("2026-10-11T03:31:00+00:00") == []               # Checked 30 seconds ago, so not its turn yet.
    polymarket["pm3"] = (0.0, "2026-10-11T03:31:10+00:00")
    assert check("2026-10-11T03:31:30+00:00") == ["k3"]
    row = conn.execute("SELECT no_result, no_payout, settled_at FROM settlements WHERE trade_id = ?", (trades[2].id,)).fetchone()
    assert tuple(row) == ("no", 10, "2026-10-11T03:31:10+00:00")   # The Kalshi leg held no on a contract that resolved no.
    # The first future paid its no leg, the second its yes leg, the third its no leg: 10$ each into its venue.
    assert cash.amounts == pytest.approx({"polymarket_us": 10000 + 10, "kalshi": 10000 + 20})
    assert check("2026-10-11T03:32:00+00:00") == []               # None left open.


def test_a_restart_spreads_the_futures_over_the_passes_rather_than_asking_about_all_at_once(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    s = settle.Settler(conn, PaperBalances(conn), lambda m: None)
    for n in range(1, 241):
        future_trade(conn, n)
    asked = []
    s.results = {"kalshi": lambda ids: asked.extend(ids) or {}, "polymarket_us": lambda events: {}}
    asyncio.run(s.settle("2026-10-11T03:30:00+00:00"))
    assert sorted(asked) == ["k1", "k2"]      # 240 futures over an hour with a pass every 30 seconds.


def test_fractional_results_pay_part_of_a_dollar_on_each_side(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    cash = PaperBalances(conn)
    logs = []
    s = settle.Settler(conn, cash, logs.append)
    trade = filled_trade(conn)
    # A drawn CS2 match: Polymarket US paid 0.41 on its yes, the long side, and Kalshi 0.60 on its yes, which the trade holds no on.
    settled(s, "2026-09-20T21:05:00+00:00",
            {("polymarket_us", "pm"): (0.41, "2026-10-09T14:25:43+00:00"), ("kalshi", "k"): (0.6, "2026-10-09T19:37:23+00:00")})
    row = conn.execute("SELECT * FROM settlements").fetchone()
    assert (row["yes_result"], row["yes_payout"]) == ("0.41", pytest.approx(50 * 0.41))
    assert (row["no_result"], row["no_payout"]) == ("0.6", pytest.approx(50 * 0.4))
    assert cash.amounts == pytest.approx({"polymarket_us": 10000 + 20.5, "kalshi": 10000 + 20})
    assert logs == [f"paper settled {trade.label}: polymarket_us yes 0.41 pays 20.50$, kalshi no 0.6 pays 20.00$, realized -5.50$"]


def test_a_nested_trade_landing_between_its_lines_is_paid_on_both_legs(tmp_path):
    # Yes on the lower line through Polymarket US's contract and no on the higher line through Kalshi's, see catalog/nest.py:
    # a number between the lines resolves the first yes and the second no, and each leg is paid by its own contract.
    conn = database.connect(tmp_path / "t.sqlite")
    cash = PaperBalances(conn)
    logs = []
    s = settle.Settler(conn, cash, logs.append)
    trade = filled_trade(conn)
    settled(s, "2026-09-20T21:05:00+00:00",
            {("polymarket_us", "pm"): (1.0, "2026-09-20T20:10:00+00:00"), ("kalshi", "k"): (0.0, "2026-09-20T20:09:00+00:00")})
    row = conn.execute("SELECT * FROM settlements").fetchone()
    assert (row["yes_result"], row["yes_payout"], row["no_result"], row["no_payout"]) == ("yes", 50, "no", 50)
    assert cash.amounts == pytest.approx({"polymarket_us": 10000 + 50, "kalshi": 10000 + 50})
    assert logs[-1].endswith("realized +54.00$")                       # The 4.00$ locked in and a second dollar a pair.
