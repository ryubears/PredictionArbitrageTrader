"""
Tests for the live process's refresh loop.
"""

import asyncio
import multiprocessing
import os
import pytest
import scripted_refresh
from db import database
from engine import run
from engine.components.market import streams
from engine.components.money import live as money_live
from engine.components.trading import brakes, live as trading_live, notify


def test_run_survives_a_failing_refresh(tmp_path, monkeypatch, capsys, fake_stream):
    def broken_refresh(sport, log=print, db_path=None):
        raise RuntimeError("kalshi is down")
    monkeypatch.setattr(run.pipeline, "refresh", broken_refresh)
    # In this process, where the broken refresh is patched in. A child would import the real one.
    monkeypatch.setattr(run, "refresh_in_child", lambda sports: asyncio.to_thread(run.refresh_catalog, sports, run.log))
    for venue in streams.STREAMS:
        monkeypatch.setitem(streams.STREAMS, venue, fake_stream)
    conn = database.connect(tmp_path / "test.sqlite")
    asyncio.run(run.run(conn, run.RunOptions(sports=("nfl",), seconds=3, catalog_seconds=1)))
    out = capsys.readouterr().out
    assert "starting nfl, code " in out.splitlines()[0]
    failed = "nfl catalog refresh failed (RuntimeError('kalshi is down')), keeping its stored catalog"
    assert out.count(failed) >= 2                                                   # Before starting, and at the next refresh.
    assert "    RuntimeError: kalshi is down" in out                                  # With the traceback.
    assert "catalog refreshed, nfl: failed" in out


def test_a_sport_whose_refresh_fails_leaves_the_others_to_refresh(monkeypatch):
    def refresh(sport, log=print, db_path=None):
        if sport == "ncaaf":
            raise RuntimeError("cfb tag gone")
        return "1 pairs"
    monkeypatch.setattr(run.pipeline, "refresh", refresh)
    logs = []
    assert run.refresh_catalog(("nfl", "ncaaf"), logs.append) == "nfl: 1 pairs; ncaaf: failed"
    assert logs[0].startswith("ncaaf catalog refresh failed (RuntimeError('cfb tag gone')), keeping its stored catalog")


def test_the_refresh_runs_in_a_child_process_that_is_gone_once_it_answers():
    line = asyncio.run(run.refresh_in_child(("nfl", "ncaaf"), scripted_refresh.refresh))
    assert line.startswith("refreshed nfl, ncaaf in process ")
    assert int(line.split()[-1]) != os.getpid()
    assert multiprocessing.active_children() == []


def test_a_refresh_cancelled_as_the_run_stops_takes_its_child_down():
    async def stop_midway():
        refresh = asyncio.create_task(run.refresh_in_child(("nfl",), scripted_refresh.slow))
        await asyncio.sleep(0.5)
        refresh.cancel()
        with pytest.raises(asyncio.CancelledError):
            await refresh
    asyncio.run(stop_midway())
    assert multiprocessing.active_children() == []


def test_a_refresh_process_that_dies_without_answering_fails_the_refresh():
    with pytest.raises(RuntimeError, match="the refresh process ended without an answer, exit code 3"):
        asyncio.run(run.refresh_in_child(("nfl",), scripted_refresh.crash))
    assert multiprocessing.active_children() == []


def session(tmp_path, monkeypatch, fake_stream, **kwargs):
    for venue in streams.STREAMS:
        monkeypatch.setitem(streams.STREAMS, venue, fake_stream)
    return run.Session(database.connect(tmp_path / "test.sqlite"), ("nfl",), **kwargs)


def test_session_without_scanning_or_trading_only_records(tmp_path, monkeypatch, capsys, fake_stream):
    async def scenario():
        s = session(tmp_path, monkeypatch, fake_stream, with_scanner=False)
        s.start()
        s.tick()
        await s.close()
        return s
    s = asyncio.run(scenario())
    assert (s.scanner, s.desks) == (None, [])
    out = capsys.readouterr().out
    assert "nothing to record, run pipeline.py first" in out
    assert "tracking 0 books" in out and "scanner:" not in out and "paper:" not in out


def test_session_logs_every_components_summary_when_due_and_on_close(tmp_path, monkeypatch, capsys, fake_stream):
    async def scenario():
        s = session(tmp_path, monkeypatch, fake_stream)
        s.start()
        capsys.readouterr()
        s.last_summary = s.last_status = 0        # Everything is overdue at the first tick.
        s.tick()
        on_tick = capsys.readouterr().out
        await s.close()
        return on_tick, capsys.readouterr().out

    def heads(out):
        return [line[9:].split(":")[0].split(",")[0] for line in out.splitlines()]      # Past the timestamp.
    on_tick, on_close = asyncio.run(scenario())
    assert heads(on_tick) == heads(on_close) == ["scanner", "paper", "paper settled", "tracking 0 books"]


def test_a_trading_session_logs_its_settings_when_it_starts(tmp_path, monkeypatch, capsys, fake_stream):
    async def scenario():
        s = session(tmp_path, monkeypatch, fake_stream)
        s.start()
        await s.close()
    asyncio.run(scenario())
    out = [line[9:] for line in capsys.readouterr().out.splitlines()]                       # Past the timestamp.
    assert out[0].startswith("settings: min edge 0.02$ and 100% a year, live paying 24h or more out before a game and on a future, "
                             "a game paying within 24h, legs on two venues, Polymarket US's first, or on a future two of one venue "
                             "with the same rules, fill share 1.0, expected game nfl 3.25h")
    assert out[1] == ("paper rejects 0% of orders, which take kalshi 12ms there (12 to flatten) and 8 back, polymarket_us 59ms there "
                      "(24 to flatten) and 31 back, filling at the venue's book then, waiting up to kalshi 0.1s, polymarket_us 0.3s "
                      "for it to reach us, start balance 10,000$")


def test_a_session_trading_both_modes_keeps_a_desk_for_each_and_offers_live_the_signal_first(tmp_path, monkeypatch, capsys, fake_stream):
    monkeypatch.setattr(money_live, "READERS", {"kalshi": lambda: (800.0, {}), "polymarket_us": lambda: (600.0, {})})
    monkeypatch.setattr(trading_live, "POSITIONS", {"kalshi": lambda: {}, "polymarket_us": lambda: {}})     # The venues' positions, checked.
    monkeypatch.setattr(brakes, "HALT_FILE", tmp_path / "live_halt.txt")
    monkeypatch.setattr(notify, "EMAIL_FILE", tmp_path / "email.json")

    async def scenario():
        s = session(tmp_path, monkeypatch, fake_stream, executors=run.EXECUTE["both"])
        s.start()
        s.last_summary = 0
        s.tick()                                    # The first tick starts reading the live balances.
        await s.desks[0].cash.readings.running
        s.tick()
        await s.close()
        return s
    s = asyncio.run(scenario())
    assert [d.mode for d in s.desks] == ["live", "paper"]
    assert s.scanner.on_signals == [s.desks[0].signal, s.desks[1].signal]
    assert all(d.executor.recheck == s.scanner.recheck and d.executor.edge_since == s.scanner.edge_since
               and d.executor.edge_books == s.scanner.edge_books for d in s.desks)
    # Both trade only where the exchange trades, and a market that turned a live order away as not trading is left alone.
    assert all(d.executor.venue_trading == s.exchange.trading and d.executor.market_closed == s.recorder.refuse for d in s.desks)
    assert s.exchange.readings.last is not None                                             # The first tick read it.
    assert s.desks[0].cash.amounts == {"kalshi": 800.0, "polymarket_us": 600.0}
    assert s.desks[1].cash.amounts == {"kalshi": 10000.0, "polymarket_us": 10000.0}         # Paper money is its own.
    out = [line[9:] for line in capsys.readouterr().out.splitlines()]                       # Past the timestamp.
    assert out[0].startswith("settings: min edge") and out[1].startswith(
        "LIVE TRADING with real money: a future's edge once it has lasted 0.1s returning 100% a year, its orders down to the levels "
        "returning that, a leg's book waiting only for the other's change before the edge began, balances read")
    assert out[2:6] == ["live trades the futures of nfl", f"no email settings in {tmp_path / 'email.json'}, alerts are only logged and stored",
                        out[4], "paper trades the games, matches, and races of every sport that pay within 24h, in play too"]
    assert out[4].startswith("paper rejects 0% of orders")
    assert s.desks[1].executor.tapes is s.tapes is s.recorder.tapes         # Paper's orders meet the books the recorder tapes.
    assert "live balances read: kalshi 800$, polymarket_us 600$" in out
    heads = [line.split(":")[0] for line in out]
    assert {"live", "live settled", "paper", "paper settled"} <= set(heads) and not any(head.endswith("capital") for head in heads)


def test_paper_trades_the_bets_on_one_event_and_live_the_futures_of_sports_not_held_out(tmp_path, monkeypatch, capsys, fake_stream):
    monkeypatch.setattr(money_live, "READERS", {"kalshi": lambda: (800.0, {}), "polymarket_us": lambda: (600.0, {})})
    monkeypatch.setattr(trading_live, "POSITIONS", {"kalshi": lambda: {}, "polymarket_us": lambda: {}})
    monkeypatch.setattr(notify, "EMAIL_FILE", tmp_path / "email.json")
    for venue in streams.STREAMS:
        monkeypatch.setitem(streams.STREAMS, venue, fake_stream)

    async def scenario():
        s = run.Session(database.connect(tmp_path / "test.sqlite"), ("nfl", "darts"), executors=run.EXECUTE["both"], not_live=("darts",))
        s.start()
        offered = []
        for desk in s.desks:
            desk.executor.signal = lambda pair, *args, mode=desk.mode: offered.append((mode, pair["sport"], pair["game_date"])) or True
        pairs = [{"sport": "nfl", "game_date": "2026-10-11"}, {"sport": "nfl", "game_date": None},
                 {"sport": "darts", "game_date": "2026-10-04"}, {"sport": "darts", "game_date": None}]
        results = [desk.signal(pair, None, None, 0.1, 5, {}, "now") for pair in pairs for desk in s.desks]
        await s.close()
        return offered, results
    offered, results = asyncio.run(scenario())
    # Each desk trades apart, since on one signal live's real orders would take what paper's simulated ones look for. A
    # sport held out of live has its futures traded by neither, and its events by paper.
    assert offered == [("paper", "nfl", "2026-10-11"), ("live", "nfl", None), ("paper", "darts", "2026-10-04")]
    assert results == [False, True, True, False, False, True, False, False]
    out = capsys.readouterr().out
    assert "live trades the futures of nfl, not of darts" in out and "paper trades the games, matches, and races" in out


def test_live_in_play_is_offered_games_too_alone_or_beside_paper_which_gets_back_what_it_took(tmp_path, monkeypatch, capsys, fake_stream):
    monkeypatch.setattr(money_live, "READERS", {"kalshi": lambda: (800.0, {}), "polymarket_us": lambda: (600.0, {})})
    monkeypatch.setattr(trading_live, "POSITIONS", {"kalshi": lambda: {}, "polymarket_us": lambda: {}})
    monkeypatch.setattr(notify, "EMAIL_FILE", tmp_path / "email.json")
    with pytest.raises(ValueError, match="needs the live desk"):
        session(tmp_path, monkeypatch, fake_stream, executors=("paper",), live_in_play=True)

    async def scenario(executors):
        s = session(tmp_path, monkeypatch, fake_stream, executors=executors, live_in_play=True)
        s.start()
        offered = []
        for desk in s.desks:
            desk.executor.signal = lambda pair, *args, mode=desk.mode: offered.append((mode, pair["game_date"])) or True
        game, future = {"id": 1, "sport": "nfl", "game_date": "2026-10-11"}, {"id": 2, "sport": "nfl", "game_date": None}
        results = [desk.signal(pair, "y", "n", 0.1, 5, {}, "now") for pair in (game, future) for desk in s.desks]
        await s.close()
        return s, offered, results
    s, offered, results = asyncio.run(scenario(("live",)))
    # Live alone is offered the games and the futures. Whether a game is under way its executor judges, see live.py.
    assert s.desks[0].markets is None and s.desks[0].executor.in_play and s.desks[0].executor.footprints is None
    # A future's trade gives its episode back to this desk, the scanner's first, once done, see Executor.again().
    again = s.desks[0].executor.offer_again
    assert again.func == s.scanner.offer_again and again.args == (0,)
    assert offered == [("live", "2026-10-11"), ("live", None)] and results == [True, True]
    assert ("live also trades the games, matches, and races under way of every sport that pay within 24h, an edge of 0.02$ "
            "or more once it has lasted 0.1s at 0.02$ or more, at most 20 contracts a trade, "
            "Polymarket US's order first and Kalshi's for what it filled, giving up as much as 50% of the edge left"
            ) in capsys.readouterr().out
    s, offered, results = asyncio.run(scenario(run.EXECUTE["both"]))
    # Beside paper both take the game, live first, and paper is given back what live's orders took.
    assert s.desks[0].executor.footprints is s.desks[1].executor.footprints is not None
    assert offered == [("live", "2026-10-11"), ("paper", "2026-10-11"), ("live", None)] and results == [True, True, True, False]


def test_live_in_play_needs_live_trading_on_the_command_line():
    import subprocess, sys
    from pathlib import Path
    src = Path(run.__file__).resolve().parents[1]
    done = subprocess.run([sys.executable, "-m", "engine.run", "--live-in-play", "--execute", "paper"], cwd=src, capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 2 and "--live-in-play needs --execute live or both, scanning and trading" in done.stderr
