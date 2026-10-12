"""
When a game is played, whether it has started, and when the bets on it pay out.

Every timing assumption about games lives here, so the recorder, the
scanner, and the executor agree on them: when a game is expected to end,
when its money comes back, how long after kickoff it may still be under
way, and whether it has started, after which its pairs are not traded and
their books go stale if they stop changing. How long a game is expected
to last depends on the sport, see config.GAME_HOURS: three in four of the
sport's past games had ended by then. Both venues settle a game within
half an hour of its final whistle, see config.SETTLE_HOURS.
"""

from common.timeutil import days_between, shift
from engine.helper import config


def expected_end(kickoff, sport):
    """
    When a game of the sport that kicked off at kickoff is expected to end, config.GAME_HOURS later.
    """
    return shift(kickoff, hours=config.GAME_HOURS[sport])


def money_back(end):
    """
    When the money on a game that ends at end comes back: config.SETTLE_HOURS later, once the venues have settled.
    """
    return shift(end, hours=config.SETTLE_HOURS)


def recorded_since(now):
    """
    The kickoff after which a game may still be under way at now, config.RECORD_HOURS earlier. Its books are recorded
    until then, whatever its contracts' close times say.
    """
    return shift(now, hours=-config.RECORD_HOURS)


def resolution_time(start_time, close_time, sport):
    """
    When a contract of the sport pays out: once the venues settle after the final whistle, or, for a contract whose
    venue gives no kickoff, like Kalshi's, at its close time. A pair pays at the latest of its members', so the kickoff rules.
    """
    if start_time:
        return money_back(expected_end(start_time, sport))
    return close_time


def kickoff(members):
    """
    The latest kickoff among the members, or None when none of them is a game.
    """
    return max((m["start_time"] for m in members if m["start_time"]), default=None)


def started(game_date, members, now):
    """
    Whether the game of game_date that a pair with these members is on may
    have started by now: its kickoff has passed, or no member gives it. A
    future, whose game_date is None, never starts. A pair is traded only
    until its game starts, and only then do its books go stale when they
    stop changing, see pricing.fresh(): before kickoff a game's markets are
    open, and a future's until its season settles, so their books may rest
    unchanged for hours.
    """
    if game_date is None:
        return False
    start = kickoff(members)
    return start is None or start <= now


def past_close(game_date, member, now):
    """
    Whether a future's member is past its close time at now, so its market
    has stopped trading or its bet is expected to be decided, and is not
    traded. A game's member never is: Kalshi gives a game's contract the
    time Kalshi expects it to end, which many games outlast, and a game's
    markets say themselves when they close, see market/record.py.
    """
    close = member.get("close_time")
    return game_date is None and bool(close) and close <= now


def days_until(now, pays_at):
    """
    Days from now until a bet paying at pays_at pays, at least an hour, for its return a year, or None for a bet that
    gives no payout time.
    """
    return max(days_between(now, pays_at), 1 / 24) if pays_at else None


def pays_at(members, sport):
    """
    When the slowest of a pair's members pays out, since capital is locked until then. None when none of them says.
    """
    return max((t for t in (resolution_time(m["start_time"], m["close_time"], sport) for m in members) if t), default=None)
