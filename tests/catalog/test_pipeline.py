"""
Test the catalog refresh with the venue calls replaced by canned contracts.
"""

from catalog import pipeline
from db import database
from db.models import Contract


def canned(venue, contract_id, **fields):
    c = Contract(venue=venue, contract_id=contract_id, market_id=contract_id,
                 event_id="KXSB-27" if venue == "kalshi" else "nfl-champ-2027-02-14-w",
                 series_id="KXSB" if venue == "kalshi" else "nfl-2026", sport="nfl", event_title="Pro Football Champion",
                 title="KC Chiefs", outcome="Kansas City", market_type=None if venue == "kalshi" else "futures",
                 line=None, rules=None, start_time=None, close_time="2027-02-14T23:30:00+00:00", fee_info={"rate": 0.05})
    for k, v in fields.items():
        setattr(c, k, v)
    return c


def test_refresh_fetches_classifies_and_pairs(tmp_path, monkeypatch):
    # The refresh must be pointed at a scratch database. It writes every table it touches.
    canned_by_venue = {"kalshi": [canned("kalshi", "KXSB-27-KC")], "polymarket_us": [canned("polymarket_us", "tec-nfl-champ-2027-02-14-w-kc")]}
    monkeypatch.setattr(pipeline.fetch, "fetch_contracts", lambda venue, sport: canned_by_venue[venue])
    logs = []
    summary = pipeline.refresh("nfl", log=logs.append, db_path=tmp_path / "t.sqlite")
    assert summary == "kalshi 1 contracts, polymarket_us 1 contracts, 2 bets, 1 pairs, 0 nested"
    assert [line.split(" in ")[0] for line in logs] == ["fetched 1 nfl contracts from kalshi", "fetched 1 nfl contracts from polymarket_us"]
    with database.connect(tmp_path / "t.sqlite") as conn:
        assert [p["label"] for p in database.load_pairs(conn, "nfl").values()] == ["nfl champion 2027 KC"]
