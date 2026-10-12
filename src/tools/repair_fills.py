"""
Repair the live trades recorded under an older reading of Polymarket US fills.

Polymarket US fills an order in pieces as small as a hundredth of a
contract, 0.1 then 0.89 then 0.01 for one. An older reading cut each
piece, or their sum, to whole contracts, so an order that filled in full
was stored as filled short or not at all, and its trade was flattened
against what it did not hold. Fills are counted to the hundredth now,
see orders.exact().

This reads every stored Polymarket US answer again with
polymarket_us.read_answer(), corrects each order read differently, and
works out each trade with a corrected order again from its orders, as the
executor does: what each leg filled and holds, what that cost, the profit
locked in, and what flattening made or lost. The live executor flattens a
repaired trade holding more on one side than the other at its next start,
by selling the excess back. A trade whose orders sold more of a leg than it
held cannot be written as a trade, so it is left as it is, with every other
trade on its bet, for a human to settle with the venue. Last it reads each
venue's positions and compares them with what the live trades then hold,
contract by contract.

A trade whose record fell behind its orders for another reason, such as
a sale that went unrecorded, is worked out again from its orders when
named with --trade.

Stop the recorder first, so no order is in flight and the executor reads
the repaired trades when it starts. Without --apply it only says what it
would change. Run from src/ with:
    python3 -m tools.repair_fills
    python3 -m tools.repair_fills --apply
    python3 -m tools.repair_fills --trade 48 --apply
"""

import argparse
import dataclasses
import json
from api import polymarket_us
from api.orders import exact
from db import database
from db.models import Trade
from engine.components.trading.live import POSITIONS

TOLERANCE = 1e-6    # Dollars and contracts closer than this are the same.


class Oversold(Exception):
    """
    A trade's orders sold more of a leg than it held, which a trade cannot hold.
    """


def reread(order):
    """
    The order as its stored answer reads now, or None when it reads the same
    or is not a Polymarket US order whose answer says how it ended.
    """
    if order.venue != "polymarket_us" or not order.response:
        return None
    answer = polymarket_us.read_answer(json.loads(order.response), order.action, order.outcome, order.quantity)
    if answer is None:
        return None
    if (answer.status, answer.filled) == (order.status, order.filled) and abs(answer.dollars - order.dollars) < TOLERANCE \
            and abs(answer.fees - order.fees) < TOLERANCE:
        return None
    return dataclasses.replace(order, status=answer.status, filled=answer.filled, dollars=answer.dollars, fees=answer.fees, note=answer.note)


def replay(trade, orders):
    """
    The trade worked out again from its orders, oldest first, as the
    executor did: each leg's opening order sets what it filled and paid,
    each sale takes contracts off the leg at the average they cost, and
    what the sale brought over that cost is flattening's gain or loss.
    Raises Oversold when a sale takes a leg below nothing.
    """
    side = {(trade.yes_venue, trade.yes_contract): "yes", (trade.no_venue, trade.no_contract): "no"}
    filled, paid, held, cost = {"yes": 0, "no": 0}, {"yes": 0.0, "no": 0.0}, {"yes": 0, "no": 0}, {"yes": 0.0, "no": 0.0}
    hedge = 0.0
    for o in orders:
        leg = side[(o.venue, o.contract_id)]
        if o.purpose == "open":
            filled[leg], paid[leg], held[leg], cost[leg] = o.filled, o.dollars, o.filled, o.dollars
        elif o.action == "sell":
            if o.filled > held[leg]:
                raise Oversold(f"order {o.id} sold {o.filled:g} of the {leg} leg, which held {held[leg]:g}")
            average = cost[leg] / held[leg] if held[leg] else 0.0
            held[leg] = exact(held[leg] - o.filled)
            cost[leg] -= o.filled * average
            hedge += o.dollars - o.filled * average
        else:
            raise ValueError(f"order {o.id} buys to flatten, which this repair does not replay")
    matched = min(filled["yes"], filled["no"])
    average = {leg: paid[leg] / filled[leg] if filled[leg] else 0.0 for leg in filled}
    return dataclasses.replace(
        trade, yes_filled=filled["yes"], no_filled=filled["no"], yes_held=held["yes"], no_held=held["no"],
        yes_cost=cost["yes"], no_cost=cost["no"], matched=matched, profit=matched * (1 - average["yes"] - average["no"]), hedge_pnl=hedge,
        status="failed" if matched == 0 else "partial" if matched < trade.quantity else "filled")


def differs(a, b):
    """
    The fields the repair sets that differ between two versions of a trade, in words.
    """
    out = []
    for name in ("yes_filled", "no_filled", "yes_held", "no_held", "matched", "status", "yes_cost", "no_cost", "profit", "hedge_pnl"):
        x, y = getattr(a, name), getattr(b, name)
        if (abs(x - y) >= TOLERANCE) if isinstance(x, float) else x != y:
            out.append(f"{name} {x:.4g} -> {y:.4g}" if isinstance(x, float) else f"{name} {x} -> {y}")
    return out


def holdings(trades):
    """
    What the trades hold of each contract, as {(venue, contract): contracts}, positive for its yes and negative for its no.
    """
    out = {}
    for t in trades:
        for leg in t.legs():
            contracts = t.yes_held if leg.side == "yes" else t.no_held
            key = (leg.venue, leg.contract_id)
            out[key] = out.get(key, 0) + (contracts if leg.outcome == "yes" else -contracts)
    return out


def repair(conn, apply, positions=POSITIONS, out=print, rewrite=()):
    """
    Correct the misread orders and their trades, writing them when apply is
    true, then compare what the live trades hold with each venue's positions.
    The trades in rewrite are worked out again from their orders though no
    order of theirs was misread, for a trade whose record is known to have
    fallen behind its orders. Any other trade that works out differently
    stops the repair, since the replay itself would then be in doubt.
    Returns the ids of the trades left for a human.
    """
    orders = database.load_orders(conn)
    corrected = {o.id: o for o in (reread(o) for o in orders) if o}
    for o in orders:
        if o.id in corrected:
            new = corrected[o.id]
            out(f"order {o.id} of trade {o.trade_id}: {o.action} {o.quantity:g} {o.outcome} {o.status} {o.filled:g} -> {new.status} {new.filled:g}, "
                f"{o.dollars:.4f}$ -> {new.dollars:.4f}$")
    by_trade = {}
    for o in orders:
        by_trade.setdefault(o.trade_id, []).append(corrected.get(o.id, o))
    trades = {r["id"]: Trade(**dict(r)) for r in conn.execute(
        "SELECT * FROM trades t WHERE mode = 'live' AND NOT EXISTS (SELECT 1 FROM settlements s WHERE s.trade_id = t.id) ORDER BY id")}
    repaired, oversold = {}, {}
    for trade_id, trade in trades.items():
        try:
            again = replay(trade, by_trade.get(trade_id, []))
        except Oversold as e:
            oversold[trade_id] = str(e)
            continue
        if any(o.id in corrected for o in by_trade.get(trade_id, [])) or (trade_id in rewrite and differs(trade, again)):
            repaired[trade_id] = again
        elif differs(trade, again):
            raise ValueError(f"trade {trade_id} has no misread order, yet works out differently again: {', '.join(differs(trade, again))}")
    left = {t for t in trades if trades[t].pair_id in {trades[o].pair_id for o in oversold}}
    for trade_id in sorted(left):
        repaired.pop(trade_id, None)
        out(f"trade {trade_id} {trades[trade_id].trade}: left for a human, " + oversold.get(trade_id, "on the same bet as one that was oversold"))
    for trade_id, again in repaired.items():
        again.hedge = f"{trades[trade_id].hedge}; " + ("worked out again from its orders" if trade_id in rewrite else "repaired from the venue's answers")
        out(f"trade {trade_id}: " + ", ".join(differs(trades[trade_id], again)))
    if apply:
        for o in corrected.values():
            if o.trade_id not in left:
                database.update_order(conn, o)
        for again in repaired.values():
            database.update_trade(conn, again)
        out(f"wrote {sum(o.trade_id not in left for o in corrected.values())} orders and {len(repaired)} trades")
    else:
        out("nothing written: run again with --apply to write the repaired orders and trades")
    held = holdings([repaired.get(t, trades[t]) for t in trades])
    venue = {(v, contract): contracts for v, read in positions.items() for contract, contracts in read().items()}
    for key in sorted(set(held) | set(venue)):
        ours, theirs = held.get(key, 0), venue.get(key, 0)
        if abs(ours - theirs) >= TOLERANCE:
            out(f"position {key[0]} {key[1]}: the trades hold {ours:g}, the venue {theirs:g}")
    return sorted(left)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Repair the live trades whose Polymarket US fills were misread.")
    ap.add_argument("--apply", action="store_true", help="write the repaired orders and trades, rather than only saying what would change")
    ap.add_argument("--trade", type=int, action="append", default=[],
                    help="also work out this trade again from its orders, though none was misread; may be given more than once")
    args = ap.parse_args()
    repair(database.connect(), args.apply, rewrite=args.trade)
