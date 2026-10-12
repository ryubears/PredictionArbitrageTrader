"""
Bring older databases up to the current schema, one numbered step at a time.

SQLite's user_version holds the number of the last step a database has
had. Each connection runs the steps after it, in order, and records each
one, so a step runs once per database. A new database is built from
schema.sql, which already has every step in it, so it starts at the last
step and runs none.

To change a table that already exists: change schema.sql, and add a step
at the end of STEPS that makes the same change to an older database.
Steps run after schema.sql, so a table the step needs that is new in the
schema is already there.
"""

from db import schema

LEDGER_START_BALANCE = 10000.0      # What each venue started with when ledgers only held movements.
# How a trade settled, in the columns trades carried before settlements had a table of their own.
SETTLEMENT_COLUMNS = ["yes_result", "yes_payout", "yes_settled_at", "no_result", "no_payout", "no_settled_at", "settled_at"]


def add_columns(conn, table, columns, kind):
    """
    Add each of the columns the table lacks, all of one kind, a type and any constraint.
    """
    have = schema.table_columns(conn, table)
    for column in columns:
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


def copy_settlements(conn, table):
    """
    Copy the settlement columns of a trades table in the older shape into the settlements table.
    """
    conn.execute(f"""INSERT OR IGNORE INTO settlements (trade_id, settled_at, yes_result, yes_payout, yes_settled_at,
                                                        no_result, no_payout, no_settled_at)
                     SELECT id, settled_at, yes_result, yes_payout, yes_settled_at, no_result, no_payout, no_settled_at
                     FROM {table} WHERE settled_at IS NOT NULL""")


def migrate_pair_ids(conn):
    """
    Pairs used to be keyed by their label, copied onto bets, opportunities,
    and trades. Give them an id and point the other tables at it. Episodes
    and trades of pairs that had already left the catalog get a bare pair
    row, so their id resolves.
    """
    if "id" not in schema.table_columns(conn, "pairs"):
        conn.execute("ALTER TABLE pairs RENAME TO pairs_old")
        schema.create(conn)
        conn.execute("""INSERT INTO pairs (label, kind, season, game_date, team_a, team_b, subject, line, venues, contracts, flags, matched_at)
                        SELECT label, kind, season, game_date, team_a, team_b, subject, line, venues, contracts, flags, matched_at FROM pairs_old""")
        conn.execute("DROP TABLE pairs_old")
    if "pair_label" in schema.table_columns(conn, "bets"):
        conn.execute("ALTER TABLE bets ADD COLUMN pair_id INTEGER")
        conn.execute("UPDATE bets SET pair_id = (SELECT id FROM pairs WHERE label = bets.pair_label)")
        conn.execute("ALTER TABLE bets DROP COLUMN pair_label")
    for table, first_ts in (("opportunities", "start_ts"), ("trades", "signal_ts")):
        old_columns = schema.table_columns(conn, table)
        if "label" not in old_columns:
            continue
        conn.execute(f"""INSERT OR IGNORE INTO pairs (label, kind, venues, contracts, flags, matched_at)
                         SELECT label, kind, '', 0, '[]', MIN({first_ts}) FROM {table} GROUP BY label""")
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
        schema.create(conn)
        shared = [c for c in schema.table_columns(conn, table) if c in old_columns]
        conn.execute(f"""INSERT INTO {table} ({', '.join(shared)}, pair_id)
                         SELECT {', '.join('o.' + c for c in shared)}, p.id FROM {table}_old o JOIN pairs p ON p.label = o.label""")
        if table == "trades" and "settled_at" in old_columns:
            # The table was rebuilt from today's schema, where settlements have a table of their own.
            copy_settlements(conn, "trades_old")
        conn.execute(f"DROP TABLE {table}_old")


def step_1_catch_up(conn):
    """
    Every change made before migrations were numbered, each applied only
    when the database still shows the old shape. Derived tables are dropped
    when their columns changed, since a match rebuilds them.
    """
    if "group_label" in schema.table_columns(conn, "bets"):
        conn.execute("ALTER TABLE bets RENAME COLUMN group_label TO pair_label")     # Turned into pair_id below.
    conn.execute("DROP TABLE IF EXISTS bet_groups")
    conn.execute("DROP TABLE IF EXISTS fee_history")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'stream_gaps'").fetchone():
        conn.execute("INSERT OR REPLACE INTO gaps (venue, start_ts, end_ts) SELECT venue, start_ts, end_ts FROM stream_gaps")
        conn.execute("DROP TABLE stream_gaps")
    ledger_columns = schema.table_columns(conn, "ledger")
    if ledger_columns and "balance" not in ledger_columns:
        # Older ledgers only held the movements. Replay them from the starting balance to fill in the running balance.
        conn.execute("ALTER TABLE ledger ADD COLUMN balance REAL NOT NULL DEFAULT 0")
        running = {}
        for row_id, venue, amount in conn.execute("SELECT id, venue, amount FROM ledger ORDER BY id").fetchall():
            running[venue] = running.get(venue, LEDGER_START_BALANCE) + amount
            conn.execute("UPDATE ledger SET balance = ? WHERE id = ?", (running[venue], row_id))
    trade_columns = schema.table_columns(conn, "trades")
    if trade_columns and "cap" not in trade_columns:
        conn.execute("ALTER TABLE trades ADD COLUMN cap INTEGER")
    if trade_columns and "yes_result" not in trade_columns:
        for column, kind in (("yes_result", "TEXT"), ("yes_payout", "REAL"), ("yes_settled_at", "TEXT"),
                             ("no_result", "TEXT"), ("no_payout", "REAL"), ("no_settled_at", "TEXT")):
            conn.execute(f"ALTER TABLE trades ADD COLUMN {column} {kind}")
    if "side" in schema.table_columns(conn, "settlements"):
        # Settlements used to be a table of legs. Fold each leg into its trade's columns, which step 2 moves to today's settlements.
        for trade_id, side, result, payout, settled_at in conn.execute(
                "SELECT trade_id, side, result, payout, settled_at FROM settlements").fetchall():
            conn.execute(f"UPDATE trades SET {side}_result = ?, {side}_payout = ?, {side}_settled_at = ? WHERE id = ?",
                         (result, payout, settled_at, trade_id))
        conn.execute("DROP TABLE settlements")
    columns = schema.table_columns(conn, "opportunities")
    if "scope" in columns:
        conn.execute("DROP TABLE opportunities")
    if "source" in columns:
        # Rows from the retired replay scanner stay as part of the log, without the column that told them apart.
        conn.execute("ALTER TABLE opportunities DROP COLUMN source")
    migrate_pair_ids(conn)
    schema.create(conn)


def step_2_settlements(conn):
    """
    Trades used to carry how they settled in seven columns. Move those to
    the settlements table, one row per settled trade, and drop them.
    """
    columns = schema.table_columns(conn, "trades")
    if "settled_at" not in columns:
        return
    copy_settlements(conn, "trades")
    for column in SETTLEMENT_COLUMNS:
        conn.execute(f"ALTER TABLE trades DROP COLUMN {column}")


def step_3_opening_balances(conn):
    """
    Each venue's ledger now opens with a 'transfer_in' of its starting
    balance. Older ledgers began with their first trade, so work each
    venue's starting balance out from its first entry and write the ledger
    again with the openings in front. Nothing refers to ledger ids.
    """
    rows = conn.execute("SELECT ts, venue, amount, reason, trade_id, balance FROM ledger ORDER BY id").fetchall()
    first = {}
    for ts, venue, amount, reason, trade_id, balance in rows:
        first.setdefault(venue, (ts, balance - amount))
    if not first:
        return
    openings = [(ts, venue, start, "transfer_in", None, start) for venue, (ts, start) in first.items()]
    conn.execute("DELETE FROM ledger")
    conn.executemany("INSERT INTO ledger (ts, venue, amount, reason, trade_id, balance) VALUES (?,?,?,?,?,?)",
                     openings + [tuple(r) for r in rows])


def step_4_modes(conn):
    """
    Trades and settlements now say which executor made them, 'paper' or
    'live'. Every row before live trading came from the paper executor.
    """
    for table in ("trades", "settlements"):
        add_columns(conn, table, ["mode"], "TEXT NOT NULL DEFAULT 'paper'")


def step_5_drop_quotes(conn):
    """
    Order books are no longer stored, since everything prices from the
    books in memory, so the quotes table goes. It held nearly all of the
    file, so the file is rebuilt without it, which gives the space back.
    """
    conn.execute("DROP TABLE IF EXISTS quotes")
    conn.commit()
    conn.execute("VACUUM")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")     # So the file shrinks now, not at the next checkpoint.


def step_6_pair_sports(conn):
    """
    Pairs now say which sport they are, and their labels start with it,
    since two sports can describe different bets in the same words: the
    Broncos' and the Nuggets' titles are both 'champion 2027 DEN'. Every
    pair before this is an NFL one. An older database whose pairs table was
    rebuilt by an earlier step already has the column, but not the labels.
    """
    add_columns(conn, "pairs", ["sport"], "TEXT NOT NULL DEFAULT 'nfl'")
    conn.execute("UPDATE pairs SET label = 'nfl ' || label WHERE label NOT LIKE 'nfl %'")


def step_7_min_edge_stretch(conn):
    """
    Episodes now keep their longest stretch at config.MIN_EDGE or more and
    the contracts that stayed fillable through it. Older episodes did not
    record them, so theirs are null.
    """
    add_columns(conn, "opportunities", ["min_edge_seconds", "min_edge_size", "min_edge_profit"], "REAL")


def step_8_drop_transfers(conn):
    """
    Paper money no longer moves between the venues, so the transfers table goes.
    """
    conn.execute("DROP TABLE IF EXISTS transfers")


def step_9_drop_trade_caps(conn):
    """
    Live trades are no longer capped, so trades lose the cap column that held a live trade's.
    """
    if "cap" in schema.table_columns(conn, "trades"):
        conn.execute("ALTER TABLE trades DROP COLUMN cap")


def step_10_twin_sequences(conn):
    """
    The in-play test's trades with a leg on each venue now say in what order
    their two orders went out. Every one before this sent both at once.
    """
    add_columns(conn, "twins", ["sequence"], "TEXT")
    if conn.execute("SELECT 1 FROM twins LIMIT 1").fetchone():     # Only a database that ran the test has trades with venues.
        conn.execute("""UPDATE twins SET sequence = 'together' WHERE sequence IS NULL AND live_trade_id IN
                        (SELECT id FROM trades WHERE yes_venue != no_venue)""")


def step_11_order_book_times(conn):
    """
    Live orders now record the times of the book they were sent on, so a
    miss can be told apart from a market that had gone quiet. Older orders
    did not, so theirs are null.
    """
    add_columns(conn, "orders", ["book_at", "book_ts"], "TEXT")


def step_12_trades_in_play(conn):
    """
    Trades now say whether their game was under way at the signal. Older
    trades did not, so theirs is null.
    """
    add_columns(conn, "trades", ["in_play"], "INTEGER")


def step_13_opportunities_take(conn):
    """
    Episodes now keep what an order sent at their peak could have had as
    live takes it, which the summary shows them by. Older episodes did not,
    so theirs is null and the summary leaves them out.
    """
    add_columns(conn, "opportunities", ["take_size", "take_profit"], "REAL")


def step_14_opportunities_pm_changed(conn):
    """
    Episodes now say whether their peak came with a change of the
    Polymarket US leg's book. Older episodes did not, so theirs is null.
    """
    add_columns(conn, "opportunities", ["pm_changed"], "INTEGER")


# Step n brings a database from user_version n - 1 to n. Only ever add to the end.
STEPS = [step_1_catch_up, step_2_settlements, step_3_opening_balances, step_4_modes, step_5_drop_quotes, step_6_pair_sports,
         step_7_min_edge_stretch, step_8_drop_transfers, step_9_drop_trade_caps, step_10_twin_sequences, step_11_order_book_times,
         step_12_trades_in_play, step_13_opportunities_take, step_14_opportunities_pm_changed]


def migrate(conn, fresh):
    """
    Run the steps this database has not had, recording each. A fresh database only records the last step.
    """
    version = len(STEPS) if fresh else conn.execute("PRAGMA user_version").fetchone()[0]
    for number, step in enumerate(STEPS[version:], start=version + 1):
        step(conn)
        conn.execute(f"PRAGMA user_version = {number}")
    if fresh:
        conn.execute(f"PRAGMA user_version = {len(STEPS)}")
    conn.commit()
