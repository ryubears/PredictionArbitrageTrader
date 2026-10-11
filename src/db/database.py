"""
SQLite storage for SportsArb.

One database file holds every table, which makes it easy to open in any
SQLite browser. The tables follow the pipeline in order.

    contracts      what each venue lists, written by fetch.py
    bets           each contract restated in venue neutral terms, by classify.py
    pairs          the contracts on both venues for one bet, by match.py
    gaps           stretches when a venue's feed was down, by market/record.py
    opportunities  every episode the live scanner saw, by market/scan.py
    trades         every trade the executors made, paper or live, by trading/
    settlements    how each trade's legs paid out, by money/settle.py
    orders         every real order the live executor sent, by trading/live.py
    ledger         every paper cash movement per venue, by money/paper.py
    alerts         everything the live process emailed a human, by trading/notify.py

Trades and settlements carry a mode, 'paper' or 'live', and every read of
open trades is for one mode, so paper and live trades never mix.

The tables themselves are in schema.sql, and the steps that bring older
databases up to them in migrations.py. This file holds the reads and writes.
"""

import hashlib
import sqlite3
from dataclasses import asdict, fields
from common import jsonutil
from common.paths import DATA_DIR
from db import migrations, schema
from db.models import Alert, Bet, Gap, Ledger, Opportunity, Order, Settlement, Trade
from pathlib import Path

DB_PATH = DATA_DIR / "sportsarb.sqlite"

# Model fields that are read from other tables or set by the database, so they are never written.
NOT_STORED = {"id", "label", "starts_at"}
# What a Trade's update and settlement change, in the order they happen.
TRADE_FILLS = ["yes_filled", "yes_cost", "yes_latency_ms", "yes_fill_ts", "no_filled", "no_cost", "no_latency_ms", "no_fill_ts",
               "yes_held", "no_held", "matched", "profit", "hedge", "hedge_pnl", "status"]
# What the venue's answer to an Order sets.
ORDER_ANSWER = ["status", "venue_order_id", "answered_at", "latency_ms", "filled", "dollars", "fees", "note", "response"]


def columns(model):
    """
    The columns a model's rows are written to: its fields, less the ones never stored.
    """
    return [f.name for f in fields(model) if f.name not in NOT_STORED]


def insert_sql(table, model, verb="INSERT"):
    """
    An insert of every stored field of a model, with a named parameter per column.
    """
    names = columns(model)
    return f"{verb} INTO {table} ({', '.join(names)}) VALUES ({', '.join(':' + n for n in names)})"


def update_sql(table, names):
    """
    An update of the named columns of one row, found by its id.
    """
    return f"UPDATE {table} SET {', '.join(f'{n} = :{n}' for n in names)} WHERE id = :id"


# CONNECTION

def connect(db_path=None):
    """
    Open the database, creating the file and tables if needed. Uses DB_PATH unless a path is given.
    """
    db_path = Path(db_path or DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # A long busy timeout lets the recorder and the hourly catalog job share the file.
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    # Write ahead logging lets readers query while the live process writes.
    conn.execute("PRAGMA journal_mode=WAL")
    fresh = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'").fetchone() is None
    schema.create(conn)
    migrations.migrate(conn, fresh)
    return conn


def read_only(db_path=None):
    """
    Open the database to read only, as the tools do, which is safe while the
    live process writes. Rows are plain tuples. Uses DB_PATH unless a path is given.
    """
    return sqlite3.connect(f"file:{Path(db_path or DB_PATH)}?mode=ro", uri=True, timeout=30)


# CONTRACTS

def upsert_contracts(conn, contracts, fetched_at):
    """
    Insert new contracts or refresh existing ones. first_seen is kept as is.
    """
    rows = []
    for c in contracts:
        rows.append((
            c.venue, c.contract_id, c.market_id, c.event_id, c.series_id, c.sport,
            c.event_title, c.title, c.outcome, c.market_type, c.line, c.rules,
            c.start_time, c.close_time, jsonutil.dump(c.fee_info), fetched_at, fetched_at,
        ))
    conn.executemany("""
        INSERT INTO contracts (
            venue, contract_id, market_id, event_id, series_id, sport,
            event_title, title, outcome, market_type, line, rules,
            start_time, close_time, fee_info, first_seen, last_seen
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT (venue, contract_id) DO UPDATE SET
            market_id = excluded.market_id,
            event_id = excluded.event_id,
            series_id = excluded.series_id,
            sport = excluded.sport,
            event_title = excluded.event_title,
            title = excluded.title,
            outcome = excluded.outcome,
            market_type = excluded.market_type,
            line = excluded.line,
            rules = excluded.rules,
            start_time = excluded.start_time,
            close_time = excluded.close_time,
            fee_info = excluded.fee_info,
            last_seen = excluded.last_seen
    """, rows)
    conn.commit()


def load_contracts(conn, sport=None, venue=None):
    """
    Return contract rows as dicts, filtered by sport and venue when given.
    """
    sql = "SELECT * FROM contracts WHERE 1=1"
    params = []
    if sport:
        sql += " AND sport = ?"
        params.append(sport)
    if venue:
        sql += " AND venue = ?"
        params.append(venue)
    return [dict(r) for r in conn.execute(sql, params)]


def load_fee_infos(conn, sport):
    """
    Return {(venue, contract_id): fee_info} with the fee schedule currently stored for every contract of a sport.
    """
    return {(venue, cid): jsonutil.parse(fee_info, {})
            for venue, cid, fee_info in conn.execute("SELECT venue, contract_id, fee_info FROM contracts WHERE sport = ?", (sport,))}


def event_ids(conn, venue, contract_ids):
    """
    Return {contract_id: event_id} for the contracts of one venue.
    """
    return {cid: event for cid, event in conn.execute(
        f"SELECT contract_id, event_id FROM contracts WHERE venue = ? AND contract_id IN ({','.join('?' * len(contract_ids))})",
        (venue, *contract_ids))} if contract_ids else {}


# Every paired game with its kickoff, the latest start time any of its contracts gives, which is Polymarket US's since
# Kalshi gives none.
GAMES = """
    SELECT p.sport, p.game_date, p.team_a, p.team_b, MAX(c.start_time) AS kickoff
    FROM pairs p JOIN bets b ON b.pair_id = p.id JOIN contracts c ON c.venue = b.venue AND c.contract_id = b.contract_id
    WHERE p.game_date IS NOT NULL GROUP BY 1, 2, 3, 4"""


def load_recording_targets(conn, sport, now, horizon, venues, game_started_after):
    """
    The contracts to record right now, as {venue: [contract_id, ...]}: every
    contract in a pair on a game no later than the horizon's date, for as
    long as it can trade. A game's contracts on both venues are recorded
    while the game may still be in play, meaning it kicked off after
    game_started_after, whatever their close times say, with the kickoff
    from GAMES. Polymarket US leaves a game's contracts open two
    weeks after it, and Kalshi's close time is its guess at the final
    whistle, three hours after kickoff, which nearly every college game and
    most NFL games outlast. A contract with no game, or on a game no
    contract gives the kickoff of, is recorded until its close time.
    """
    targets = {}
    for venue in venues:
        rows = conn.execute(f"""
            WITH games AS ({GAMES})
            SELECT c.contract_id FROM contracts c
            JOIN bets b ON b.venue = c.venue AND b.contract_id = c.contract_id
            JOIN pairs p ON p.id = b.pair_id
            LEFT JOIN games g ON g.sport = p.sport AND g.game_date = p.game_date AND g.team_a = p.team_a AND g.team_b = p.team_b
            WHERE c.venue = ? AND c.sport = ?
              AND (g.kickoff > ? OR (g.kickoff IS NULL AND (c.close_time IS NULL OR c.close_time > ?)))
              AND (b.game_date IS NULL OR b.game_date <= ?)
        """, (venue, sport, game_started_after, now, horizon[:10]))
        targets[venue] = [r[0] for r in rows]
    return targets


# BETS

def replace_bets(conn, sport, bets):
    """
    Drop every bet belonging to the sport's contracts, then insert the new ones.
    """
    conn.execute("""
        DELETE FROM bets WHERE (venue, contract_id) IN
            (SELECT venue, contract_id FROM contracts WHERE sport = ?)
    """, (sport,))
    conn.executemany(insert_sql("bets", Bet), [asdict(b) for b in bets])
    conn.commit()


def load_bets(conn, sport, venue=None):
    """
    Return bet rows as dicts for one sport, joined with a few contract columns.
    """
    sql = """
        SELECT b.*, c.title, c.outcome, c.event_id, c.series_id, c.close_time, c.start_time
        FROM bets b JOIN contracts c USING (venue, contract_id)
        WHERE c.sport = ?
    """
    params = [sport]
    if venue:
        sql += " AND b.venue = ?"
        params.append(venue)
    return [dict(r) for r in conn.execute(sql, params)]


# PAIRS

def replace_pairs(conn, sport, pairs, matched_at):
    """
    Store the sport's pairs and point each member's bet row at its pair.
    A pair that was stored before keeps its id, found by its label, and
    each Pair and member Bet gets its id set. Pairs that no bet points at
    any more stay in the table, they are just no longer current.
    """
    conn.execute("""
        UPDATE bets SET pair_id = NULL WHERE (venue, contract_id) IN
            (SELECT venue, contract_id FROM contracts WHERE sport = ?)
    """, (sport,))
    for p in pairs:
        conn.execute("""
            INSERT INTO pairs (sport, label, kind, season, game_date, team_a, team_b, subject, line, venues, contracts, flags, matched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (label) DO UPDATE SET kind = excluded.kind, season = excluded.season, game_date = excluded.game_date,
                team_a = excluded.team_a, team_b = excluded.team_b, subject = excluded.subject, line = excluded.line,
                venues = excluded.venues, contracts = excluded.contracts, flags = excluded.flags, matched_at = excluded.matched_at
        """, (p.sport, p.label, p.kind, p.season, p.game_date, p.team_a, p.team_b, p.subject, p.line,
              ",".join(p.venues), len(p.members), jsonutil.dump(p.flags), matched_at))
        p.id = conn.execute("SELECT id FROM pairs WHERE label = ?", (p.label,)).fetchone()[0]
        for m in p.members:
            m.pair_id = p.id
    conn.executemany("UPDATE bets SET pair_id = ? WHERE venue = ? AND contract_id = ?",
                     [(p.id, m.venue, m.contract_id) for p in pairs for m in p.members])
    conn.commit()


def load_pairs(conn, sport):
    """
    Return {id: pair row dict with a 'members' list of bet row dicts} for
    the current pairs of one sport, the ones with bets pointing at them.
    Each member carries a digest of its contract's rules, or None without
    any, so two contracts that settle alike can be told apart from two
    that do not without holding every rule's text, see pricing.same_rules().
    """
    members = {}
    for r in conn.execute("""
        SELECT b.*, c.event_id, c.start_time, c.close_time, c.rules FROM bets b JOIN contracts c USING (venue, contract_id)
        WHERE c.sport = ? AND b.pair_id IS NOT NULL""", (sport,)):
        member = dict(r)
        rules = member.pop("rules")
        member["rules_digest"] = hashlib.sha256(rules.encode()).hexdigest() if rules else None
        members.setdefault(r["pair_id"], []).append(member)
    return {r["id"]: dict(r, members=members[r["id"]]) for r in conn.execute("SELECT * FROM pairs") if r["id"] in members}


# GAPS

def insert_gap(conn, gap):
    """
    Record a stretch when a venue's feed was down.
    """
    conn.execute(insert_sql("gaps", Gap, "INSERT OR REPLACE"), asdict(gap))
    conn.commit()


def load_gaps(conn, venue, since=None):
    """
    Return a venue's Gaps in time order, optionally only those starting at or after since.
    """
    sql = "SELECT venue, start_ts, end_ts FROM gaps WHERE venue = ?"
    params = [venue]
    if since:
        sql += " AND start_ts >= ?"
        params.append(since)
    return [Gap(*row) for row in conn.execute(sql + " ORDER BY start_ts", params)]


# OPPORTUNITIES

def insert_opportunities(conn, opportunities):
    """
    Append Opportunities.
    """
    conn.executemany(insert_sql("opportunities", Opportunity), [asdict(o) for o in opportunities])
    conn.commit()


# TRADES

def insert_trade(conn, t):
    """
    Append a Trade as it is sent and return its id.
    """
    cur = conn.execute(insert_sql("trades", Trade), asdict(t))
    conn.commit()
    t.id = cur.lastrowid
    return t.id


def update_trade(conn, t):
    """
    Write a Trade's fills, holdings, and outcome once it is done.
    """
    conn.execute(update_sql("trades", TRADE_FILLS), asdict(t))
    conn.commit()


def load_open_trades(conn, mode):
    """
    Trades of one mode that are done, still hold contracts, and have not settled,
    with their pair's label for log lines and the kickoff of their game, if any.
    """
    return [Trade(**dict(r)) for r in conn.execute("""
        SELECT t.*, p.label,
               (SELECT MAX(c.start_time) FROM contracts c
                WHERE (c.venue = t.yes_venue AND c.contract_id = t.yes_contract) OR (c.venue = t.no_venue AND c.contract_id = t.no_contract)) AS starts_at
        FROM trades t JOIN pairs p ON p.id = t.pair_id
        WHERE t.mode = ? AND t.status != 'sent' AND t.yes_held + t.no_held > 0
          AND NOT EXISTS (SELECT 1 FROM settlements s WHERE s.trade_id = t.id) ORDER BY t.id""", (mode,))]


def load_exposed_trades(conn, mode, paid_after):
    """
    Trades of one mode that are done, hold more on one side than the other,
    have not settled, and pay out, by the time we expected, after
    paid_after, with their pair's label, as (Trade, yes fee_info, no
    fee_info) with the fee schedule of each leg's contract. A trade with an
    order of unknown outcome is left out, since what it holds is unknown.
    """
    out = []
    for r in conn.execute("""
        SELECT t.*, p.label, yc.fee_info AS yes_fee_info, nc.fee_info AS no_fee_info
        FROM trades t
        LEFT JOIN pairs p ON p.id = t.pair_id
        LEFT JOIN contracts yc ON yc.venue = t.yes_venue AND yc.contract_id = t.yes_contract
        LEFT JOIN contracts nc ON nc.venue = t.no_venue AND nc.contract_id = t.no_contract
        WHERE t.mode = ? AND t.status != 'sent' AND t.yes_held != t.no_held AND t.pays_at > ?
          AND NOT EXISTS (SELECT 1 FROM settlements s WHERE s.trade_id = t.id)
          AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.trade_id = t.id AND o.status = 'error')
        ORDER BY t.id""", (mode, paid_after)):
        row = dict(r)
        yes_fee_info, no_fee_info = jsonutil.parse(row.pop("yes_fee_info"), {}), jsonutil.parse(row.pop("no_fee_info"), {})
        out.append((Trade(**row), yes_fee_info, no_fee_info))
    return out


# Each leg the unsettled trades of one mode still hold contracts on, a row a leg: its venue, its contract, what it cost,
# and its contracts, positive for the contract's yes and negative for its no. The reads of what the trades hold add it up.
HELD_LEGS = """
    SELECT yes_venue AS venue, yes_contract AS contract_id, yes_cost AS cost,
           CASE WHEN yes_polarity = 'yes' THEN yes_held ELSE -yes_held END AS contracts FROM trades t
    WHERE mode = :mode AND yes_held > 0 AND NOT EXISTS (SELECT 1 FROM settlements s WHERE s.trade_id = t.id)
    UNION ALL
    SELECT no_venue, no_contract, no_cost, CASE WHEN no_polarity = 'no' THEN no_held ELSE -no_held END FROM trades t
    WHERE mode = :mode AND no_held > 0 AND NOT EXISTS (SELECT 1 FROM settlements s WHERE s.trade_id = t.id)"""


def load_held(conn, mode):
    """
    Dollars the unsettled trades of one mode hold on each venue, at what
    their legs paid for the contracts they still hold, as {venue: dollars},
    for the brakes. Pairs a venue has already paid back by netting are
    still in it, see load_netted().
    """
    rows = conn.execute(f"SELECT venue, SUM(cost) FROM ({HELD_LEGS}) GROUP BY venue", {"mode": mode})
    return {venue: dollars for venue, dollars in rows}


def netted(legs):
    """
    The contracts each venue has already paid back by netting, as {venue:
    pairs}, from legs as (venue, contract_id, contracts), positive for the
    contract's yes and negative for its no, as HELD_LEGS gives them. Both
    venues net a contract's yes and no holdings into one position and pay
    a dollar a pair at once, so when a later trade holds one outcome of a
    contract an earlier open trade holds the other of, on the same venue,
    those pairs are back in cash, though our records count what they cost
    until both trades settle. A contract's pairs are the smaller of the
    contracts held on its yes and on its no, and the same id on two venues
    never nets. Only the venues pay them back: paper's books do not net.
    On 2026-10-11 they were 443.51 on Polymarket US and 29.59 on Kalshi,
    of the 3,601.57$ open live trades held.
    """
    held = {}
    for venue, contract_id, contracts in legs:
        outcomes = held.setdefault((venue, contract_id), {"yes": 0.0, "no": 0.0})
        outcomes["yes" if contracts > 0 else "no"] += abs(contracts)
    pairs = {}
    for (venue, _), outcomes in held.items():
        pairs[venue] = pairs.get(venue, 0.0) + min(outcomes.values())
    return {venue: round(n, 2) for venue, n in sorted(pairs.items()) if round(n, 2)}


def load_netted(conn, mode):
    """
    The pairs each venue has already paid back by netting what the
    unsettled trades of one mode hold, as {venue: pairs}, a dollar each,
    see netted(), for the brakes, which ask it of live alone.
    """
    return netted(conn.execute(f"SELECT venue, contract_id, contracts FROM ({HELD_LEGS})", {"mode": mode}))


def load_holdings(conn, mode):
    """
    What the unsettled trades of one mode hold of each contract, as
    {(venue, contract_id): contracts}, positive for the contract's yes and
    negative for its no, to compare with the venues' own positions. The
    sum nets yes against no as the venues do, so it is held net of the
    pairs they paid back, see netted(), like their positions.
    """
    rows = conn.execute(f"SELECT venue, contract_id, SUM(contracts) FROM ({HELD_LEGS}) GROUP BY venue, contract_id", {"mode": mode})
    return {(venue, contract_id): round(contracts, 2) for venue, contract_id, contracts in rows if round(contracts, 2)}


def last_order_id(conn):
    """
    The id of the newest live order, or None when there is none.
    """
    return conn.execute("SELECT MAX(id) FROM orders").fetchone()[0]


def insert_settlement(conn, settlement):
    """
    Store how a trade's legs paid out, which marks the trade settled.
    """
    conn.execute(insert_sql("settlements", Settlement), asdict(settlement))
    conn.commit()


def load_settlements(conn, mode=None):
    """
    Every Settlement, or those of one mode, oldest trade first.
    """
    sql = "SELECT * FROM settlements" + (" WHERE mode = ?" if mode else "") + " ORDER BY trade_id"
    return [Settlement(**dict(r)) for r in conn.execute(sql, (mode,) if mode else ())]


# ORDERS

def insert_order(conn, order):
    """
    Store a live Order before it is sent and set its id.
    """
    cur = conn.execute(insert_sql("orders", Order), asdict(order))
    conn.commit()
    order.id = cur.lastrowid
    return order.id


def update_order(conn, order):
    """
    Write what the venue answered to an Order.
    """
    conn.execute(update_sql("orders", ORDER_ANSWER), asdict(order))
    conn.commit()


def recent_orders(conn, count, venue=None, since=None):
    """
    The (id, status) of the newest answered live orders, newest first, at
    most count of them, only one venue's when given, and only those sent
    after since when given.
    """
    sql, params = "SELECT id, status FROM orders WHERE status != 'sent'", []
    if venue:
        sql, params = sql + " AND venue = ?", params + [venue]
    if since:
        sql, params = sql + " AND sent_at > ?", params + [since]
    return [tuple(row) for row in conn.execute(sql + " ORDER BY id DESC LIMIT ?", params + [count])]


def load_trade_cash(conn, mode, since):
    """
    The cash each done trade of one mode moved, for those with an order
    answered or a settlement at or after since, as dicts with its id,
    yes_held and no_held, settled_at and payouts once it has settled, the
    dollars its orders bought and sold, fees included, the time of its last
    answer, and unknown, how many of its orders had an unknown outcome.
    Only live trades have orders, so paper ones show nothing bought.
    """
    return [dict(r) for r in conn.execute("""
        SELECT t.id, t.yes_held, t.no_held, s.settled_at,
               COALESCE(s.yes_payout, 0) + COALESCE(s.no_payout, 0) AS payouts,
               COALESCE(SUM(CASE WHEN o.action = 'buy' THEN o.dollars END), 0) AS bought,
               COALESCE(SUM(CASE WHEN o.action = 'sell' THEN o.dollars END), 0) AS sold,
               MAX(o.answered_at) AS last_answer,
               COALESCE(SUM(o.status = 'error'), 0) AS unknown
        FROM trades t
        LEFT JOIN settlements s ON s.trade_id = t.id
        LEFT JOIN orders o ON o.trade_id = t.id
        WHERE t.mode = ? AND t.status != 'sent'
        GROUP BY t.id
        HAVING COALESCE(s.settled_at, MAX(o.answered_at)) >= ?
        ORDER BY t.id""", (mode, since))]


def load_orders(conn, trade_id=None):
    """
    Live Orders oldest first, all of them or those of one trade.
    """
    sql = "SELECT * FROM orders" + (" WHERE trade_id = ?" if trade_id is not None else "") + " ORDER BY id"
    return [Order(**dict(r)) for r in conn.execute(sql, (trade_id,) if trade_id is not None else ())]


# LEDGER

def add_ledger(conn, entry):
    """
    Record one Ledger entry, with the balance it left behind.
    """
    conn.execute(insert_sql("ledger", Ledger), asdict(entry))
    conn.commit()


def last_balances(conn):
    """
    Return {venue: balance} from each venue's newest ledger entry.
    """
    return {venue: balance for venue, balance in conn.execute(
        "SELECT venue, balance FROM ledger WHERE id IN (SELECT MAX(id) FROM ledger GROUP BY venue)")}


# ALERTS

def insert_alert(conn, alert):
    """
    Store an Alert as it is raised and set its id.
    """
    cur = conn.execute(insert_sql("alerts", Alert), asdict(alert))
    conn.commit()
    alert.id = cur.lastrowid
    return alert.id


def update_alert(conn, alert):
    """
    Write whether an Alert's email went out.
    """
    conn.execute(update_sql("alerts", ["sent_at", "error"]), asdict(alert))
    conn.commit()


def alert_raised(conn, kind, subject):
    """
    Whether an alert of a kind with this subject has been raised before, sent or not.
    """
    return conn.execute("SELECT 1 FROM alerts WHERE kind = ? AND subject = ?", (kind, subject)).fetchone() is not None


def last_alert_ts(conn, kind):
    """
    When the newest alert of a kind was raised, or None when there has been none.
    """
    return conn.execute("SELECT MAX(ts) FROM alerts WHERE kind = ?", (kind,)).fetchone()[0]
