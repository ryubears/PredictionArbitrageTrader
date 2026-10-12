"""
Refresh the catalog. Fetch every venue, classify the contracts into bets,
pair them across venues, and nest each bet's lines, see nest.py, in one
call.

The live process runs this on a timer so new games enter the pairs while
it runs. The same steps are available one at a time as fetch.py,
classify.py, and match.py, which also print their full reports.

Run with:
    python3 -m catalog.pipeline --sport nfl
"""

import argparse
from catalog import fetch, match, nest
from catalog.classify import classify
from common.timeutil import now_iso, shift
from common.venues import VENUES
from db import database


def refresh(sport, log=print, db_path=None):
    """
    Run fetch, classify, and match for a sport with a fresh database connection.
    Returns a one line summary. Safe to call from a worker thread.
    """
    with database.connect(db_path) as conn:
        counts = fetch.fetch_and_store(sport, VENUES, conn, log)
        bets, _ = classify.classify_all(database.load_contracts(conn, sport=sport))
        database.replace_bets(conn, sport, bets)
        rows = database.load_bets(conn, sport)
        pairs, _ = match.match(rows, sport)
        matched_at = now_iso()
        database.replace_pairs(conn, sport, pairs, matched_at)
        nests = nest.nest(rows, sport, since=shift(matched_at, days=-1)[:10])
        database.replace_nests(conn, sport, nests, matched_at)
    return ", ".join([f"{venue} {n} contracts" for venue, n in counts.items()]
                     + [f"{len(bets)} bets, {len(pairs)} pairs, {len(nests)} nested"])


# MAIN

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Fetch, classify, and match in one go.")
    ap.add_argument("--sport", default="nfl", choices=sorted(fetch.SPORTS))
    args = ap.parse_args()
    print(refresh(args.sport))
