"""
Report how far behind the venues our books ran, and how long our orders took, over a stretch such as a game.

Four sections, all from what the live process already keeps:

- Feeds, from the status line record.log gets every minute: for each venue
  how many book changes arrived a minute, and how far behind the venue they
  reached us, median and 90th percentile, and how much of that was ours.
  The busiest minutes are compared with the quiet ones, which shows whether
  a busy game slows a feed.
- Orders, from the orders table: for each venue and purpose how many filled,
  and the trip there and back, split at the time the venue put on its answer.
- Edges, from the opportunities table: how long the episodes lasted, those
  worth the minimum edge apart, for one sport or all.
- Edges held back, from the executors' summary lines: pairs whose edge
  waited for a Polymarket US book to catch up.

The log gives only the time of day, so each line's date is worked out by
walking back from the end. Reads only, so it is safe to run while the live
process runs, and like summary.py it sets its own import path, so it runs
from any folder.

Run with:
    python3 src/tools/latency_report.py
    python3 src/tools/latency_report.py --since 2026-09-29T17:30 --until 2026-09-29T21:30 --sport mlb
    python3 src/tools/latency_report.py --hours 3 --every 10
"""

import argparse
import json
import re
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))     # src, so the script runs from any folder.
from api import orders
from common.paths import DATA_DIR
from common.stats import quantile
from common.venues import VENUES
from db.database import read_only
from engine.helper import config

LOG_PATH = DATA_DIR / "record.log"
TIME_OF_DAY = re.compile(r"^(\d\d):(\d\d):(\d\d) ")
# One venue's part of the status line: 'polymarket_us 59123 (last 0s ago, 0 gaps, 81 ms behind the venue, 145 at 90%, 0 from us)'.
STATUS = re.compile(r"(kalshi|polymarket_us) (\d+) \(last [^,]*, \d+ gaps(?:, (\d+) ms behind the venue, (\d+) at 90%(?:, (\d+) from us)?)?\)")
WAITED = re.compile(r"^\S+ (paper|live): .*; (\d+) pairs' edges waited for a book to catch up")
LASTED = [(0.1, "under 100 ms"), (0.2, "100-200 ms"), (0.5, "200-500 ms"), (2.0, "0.5-2 s"), (float("inf"), "over 2 s")]


def ms(value):
    """
    Milliseconds in words for a table cell, or a dash.
    """
    return "-" if value is None else f"{value:.0f}"


def parse_time(text):
    """
    A --since or --until value as a UTC datetime: an ISO time, UTC unless it says otherwise.
    """
    when = datetime.fromisoformat(text)
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def dated(lines, now):
    """
    The log lines that start with a time of day, as (UTC datetime, line),
    oldest first. Their dates are worked out by walking back from now: a
    line whose time of day is later than that of the line after it was
    written the day before.
    """
    out = []
    day, later = now.date(), now.time()
    for line in reversed(lines):
        m = TIME_OF_DAY.match(line)
        if not m:
            continue
        t = time(*map(int, m.groups()))
        if t > later:
            day -= timedelta(days=1)
        later = t
        out.append((datetime.combine(day, t, tzinfo=timezone.utc), line))
    out.reverse()
    return out


def status_minutes(lines):
    """
    The status lines as one row a minute, {when, venue: (updates that
    minute, median ms, 90th percentile ms, ours ms)}, from dated lines.
    Updates are counted since the status line before, from the running
    count each line gives. A restart starts the count over.
    """
    rows, last = [], {}
    for when, line in lines:
        if " tracking " not in line:
            continue
        row = {"when": when}
        for venue, count, median, p90, ours in STATUS.findall(line):
            count = int(count)
            before = last.get(venue)
            row[venue] = (count - before if before is not None and count >= before else count if before is not None else None,
                          int(median) if median else None, int(p90) if p90 else None, int(ours) if ours else None)
            last[venue] = count
        rows.append(row)
    return rows


# SECTIONS

def print_feeds(rows, every):
    """
    The feed delays every few minutes, then the busiest quarter of minutes against the quietest half.
    """
    print(f"\nfeeds, every {every} minutes: book changes a minute, and ms behind the venue at the median and 90th percentile")
    header = "  time    " + "".join(f"{venue:>30}" for venue in VENUES)
    print(header)
    buckets = {}
    for row in rows:
        start = row["when"].replace(minute=row["when"].minute - row["when"].minute % every, second=0)
        buckets.setdefault(start, []).append(row)
    for start, group in sorted(buckets.items()):
        cells = []
        for venue in VENUES:
            seen = [r[venue] for r in group if r.get(venue) and r[venue][0] is not None]
            rate = sum(s[0] for s in seen) / len(seen) if seen else None
            median = quantile([s[1] for s in seen if s[1] is not None], 0.5)
            worst = max((s[2] for s in seen if s[2] is not None), default=None)
            cells.append(f"{'-' if rate is None else f'{rate:,.0f}':>12} {ms(median):>7} {ms(worst):>7} ms")
        print(f"  {start:%H:%M}   " + "".join(f"{c:>30}" for c in cells))
    for venue in VENUES:
        seen = sorted((r[venue] for r in rows if r.get(venue) and r[venue][0] is not None and r[venue][1] is not None), key=lambda s: s[0])
        if len(seen) < 8:
            continue
        quiet, busy = seen[:len(seen) // 2], seen[-max(1, len(seen) // 4):]
        print(f"  {venue}: busiest quarter of minutes, {quantile([s[0] for s in busy], 0.5):,} changes a minute, "
              f"{ms(quantile([s[1] for s in busy], 0.5))} ms at the median and {ms(quantile([s[2] for s in busy], 0.5))} at the 90th; "
              f"quietest half, {quantile([s[0] for s in quiet], 0.5):,} a minute, {ms(quantile([s[1] for s in quiet], 0.5))} and "
              f"{ms(quantile([s[2] for s in quiet], 0.5))} ms; ours at most {ms(max((s[3] or 0) for s in seen))} ms")


def venue_time(venue, response):
    """
    When the venue handled an order, in seconds since 1970, from its stored answer, or None when the answer does not say, see orders.venue_time().
    """
    try:
        answer = json.loads(response or "{}")
    except ValueError:
        return None
    return orders.venue_time(venue, answer) if isinstance(answer, dict) else None


def print_orders(conn, since, until):
    """
    Live orders per venue and purpose: how many filled, and the trip there and back.
    """
    rows = conn.execute("SELECT venue, purpose, status, sent_at, answered_at, latency_ms, response FROM orders "
                        "WHERE sent_at >= ? AND sent_at < ? ORDER BY sent_at", (since.isoformat(), until.isoformat())).fetchall()
    print("\norders: filled of sent, and ms from sending to the venue handling it and back, median and 90th percentile")
    if not rows:
        print("  none")
        return
    groups = {}
    for venue, purpose, status, sent, answered, latency, response in rows:
        g = groups.setdefault((venue, purpose), {"sent": 0, "filled": 0, "there": [], "back": [], "total": []})
        g["sent"] += 1
        g["filled"] += status in ("filled", "partial")
        if latency is not None:
            g["total"].append(latency)
        handled = venue_time(venue, response)
        if handled and sent and answered:
            g["there"].append(1000 * (handled - datetime.fromisoformat(sent).timestamp()))
            g["back"].append(1000 * (datetime.fromisoformat(answered).timestamp() - handled))
    for (venue, purpose), g in sorted(groups.items()):
        print(f"  {venue:14} {purpose:8} {g['filled']:3} of {g['sent']:3} filled; there {ms(quantile(g['there'], 0.5))} and "
              f"{ms(quantile(g['there'], 0.9))} ms, back {ms(quantile(g['back'], 0.5))} and {ms(quantile(g['back'], 0.9))} ms, "
              f"whole trip {ms(quantile(g['total'], 0.5))} ms")


def print_edges(conn, since, until, sport, edge):
    """
    How long the episodes lasted, all of them and those worth the minimum edge.
    """
    rows = conn.execute("SELECT o.seconds, o.peak_edge, o.peak_profit FROM opportunities o JOIN pairs p ON p.id = o.pair_id "
                        "WHERE o.start_ts >= ? AND o.start_ts < ? AND (? IS NULL OR p.sport = ?)",
                        (since.isoformat(), until.isoformat(), sport, sport)).fetchall()
    print(f"\nedges{f' in {sport}' if sport else ''}: episodes by how long they lasted in our view, and those of {edge * 100:.0f}c or more")
    if not rows:
        print("  none")
        return
    for upper, words in LASTED:
        lower = next((u for u, _ in reversed(LASTED) if u < upper), 0.0)
        inside = [r for r in rows if lower <= r[0] < upper]
        worth = [r for r in inside if r[1] >= edge]
        print(f"  {words:13} {len(inside):6} episodes, {len(worth):5} of {edge * 100:.0f}c or more worth {sum(r[2] for r in worth):9,.2f}$ at their peaks")


def print_waits(lines):
    """
    Pairs whose edge waited for a book to catch up, as the executors' summary lines count them.
    """
    waited = {}
    for _, line in lines:
        m = WAITED.match(line)
        if m:
            waited[m.group(1)] = waited.get(m.group(1), 0) + int(m.group(2))
    print("\nedges held back for a Polymarket US book to catch up: "
          + (", ".join(f"{mode} {n} pairs" for mode, n in sorted(waited.items())) if waited else "none"))


# MAIN

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Report feed delays, order trips, and edge lengths over a stretch of time.")
    ap.add_argument("--hours", type=float, default=4, help="the stretch ending now, when --since is not given")
    ap.add_argument("--since", help="start of the stretch, an ISO time in UTC, for example 2026-09-29T17:30")
    ap.add_argument("--until", help="end of the stretch, an ISO time in UTC, now unless given")
    ap.add_argument("--every", type=int, default=5, help="minutes per row of the feed table")
    ap.add_argument("--sport", help="the sport whose edges to count, all unless given")
    ap.add_argument("--edge", type=float, default=config.MIN_EDGE, help="the edge worth trading, in dollars, config.MIN_EDGE by default")
    args = ap.parse_args()
    now = datetime.now(timezone.utc)
    until = parse_time(args.until) if args.until else now
    since = parse_time(args.since) if args.since else until - timedelta(hours=args.hours)
    print(f"from {since:%Y-%m-%d %H:%M} to {until:%Y-%m-%d %H:%M} UTC")
    lines = [(when, line) for when, line in dated(LOG_PATH.read_text(errors="replace").splitlines(), now) if since <= when < until]
    print_feeds(status_minutes(lines), args.every)
    conn = read_only()
    print_orders(conn, since, until)
    print_edges(conn, since, until, args.sport, args.edge)
    print_waits(lines)
