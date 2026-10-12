"""
Print a short summary of the database: the pairs, the opportunities the
executors' rules trade, and the trades.

Everything is shown for the futures, then for the games, matches, and
races in play, each under a heading of its own, see MARKETS: its
opportunities, then its trades for each mode, paper then live, or one
with --mode. Opportunities are shown as live takes them: an episode
counts when one order could have had config.LIVE_MIN_CONTRACTS or more,
since live opens a trade with no fewer, on the levels at live's least
edge or more, see pricing.live_min_edge(), once the edge has lasted
config.LIVE_HOLD_SECONDS there, as live waits for it to: on a future
paying config.MIN_PAYOUT_HOURS or more out, the levels returning
config.MIN_ANNUAL_PCT a year, and on a game under way paying within
config.MAX_PAYOUT_HOURS, those at config.LIVE_IN_PLAY_MIN_EDGE or more.
Which moment is the scanner's, see Opportunity.take_size. Each market
shows what those orders could have taken at full size and what that locks
in, overall, by sport and kind, and the largest, and how long the edge
stayed at config.MIN_EDGE or more, in seconds to the thousandth. A
market's trades show their net for the window, what the trades opened in
it locked in and hedged with what exposed contracts made or lost settling
in it and the second dollar a pair of the nested trades both of whose
legs paid, see catalog/nest.py, then by kind for the window, the largest,
each open trade opened in it, the trades by outcome, and last the open
trades in a few lines: how many, how many of them opened in the last hour,
day, and week and the capital those hold, the capital they all hold, less
what netting on the venues has already paid back of live's, and the profit
they are expected to return on it, its rate a year, and when they resolve.
After every market come each mode's totals since its first trade, for the
futures and the games in play apart: the capital its trades, settled and
open, put in, what they returned or are expected to, and its rate a year.
The paper money and the live money come last. --market narrows everything
to the futures or the games in play, and --sport to some sports. Reads
only, so it is safe to run while the live process is writing.

The script sets its own import path, so it runs from any folder. The live
money is not in the database but on the venues, so it is read from each
venue with the same read-only calls the live process makes, using the
keys in data/. --no-live leaves that out, and a venue that cannot be read
says why.

Run with:
    python3 src/tools/summary.py
    python3 src/tools/summary.py --hours 6
    python3 src/tools/summary.py --mode paper
    python3 src/tools/summary.py --mode live --sport nfl,ncaaf
    python3 src/tools/summary.py --mode live --market in-play
    python3 src/tools/summary.py --no-live
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))     # src, so the script runs from any folder.
from common.paths import NAME
from common.stats import quantile
from common.timeutil import at_seconds, days_between, epoch, now_iso, shift
from common.venues import is_maintenance
from db.database import DB_PATH, netted, read_only
from db.schema import table_columns
from engine.helper import config, game

MODES = {"live": ("live",), "paper": ("paper",), "all": ("paper", "live")}     # What --mode shows, in order.
MARKETS = ("futures", "in-play")    # What everything is shown under, in order, see in_market() and print_market().
MARKET_CHOICES = {"futures": ("futures",), "in-play": ("in-play",), "all": MARKETS}    # What --market shows.
LARGEST = 5         # The largest opportunities and trades listed.
OPENED_HOURS = {"hour": 1, "day": 24, "week": 24 * 7}     # The windows the open trades are counted as opened in.
WIDTH = 100         # The longest line a list of items wraps at.
PAID = "COALESCE(s.yes_payout, 0) + COALESCE(s.no_payout, 0)"     # What a settlement s paid on both legs, in SQL.
STATUSES = ("filled", "partial", "failed", "sent")     # The order trade statuses are shown in, best first. One not listed comes last.


def query_rows(conn, sql, params=()):
    """
    Run a query and return the rows as tuples.
    """
    return [tuple(r) for r in conn.execute(sql, params)]


def first_value(conn, sql, params=()):
    """
    Run a query and return the first column of the first row.
    """
    return conn.execute(sql, params).fetchone()[0]


def short_time(ts):
    """
    An ISO timestamp cut to date and seconds, or a dash when missing.
    """
    return ts[:19].replace("T", " ") if ts else "-"


def contracts(value):
    """
    A count of contracts, to the hundredth they are traded in, with thousands separated and no trailing zeros.
    """
    return f"{value or 0:,.2f}".rstrip("0").rstrip(".")


def dollars(value):
    """
    Dollars to the cent with thousands separated, never '-0.00' for a sum that came to a hair under nothing.
    """
    return f"{round(value, 2) + 0.0:,.2f}"


def percent(value):
    """
    A percent for a table cell, or a dash when it cannot be told.
    """
    return "-" if value is None else f"{value:,.1f}"


def print_table(title, header, body, left=1):
    """
    Print a small aligned table with a title line, its first left columns aligned left and the rest right.
    """
    print(f"\n{title}")
    widths = [max(len(str(r[i])) for r in [header] + body) for i in range(len(header))]
    for r in [header] + body:
        print("  " + "  ".join(str(v).ljust(w) if i < left else str(v).rjust(w) for i, (v, w) in enumerate(zip(r, widths))))


def print_listed(title, items):
    """
    Print a title line, then its items comma separated on indented lines no longer than WIDTH, an item never split
    across two, or none.
    """
    print(title)
    line = " "
    for i, item in enumerate(items or ["none"]):
        item += "," if i < len(items) - 1 else ""
        if len(line) > 1 and len(line) + 1 + len(item) > WIDTH:
            print(line)
            line = " "
        line += " " + item
    print(line)


def by_status(status):
    """
    Where a status sorts in STATUSES, for sorting rows by it.
    """
    return STATUSES.index(status) if status in STATUSES else len(STATUSES)


def in_sports(sports, column="p.sport"):
    """
    A SQL condition keeping the sports, and its parameters, or nothing when every sport is kept.
    """
    if not sports:
        return "", ()
    return f" AND {column} IN ({', '.join('?' * len(sports))})", tuple(sports)


# SECTIONS

def print_overview(conn, sports):
    """
    The database's size, and the current pairs of each sport in one line.
    """
    size = os.path.getsize(DB_PATH)
    wal = DB_PATH.with_name(DB_PATH.name + "-wal")
    if wal.exists():
        size += os.path.getsize(wal)
    where, params = in_sports(sports)
    counts = query_rows(conn, f"""
        SELECT p.sport, COUNT(*) FROM pairs p WHERE p.id IN (SELECT pair_id FROM bets WHERE pair_id IS NOT NULL){where}
        GROUP BY p.sport ORDER BY COUNT(*) DESC""", params)
    total = sum(n for _, n in counts)
    print(f"database {DB_PATH}, {size / 1e6:,.0f} MB")
    print_listed(f"{total:,} pairs, last matched {short_time(first_value(conn, 'SELECT MAX(matched_at) FROM pairs'))} UTC",
                 [f"{s} {n:,}" for s, n in counts])


def print_gaps(conn, since, hours):
    """
    How often each venue's feed dropped in the window, and for how long.
    """
    gaps = query_rows(conn, """
        SELECT venue, COUNT(*), COALESCE(SUM((julianday(end_ts) - julianday(start_ts)) * 86400), 0), SUM(end_ts IS NULL)
        FROM gaps WHERE start_ts >= ? GROUP BY venue ORDER BY venue""", (since,))
    print_listed(f"\nfeed drops, last {hours} hours",
                 [f"{v} {n} ({secs:.0f}s down{f', {open_} without an end' if open_ else ''})" for v, n, secs, open_ in gaps])


def returns(capital, profit, days):
    """
    The return on capital as a percent, and scaled to a year over days held, or None for either that cannot be told.
    """
    ret = 100 * profit / capital if capital > 0 else None
    return ret, (ret * 365 / days if ret is not None and days else None)


def seconds(value):
    """
    A duration in seconds to the thousandth, as '0.001s', for a table cell.
    """
    return f"{value or 0:,.3f}s"


def in_market(market):
    """
    A SQL condition keeping one market's pairs: the futures, which have no game, or the games, matches, and races,
    which live trades once under way, and Bitcoin's 15 minute windows until 2026-10-10. Until 2026-10-05 paper traded
    them before they started too, and its trades then count with the in-play ones.
    """
    return " AND p.game_date IS NULL" if market == "futures" else " AND p.game_date IS NOT NULL"


def opportunity_rules(market):
    """
    The episodes of one market live trades, as a SQL condition on the
    episode, and its levels in words: the futures paying
    config.MIN_PAYOUT_HOURS or more out and returning config.MIN_ANNUAL_PCT
    a year at the peak, on the levels returning that, and the games,
    matches, and races under way at the peak paying within
    config.MAX_PAYOUT_HOURS, on the levels at config.LIVE_IN_PLAY_MIN_EDGE
    or more, of which no return a year is asked, whose edge stayed at
    config.MIN_EDGE or more for config.LIVE_HOLD_SECONDS, as live waits for
    it to, see LiveExecutor.hold(). The scanner counts a game's moments
    only from then, so the stretch condition leaves out only the episodes
    of before 2026-10-06, kept by the rules of then. Until 2026-10-07 live
    waited half a second, and the scanner counted a game's moments only
    from half a second, so those days show fewer. A future's moments it
    counts only once the edge has lasted config.LIVE_HOLD_SECONDS at the
    rate a year from 2026-10-07, and those of the episodes before, counted
    at once, stay as they were kept. Lasting does not make an edge real:
    two episodes of one Bitcoin window on 2026-10-05 showed 5,304$ to be
    locked in at 18.7 and 51.8 cents for one and two minutes, unlikely when
    both venues settle it on the same index, likely a book left standing on
    a market no longer trading. When that book is Polymarket US's, live,
    sending its order first, loses only an order that fills nothing. Which
    levels count, and at which moment, is the scanner's, see
    Opportunity.take_size.
    """
    if market == "futures":
        return ("live = 0 AND days_held * 24 >= ? AND annual_pct >= ?", (config.MIN_PAYOUT_HOURS, config.MIN_ANNUAL_PCT),
                f"levels returning {config.MIN_ANNUAL_PCT}%+ a year once the edge lasted {config.LIVE_HOLD_SECONDS:g}s, "
                f"paying {config.MIN_PAYOUT_HOURS}h+ out")
    return ("live = 1 AND days_held * 24 <= ? AND min_edge_seconds >= ?", (config.MAX_PAYOUT_HOURS, config.LIVE_HOLD_SECONDS),
            f"levels at {100 * config.LIVE_IN_PLAY_MIN_EDGE:.0f}c+ once the edge lasted {config.LIVE_HOLD_SECONDS:g}s, "
            f"games, matches, and races under way, paying within {config.MAX_PAYOUT_HOURS}h")


def print_market_opportunities(conn, since, hours, sports, market):
    """
    The episodes of one market within live's rules in the window: one order
    could have had config.LIVE_MIN_CONTRACTS or more on the levels at live's
    least edge or more, see opportunity_rules(). An edge on less, a sliver
    of a level, is one no trade could take. That is a tenth of a contract
    over the whole window, though live opened whole contracts until
    2026-10-07, so days before then show episodes it could not take. Live
    takes an edge in one order, and its own fill empties the levels it
    takes, so what stayed fillable through the stretch at config.MIN_EDGE,
    which these were counted by until 2026-10-05, left out the very episodes
    it traded. Episodes from before the scanner kept what one order could
    have had have none and are left out, as are ones still open, which are
    stored only once they end. On a database the live process has not yet
    brought up to it, the market says so. Capital is what buying those
    contracts would have cost with fees, and profit what they lock in. The
    annual rates weight each episode by its capital, over the days until it
    pays. How long the edge stayed at config.MIN_EDGE or more is the longest
    unbroken stretch of each episode, in seconds to the thousandth. An
    episode whose peak came while a leg's venue was in its weekly
    maintenance, see common/venues.py, is left out, as no order could trade
    it: the scanner stopped keeping those on 2026-10-08, after Kalshi's
    books froze that morning while a tennis match moved on Polymarket US,
    and 70 in-play episodes showed 7,630$ to be locked in.
    """
    cents = f"{100 * config.MIN_EDGE:.0f}c"
    where, params = in_sports(sports)
    rule, rule_params, levels = opportunity_rules(market)
    print(f"\n{market} opportunities ({config.LIVE_MIN_CONTRACTS:g}+ contracts in one order on {levels}), last {hours} hours")
    if "take_size" not in table_columns(conn, "opportunities"):
        print("  not kept yet, until the live process restarts on this code")
        return
    rows = query_rows(conn, f"""
        SELECT p.sport, p.kind, p.label, trade, 100 * peak_edge, min_edge_seconds, take_size, take_size - take_profit,
               take_profit, days_held, peak_ts, yes_venue, no_venue
        FROM opportunities o JOIN pairs p ON p.id = o.pair_id
        WHERE start_ts >= ? AND take_size >= ? AND {rule}{in_market(market)}{where}
        ORDER BY take_profit DESC""", (since, config.LIVE_MIN_CONTRACTS - 1e-9) + rule_params + params)
    rows = [r[:10] for r in rows if not any(is_maintenance(venue, r[10]) for venue in r[11:])]
    if not rows:
        print("  none")
        return

    def totals(group):
        capital, profit = sum(r[7] for r in group), sum(r[8] for r in group)
        if capital <= 0:
            return capital, profit, None, None, None
        yearly = sum(r[8] * 365 / r[9] for r in group if r[9])
        days = sum(r[7] * r[9] for r in group if r[9]) / capital
        return capital, profit, 100 * profit / capital, 100 * yearly / capital, days

    capital, profit, ret, annual, days = totals(rows)
    print(f"  {len(rows):,} episodes could have taken {capital:,.0f}$ in one order each and locked in {profit:,.2f}$")
    print(f"  {percent(ret)}% on capital, {percent(annual)}% a year, held {days or 0:,.1f} days on average")
    print(f"  at {cents} or more for {seconds(quantile([r[5] for r in rows], 0.5))} at the median, "
          f"{seconds(quantile([r[5] for r in rows], 0.9))} at the 90th percentile, {seconds(max(r[5] for r in rows))} at the longest")
    kinds = {}
    for r in rows:
        kinds.setdefault((r[0], r[1]), []).append(r)
    body = []
    for (sport, kind), group in sorted(kinds.items()):
        c, pr, r_, a, d = totals(group)
        body.append((sport, kind, len(group), seconds(quantile([r[5] for r in group], 0.5)), seconds(max(r[5] for r in group)),
                     f"{c:,.0f}", f"{pr:,.2f}", percent(r_), percent(a), f"{d or 0:,.1f}"))
    print_table(f"{market} opportunities by kind", ("sport", "kind", "episodes", f"median {cents}+", f"longest {cents}+", "capital $",
                                                  "profit $", "return %", "annual %", "avg days"), body, left=2)
    largest = []
    for sport, kind, label, trade, peak, lasted, size, cap, pr, d in rows[:LARGEST]:
        r_, a = returns(cap, pr, d)
        largest.append((label[:44], f"{peak:.1f}", seconds(lasted), contracts(size), f"{cap:,.0f}", f"{pr:,.2f}", percent(a),
                        f"{d:,.1f}" if d else "-"))
    print_table(f"{market} largest opportunities", ("bet", "peak c", f"{cents}+ for", "size", "capital $", "profit $", "annual %", "days"),
                largest)


def paid_back(legs):
    """
    The contracts each venue has already paid back by netting what open
    live trades hold, as {venue: pairs}, see database.netted(), from their
    legs as (venue, contract, its polarity, the side of the bet the leg
    holds, contracts held): a leg holds its contract's yes when its side is
    the contract's polarity, as Leg.outcome says, and its no otherwise.
    """
    return netted((venue, contract_id, contracts if side == polarity else -contracts) for venue, contract_id, polarity, side, contracts in legs)


def load_open_trades(conn, mode, market, sports):
    """
    The open trades of one mode in one market, those holding contracts with no settlement, soonest to pay first, as
    tuples for print_opened_in_window() and print_open_trades().
    """
    where, params = in_sports(sports)
    return query_rows(conn, f"""
        SELECT t.id, p.label, t.yes_held, t.no_held, t.yes_venue, t.yes_cost, t.no_venue, t.no_cost, t.profit + t.hedge_pnl,
               t.signal_ts, t.pays_at, t.yes_contract, t.yes_polarity, t.no_contract, t.no_polarity
        FROM trades t JOIN pairs p ON p.id = t.pair_id
        WHERE t.mode = ? AND t.yes_held + t.no_held > 0 AND NOT EXISTS (SELECT 1 FROM settlements s WHERE s.trade_id = t.id)
          {in_market(market)}{where}
        ORDER BY t.pays_at, t.id""", (mode,) + params)


def print_opened_in_window(rows, since, hours, mode, market):
    """
    Each open trade of rows, see load_open_trades(), opened in the window, newest first, with what it holds, its
    capital, the profit it is expected to return with its rates, and when it pays, or nothing when none was.
    """
    body = []
    for trade_id, label, yes, no, _, yes_cost, _, no_cost, pr, signal, pays, *_ in rows:
        if signal >= since:
            cap = yes_cost + no_cost
            ret, annual = returns(cap, pr, game.days_until(signal, pays))
            body.append((signal, trade_id, label[:44], short_time(signal)[:16], f"{contracts(yes)}/{contracts(no)}", f"{cap:,.2f}",
                         f"{pr:,.2f}", percent(ret), percent(annual), pays[:10]))
    if body:
        body.sort(reverse=True)
        print_table(f"{mode} {market} open trades opened in the last {hours} hours, newest first",
                    ("trade", "bet", "opened UTC", "held yes/no", "capital $", "profit $", "return %", "annual %", "pays"),
                    [r[1:] for r in body], left=3)


def print_open_trades(rows, mode, market, now):
    """
    The open trades of rows, see load_open_trades(), in a few lines. Capital is what the contracts still held cost
    with fees, and the expected profit what the trades locked in, a dollar for each pair held less what it cost, with
    what flattening made or lost. A contract held without its other side, which the held column shows, is counted at
    its cost, as if it broke even. Live's capital then leaves out the dollar a pair the venues have already paid back
    by netting opposite holdings of a contract, see paid_back(), on each venue too, since that money is back in cash;
    paper's books do not net. The return on capital and its rate a year are on what is still held, the rate scaling
    each trade's return over the days from the trade to its payout, weighted by capital. They resolve from the first
    payout, which may be past and waiting on a venue, to the last, and the average weighted by capital is when the
    money comes back on average.
    """
    if not rows:
        print(f"\n{mode} {market} open trades: none")
        return
    capital, profit, yearly, when, venues, legs = 0.0, 0.0, 0.0, 0.0, {}, []
    for trade_id, label, yes, no, yes_venue, yes_cost, no_venue, no_cost, pr, signal, pays, yes_contract, yes_polarity, no_contract, no_polarity in rows:
        cap = yes_cost + no_cost
        capital, profit, yearly = capital + cap, profit + pr, yearly + pr * 365 / game.days_until(signal, pays)
        when += cap * epoch(pays)
        for venue, cost in ((yes_venue, yes_cost), (no_venue, no_cost)):
            venues[venue] = venues.get(venue, 0.0) + cost
        legs += [(yes_venue, yes_contract, yes_polarity, "yes", yes), (no_venue, no_contract, no_polarity, "no", no)]
    opened = []
    for name, window in OPENED_HOURS.items():
        group = [r for r in rows if days_between(r[9], now) * 24 <= window]
        opened.append((f"opened in the last {name}", f"{len(group):,}", f"{sum(r[5] + r[7] for r in group):,.2f}$"))
    widths = [max(len(o[i]) for o in opened) for i in range(3)]
    paid = paid_back(legs) if mode == "live" else {}
    still = capital - sum(paid.values())        # What the trades still hold, once netting paid its pairs back.
    ret, annual = (100 * profit / still, 100 * yearly / still) if still > 0 else (None, None)
    held = ", ".join(f"{venue} {amount - paid.get(venue, 0.0):,.2f}$" for venue, amount in sorted(venues.items()))
    print(f"\n{mode} {market} open trades: {len(rows):,}")
    for text, count, cap in opened:
        print(f"  {text:<{widths[0]}}  {count:>{widths[1]}} using {cap:>{widths[2]}}")
    if paid:
        print(f"  capital {capital:,.2f}$, {sum(paid.values()):,.2f}$ of it already paid back by netting, so {still:,.2f}$ still held on {held}")
    else:
        print(f"  capital {capital:,.2f}$, held on {held}")
    print(f"  expected to return {profit:,.2f}$ in profit, {percent(ret)}% on capital, {percent(annual)}% a year")
    print(f"  resolving first {rows[0][10][:10]}, on average {at_seconds(when / capital)[:10] if capital > 0 else '-'}, last {rows[-1][10][:10]}")


def beyond_profit(conn, since, mode, which, where, params):
    """
    What the trades of one mode that settled in the window and meet which,
    a condition on the trade t and its settlement s, were paid beyond their
    locked-in profit, as (trades, dollars): what their legs paid less what
    the contracts cost and the profit, see exposure_settled() and
    both_paid().
    """
    return query_rows(conn, f"""
        SELECT COUNT(*), COALESCE(SUM({PAID} - t.yes_cost - t.no_cost - t.profit), 0)
        FROM settlements s JOIN trades t ON t.id = s.trade_id JOIN pairs p ON p.id = t.pair_id
        WHERE s.mode = ? AND s.settled_at >= ? AND {which}{where}""", (mode, since) + params)[0]


def exposure_settled(conn, since, mode, where, params):
    """
    What the exposed contracts of one mode's trades made or lost at
    settlement in the window, as (trades, dollars), on the trades that
    settled in it holding more on one side than the other. A trade's
    locked-in profit is what its matched contracts pay, a dollar a pair
    less what they cost, and the costs of what it holds leave out what
    flattening sold, whose result is its hedge, so what it realized beyond
    the profit is what its unmatched contracts paid less what they cost.
    A balanced trade realizes its profit to the cent, as all 1,891 balanced
    live trades settled by 2026-10-11 did, while the exposed ones had made
    -3.93$ on futures and 0.33$ in play, which until then showed only in
    the legs settled by venue, a table dropped on 2026-10-12. A leg that
    held nothing has no payout and cost nothing.
    """
    return beyond_profit(conn, since, mode, "t.yes_held != t.no_held", where, params)


def both_paid(conn, since, mode, where, params):
    """
    What the balanced trades of one mode that settled in the window were
    paid beyond their locked-in profit, as (trades, dollars): a nested
    trade's two legs both pay when the number lands between its lines, a
    second dollar a pair, see catalog/nest.py, where any other balanced
    trade is paid one dollar a pair, its profit to the cent.
    """
    return beyond_profit(conn, since, mode, f"t.yes_held = t.no_held AND {PAID} > t.yes_held + 0.005", where, params)


def print_market_trades(conn, since, hours, mode, market, sports, now):
    """
    One mode's trades in one market: how many, the net of the window, by
    kind in it, the largest, each open trade opened in it, the trades by
    outcome, and last the open trades in a few lines. The net is what the
    trades opened in the window locked in and made or lost flattening, the
    tables' net, with what exposed contracts made or lost at settlement in
    it, see exposure_settled(), so each trade counts its profit when it
    opens and its exposure when it settles, and with a nested trade's
    second dollar a pair when both its legs paid, see both_paid(). Until
    2026-10-12 live's orders followed, by venue and by how long the book
    had sent nothing, and the legs settled in the window by venue came
    before the open trades.
    """
    where, params = in_sports(sports)
    where = in_market(market) + where
    total = first_value(conn, f"SELECT COUNT(*) FROM trades t JOIN pairs p ON p.id = t.pair_id WHERE t.mode = ?{where}", (mode,) + params)
    window = f"FROM trades t JOIN pairs p ON p.id = t.pair_id WHERE t.mode = ? AND signal_ts >= ?{where}"     # The trades of the window.
    args = (mode, since) + params
    recent, locked = query_rows(conn, f"SELECT COUNT(*), COALESCE(SUM(profit + hedge_pnl), 0) {window}", args)[0]
    exposed, exposure = exposure_settled(conn, since, mode, where, params)
    doubled, second = both_paid(conn, since, mode, where, params)
    print(f"\n{mode} {market} trades: {total:,} in all, {recent:,} in the last {hours} hours")
    if recent or exposed or doubled:
        print(f"  net {dollars(locked + exposure + second)}$: {dollars(locked)}$ locked in and hedged on the trades opened, "
              f"{dollars(exposure)}$ on the exposed contracts of {exposed:,} trades settled"
              + (f", {dollars(second)}$ more on {doubled:,} nested trades both of whose legs paid" if doubled else ""))
    open_rows = load_open_trades(conn, mode, market, sports)
    if recent:
        body = query_rows(conn, f"""
            SELECT p.sport, p.kind, COUNT(*), ROUND(AVG(100 * edge), 1), ROUND(100.0 * SUM(matched) / SUM(quantity), 0),
                   ROUND(SUM(profit + hedge_pnl), 2), ROUND(AVG(julianday(pays_at) - julianday(signal_ts)), 1)
            {window} GROUP BY p.sport, p.kind ORDER BY p.sport, p.kind""", args)
        print_table(f"{mode} {market} by kind, last {hours} hours", ("sport", "kind", "trades", "avg edge c", "fill %", "net $", "avg days held"), body)
        body = query_rows(conn, f"""
            SELECT t.id, p.label, t.signal_ts, t.status, t.matched, t.quantity, t.yes_cost + t.no_cost, t.profit, t.profit + t.hedge_pnl, t.pays_at
            {window} ORDER BY t.yes_cost + t.no_cost DESC, t.id LIMIT ?""", args + (LARGEST,))
        print_table(f"{mode} {market} largest trades, last {hours} hours",
                    ("trade", "bet", "opened UTC", "status", "matched", "wanted", "capital $", "locked in $", "net $", "pays"),
                    [(i, label[:44], short_time(signal)[:16], status, contracts(m), contracts(q), f"{cap:,.2f}", f"{pr:,.2f}", f"{net:,.2f}",
                      pays[:10]) for i, label, signal, status, m, q, cap, pr, net, pays in body], left=4)
        print_opened_in_window(open_rows, since, hours, mode, market)
        body = query_rows(conn, f"""
            SELECT status, COUNT(*), SUM(quantity), SUM(matched), ROUND(SUM(profit), 2), ROUND(SUM(hedge_pnl), 2), ROUND(SUM(profit + hedge_pnl), 2)
            {window} GROUP BY status""", args)
        body.sort(key=lambda r: by_status(r[0]))
        print_table(f"{mode} {market} by outcome, last {hours} hours", ("status", "trades", "wanted", "matched", "locked in $", "hedges $", "net $"),
                    [(s, n, contracts(q), contracts(m), *rest) for s, n, q, m, *rest in body])
    print_open_trades(open_rows, mode, market, now)


def print_totals(conn, mode, markets, sports):
    """
    One mode's trades since its first, settled and open, for each market
    apart: how many settled and how many are open, the capital they put in,
    what they returned or are expected to, and that as a share of the
    capital and a rate a year. A trade's capital is what the contracts it
    holds cost with fees, the part flattening sold back being in its hedge.
    A settled trade returns what its legs paid less that capital, with its
    hedge, which counts what its exposed contracts made or lost; an open
    one is expected to return what it locked in with its hedge, a contract
    held without its other side counted as breaking even, as with the open
    trades. A trade flattened to nothing put in no capital but returns its
    hedge. The capital is each trade's own, not netted: netting pays back
    pairs held at the same time, while this adds up what every trade put in
    over the whole span. The rate a year is the return on each dollar for
    each day it was out: the return over the sum of each trade's capital
    times the days it is held, from the trade to its settlement or, still
    open, to its payout, an hour at the least, times 365. A trade still in
    flight is left out.
    """
    where, params = in_sports(sports)
    body, first = [], None
    for market in markets:
        rows = query_rows(conn, f"""
            SELECT t.yes_cost + t.no_cost, t.profit + t.hedge_pnl, t.hedge_pnl, t.yes_held + t.no_held, t.signal_ts, t.pays_at,
                   s.settled_at, {PAID}
            FROM trades t JOIN pairs p ON p.id = t.pair_id LEFT JOIN settlements s ON s.trade_id = t.id
            WHERE t.mode = ? AND t.status != 'sent'{in_market(market)}{where}""", (mode,) + params)
        settled, still_open, capital, returned, dollar_days = 0, 0, 0.0, 0.0, 0.0
        for cap, expected, hedge, held, signal, pays, settled_at, payouts in rows:
            first = min(first or signal, signal)
            if settled_at is not None:
                settled, returned, until = settled + 1, returned + payouts - cap + hedge, settled_at
            elif held > 0:
                still_open, returned, until = still_open + 1, returned + expected, pays
            else:
                returned, until = returned + hedge, signal
            capital += cap
            dollar_days += cap * game.days_until(signal, until)
        ret = 100 * returned / capital if capital > 0 else None
        annual = 100 * returned * 365 / dollar_days if dollar_days > 0 else None
        body.append((market, f"{settled:,}", f"{still_open:,}", dollars(capital), dollars(returned), percent(ret), percent(annual)))
    if first is None:
        print(f"\n{mode} totals since the first trade: none")
        return
    print_table(f"{mode} totals since the first trade, on {first[:10]}, settled and open",
                ("market", "settled", "open", "capital $", "return $", "return %", "annual %"), body)


def print_paper_money(conn):
    """
    The paper balances from the ledger. Live money is on the venues, see print_live_money().
    """
    balances = query_rows(conn, "SELECT venue, ROUND(balance, 2) FROM ledger WHERE id IN (SELECT MAX(id) FROM ledger GROUP BY venue) ORDER BY venue")
    if balances:
        print("\npaper money from the ledger")
        for venue, amount in balances:
            print(f"  {venue} {amount:,.2f}$")


def print_market(conn, since, hours, sports, market, modes=("live",), now=None):
    """
    Everything on one market under a heading of its own: its opportunities,
    then the trades of each mode asked for, apart, since paper and live
    money never mix.
    """
    now = now or now_iso()
    print(f"\n===== {market} =====")
    print_market_opportunities(conn, since, hours, sports, market)
    for mode in modes:
        print_market_trades(conn, since, hours, mode, market, sports, now)


def read_live_balances(readers=None):
    """
    Each venue's live balance as {venue: (dollars, {exchange shard: dollars}), or the reason it could not be read}.
    readers maps a venue to a function returning those; by default the live process's own, which need the keys in
    data/. They are loaded only here, so the rest of the report runs without the venue clients.
    """
    if readers is None:
        try:
            from engine.components.money.live import READERS as readers
        except Exception as e:
            return {"live balances": f"not read, the venue clients did not load ({e!r})"}
    out = {}
    for venue, read in readers.items():
        try:
            out[venue] = read()
        except Exception as e:
            out[venue] = f"not read ({str(e)[:120]})"
    return out


def print_live_money(balances):
    """
    The live balances on the venues, read now, with the exchange shards live trading uses, config.LIVE_SHARDS, and
    any other holding money. What open live trades hold is with the live trades.
    """
    def money(venue, reading):
        if isinstance(reading, str):
            return f"{venue} {reading}"
        dollars, shards = reading
        parts = [f"shard {s} {a:,.2f}$" for s, a in sorted(shards.items()) if a or s in config.LIVE_SHARDS.get(venue, ())]
        return f"{venue} {dollars:,.2f}$" + (f" ({', '.join(parts)})" if parts else "")

    print("\nlive money on the venues, read now")
    for venue, reading in balances.items():
        print(f"  {money(venue, reading)}")


# MAIN

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=f"Summarize the {NAME} database.")
    ap.add_argument("--hours", type=int, default=24, help="size of the recent window for feed drops, opportunities, and trades")
    ap.add_argument("--mode", choices=sorted(MODES), default="all",
                    help="the trades to show: live, paper, or all, the default")
    ap.add_argument("--market", choices=sorted(MARKET_CHOICES), default="all",
                    help="the opportunities and trades to show: the futures, the games in-play, or all, the default")
    ap.add_argument("--sport", default="", help="the sports to show, comma separated, every sport when left out")
    ap.add_argument("--no-live", action="store_true", help="leave out the live balances, which are read from the venues")
    args = ap.parse_args()
    now = now_iso()
    since = shift(now, hours=-args.hours)
    sports = tuple(s.strip() for s in args.sport.split(",") if s.strip())
    conn = read_only()
    print_overview(conn, sports)
    print_gaps(conn, since, args.hours)
    for market in MARKET_CHOICES[args.market]:
        print_market(conn, since, args.hours, sports, market, MODES[args.mode], now)
    for mode in MODES[args.mode]:
        print_totals(conn, mode, MARKET_CHOICES[args.market], sports)
    if "paper" in MODES[args.mode]:
        print_paper_money(conn)
    if "live" in MODES[args.mode] and not args.no_live:
        print_live_money(read_live_balances())
