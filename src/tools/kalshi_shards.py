"""
Split the Kalshi cash across the exchange shards live trading uses, by the
percents config.LIVE_SHARDS gives them.

Kalshi keeps the cash on each exchange shard apart, and an order spends
only its market's shard's: football, hockey, soccer, national teams'
too, motorsport, UFC, darts, esports, and politics trade on shard 0, and
baseball, basketball, and tennis on 3. Most of the cash goes to
football's, 80%, as the football futures are where the long-lasting
edges are, and 20% to baseball's, basketball's, and tennis'. This reads
the cash on each shard, moves what is over a shard's share to the shards
under theirs, and then sets Kalshi's own target split to the same
shares, which Kalshi rebalances to every 10 seconds, so payouts landing
on one shard are shared out too. It does not refill a shard whose cash
orders spend, so run this again when one shard runs low. The money stays
in the account, and nothing is traded.
Without --apply it only says what it would do.

Run from src/ on the instance, where the Kalshi key is, with:
    python3 -m tools.kalshi_shards
    python3 -m tools.kalshi_shards --apply
"""

import argparse
import time
from api import kalshi
from engine.helper import config

SETTLE_SECONDS = 5      # How long to wait after the moves before reading the shards again.


def moves(balances, split):
    """
    The moves that bring each shard's cash to its share of the whole, as
    [(dollars, from shard, to shard)], given the dollars on each shard,
    {shard: dollars}, and the split, {shard: percent}. A shard outside the
    split gives up all it has. Worked in hundredths of a cent, Kalshi's
    unit, so the shares add up to the whole, the first shard taking what is
    left over. A move of less than a cent is not worth making.
    """
    held = {shard: round(dollars * 10000) for shard, dollars in balances.items()}
    total = sum(held.values())
    target = {shard: total * percent // 100 for shard, percent in split.items()}
    target[next(iter(split))] += total - sum(target.values())
    shards = sorted(set(held) | set(target))
    over = [[held.get(s, 0) - target.get(s, 0), s] for s in shards if held.get(s, 0) > target.get(s, 0)]
    under = [[target.get(s, 0) - held.get(s, 0), s] for s in shards if target.get(s, 0) > held.get(s, 0)]
    out = []
    while over and under:
        amount = min(over[0][0], under[0][0])
        if amount >= 100:
            out.append((amount / 10000, over[0][1], under[0][1]))
        for side in (over, under):
            side[0][0] -= amount
            if not side[0][0]:
                side.pop(0)
    return out


def shards_line(dollars, balances):
    """
    The cash on the account and on each shard, in one line.
    """
    return f"kalshi {dollars:,.2f}$ available, by exchange shard: " + ", ".join(f"shard {s} {a:,.2f}$" for s, a in sorted(balances.items()))


def split_shards(apply, read=kalshi.balances, move=kalshi.move_between_shards, set_split=kalshi.set_shard_split,
                 wait=time.sleep, out=print):
    """
    Say how the cash would move to split it over config.LIVE_SHARDS["kalshi"], and when apply is true move it,
    then set Kalshi's target split, and read the shards again.
    """
    split = dict(config.LIVE_SHARDS["kalshi"])
    dollars, balances = read()
    out(shards_line(dollars, balances))
    out("target split: " + ", ".join(f"shard {s} {p}%" for s, p in split.items()))
    planned = moves(balances, split)
    for amount, source, destination in planned:
        out(f"to move: {amount:,.4f}$ from shard {source} to shard {destination}")
    if not planned:
        out("already split, nothing to move")
    if not apply:
        out("nothing done: run again with --apply to move the money and set Kalshi's target split")
        return
    for amount, source, destination in planned:
        out(f"moved {amount:,.4f}$ from shard {source} to shard {destination}: {move(amount, source, destination)}")
    out(f"target split set: {set_split(split)}")
    wait(SETTLE_SECONDS)
    out("now " + shards_line(*read()))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Split the Kalshi cash across the exchange shards live trading uses, by config.LIVE_SHARDS.")
    ap.add_argument("--apply", action="store_true", help="move the money and set Kalshi's target split, rather than only saying how")
    split_shards(ap.parse_args().apply)
