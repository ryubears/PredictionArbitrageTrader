"""
What a venue answered to an order, in the same words for every venue.

Each venue client's place_order() returns an Answer, so the live executor
never reads a venue's own field names.
"""

from typing import NamedTuple
from common.timeutil import epoch

STEP = 0.01     # The least part of a contract both venues fill and take orders for, a hundredth.
NOT_TRADING = "market not trading"      # How a closed order's note begins, see closed().


class Answer(NamedTuple):
    """
    What came back for one immediate or cancel order.
    """
    order_id: str | None    # The venue's id for the order, None when it never took it.
    status: str             # 'filled', 'partial', 'unfilled', 'unfunded' when the venue lacked our cash for it, 'closed' when it
                            # turned it away because the market was not trading, 'rejected' when it refused it, or 'error' when
                            # we cannot tell what happened.
    filled: float           # Contracts bought or sold, to the hundredth, see exact().
    dollars: float          # Paid for a buy or received for a sale, fees included.
    fees: float
    note: str | None        # Why the venue refused the order, or the error, when there is one.
    response: dict          # The venue's answer as it came, for reconciling.


def status(filled, quantity):
    """
    The status of an order the venue took, from how much of it filled.
    """
    return "filled" if filled >= quantity else "partial" if filled else "unfilled"


def exact(contracts):
    """
    A number of contracts to the hundredth both venues count in, STEP, so a
    sum of fills, 0.1 then 0.89 then 0.01 of a contract, lands on the 1 it
    makes rather than on a float just under it.
    """
    return round(contracts + 0.0, 2)


def size(contracts):
    """
    A number of contracts as an order's size, to the hundredth, and a whole
    number when it is one.
    """
    contracts = exact(contracts)
    return int(contracts) if contracts == int(contracts) else contracts


def unknown(error, order_id=None, response=None):
    """
    The Answer for an order whose fate we cannot know: it timed out, the
    connection dropped, the venue failed on its side, or it answered without
    saying how the order ended. It may have traded.
    """
    return Answer(order_id, "error", 0, 0.0, 0.0, repr(error)[:500], {"error": repr(error), **(response or {})})


def refused(error):
    """
    The Answer for an order the venue refused, with the reason it gave.
    """
    return Answer(None, "rejected", 0, 0.0, 0.0, error.body[:500], {"error": error.body, "status": error.status})


def unfunded(error):
    """
    The Answer for an order the venue turned away for lack of cash in the
    account. Nothing traded, and the venue is no less able to trade, so it
    is no refusal: the same order may go through once cash arrives.
    """
    return Answer(None, "unfunded", 0, 0.0, 0.0, f"not enough funds: {error.body[:300]}", {"error": error.body, "status": error.status})


def closed(error):
    """
    The Answer for an order the venue turned away because its market was
    not trading: paused, closed, or decided, or the exchange stopped.
    Nothing traded, and the venue is no less able to trade, so it is no
    refusal either: the market is left alone until it trades again, see
    market/record.py.
    """
    return Answer(None, "closed", 0, 0.0, 0.0, f"{NOT_TRADING}: {error.body[:300]}", {"error": error.body, "status": error.status})


def unfilled(error, why):
    """
    The Answer for an order the venue turned away with an error that is no
    refusal of ours, since nothing traded and the next order may well go
    through, such as a lack of cash on a Kalshi shard, with why in words
    ahead of what the venue said.
    """
    return Answer(None, "unfilled", 0, 0.0, 0.0, f"{why}: {error.body[:300]}", {"error": error.body, "status": error.status})


def venue_time(venue, response):
    """
    When the venue handled an order, by its own clock, the one it stamps
    its books with, in seconds since 1970, from its answer as it came, or
    None when the answer does not say: Kalshi's ts_ms, and the time of
    Polymarket US's last execution, or its order's creation without one.
    """
    response = response or {}
    if venue == "kalshi":
        return response["ts_ms"] / 1000 if response.get("ts_ms") else None
    execution = (response.get("executions") or [{}])[-1]
    return epoch(execution.get("transactTime") or (execution.get("order") or {}).get("createTime"))
