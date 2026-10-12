"""
Tests for the database report, run against a small database.
"""

from db import database
from db.models import Opportunity, Order, Settlement, Trade
from tools import summary

SINCE = "2026-09-27T12:00:00+00:00"     # Where the report's window starts.
BEFORE = "2026-09-27T11:00:00+00:00"    # An hour before the window.
INSIDE = "2026-09-27T20:00:00+00:00"    # Eight hours into it.


NOW = "2026-09-28T00:00:00+00:00"       # When the report runs, twelve hours into the window.


def report(tmp_path, monkeypatch, capsys, fill, modes=("live",), sports=(), markets=summary.MARKETS):
    """
    What the report prints for the 12 hours from SINCE, on a database with an nfl future, an nba future, and an nfl game,
    pairs 1 to 3, that fill(conn) adds to.
    """
    path = tmp_path / "t.sqlite"
    conn = database.connect(path)
    conn.execute("INSERT INTO pairs (id, sport, label, kind, venues, contracts, flags, matched_at) "
                 "VALUES (1, 'nfl', 'the bet', 'winner', 'kalshi,polymarket_us', 2, '[]', ?)", (BEFORE,))
    conn.execute("INSERT INTO pairs (id, sport, label, kind, venues, contracts, flags, matched_at) "
                 "VALUES (2, 'nba', 'other bet', 'champion', 'kalshi,polymarket_us', 2, '[]', ?)", (BEFORE,))
    conn.execute("INSERT INTO pairs (id, sport, label, kind, game_date, venues, contracts, flags, matched_at) "
                 "VALUES (3, 'nfl', 'a game', 'spread', '2026-09-27', 'kalshi,polymarket_us', 2, '[]', ?)", (BEFORE,))
    fill(conn)
    conn.commit()
    monkeypatch.setattr(summary, "DB_PATH", path)
    ro = database.read_only(path)      # As the script opens it.
    summary.print_overview(ro, sports)
    for market in markets:
        summary.print_market(ro, SINCE, 12, sports, market, modes, now=NOW)
    return capsys.readouterr().out


def table(out, title):
    """
    The rows of the table printed under title, each split into its cells:
    the lines after its header with as many cells as the first of them.
    """
    rows = []
    for line in out.split(f"\n{title}\n")[1].splitlines()[1:]:     # The header left out.
        cells = line.split()
        if not cells or (rows and len(cells) != len(rows[0])):
            break
        rows.append(cells)
    return rows


def episode(start, days, edge, size, live=0, pair_id=1, lasted=600.0, take=None, pm_changed=1):
    """
    An episode of the pair at start, paying days out, edge at its peak, and size fillable at the minimum edge or more for
    lasted seconds, ten minutes unless given. One order could have had take on live's levels, size unless given,
    and that moment came with a change of Polymarket US's book unless pm_changed is 0.
    """
    take = size if take is None else take
    return Opportunity(pair_id=pair_id, trade="t", yes_venue="kalshi", yes_contract="k", no_venue="polymarket_us", no_contract="pm",
                       start_ts=start, end_ts=start, seconds=600, peak_ts=start, peak_edge=edge, peak_size=size, peak_profit=size * edge,
                       live=live, days_held=days, return_pct=100 * edge / (1 - edge), annual_pct=100 * edge / (1 - edge) * 365 / days,
                       min_edge_seconds=lasted, min_edge_size=size, min_edge_profit=size * edge, take_size=take, take_profit=take * edge,
                       pm_changed=pm_changed)


def test_only_episodes_within_the_rules_are_shown(tmp_path, monkeypatch, capsys):
    def fill(conn):
        database.insert_opportunities(conn, [
            episode("2026-09-27T13:00:00+00:00", 2, 0.06, 100),           # 6 cents paying in two days, 1,165% a year.
            episode("2026-09-27T14:00:00+00:00", 0.5, 0.10, 100),         # Pays out in 12 hours, too soon.
            episode("2026-09-27T15:00:00+00:00", 150, 0.10, 100),         # 10 cents over 150 days, 27% a year, too little.
            episode("2026-09-27T16:00:00+00:00", 2, 0.015, 100),          # 1.5 cents paying in two days: a future has no 2 cent minimum.
            episode("2026-09-27T16:30:00+00:00", 2, 0.03, 100, lasted=0.004),      # 3 cents paying in two days, 278% a year.
            episode("2026-09-27T16:45:00+00:00", 2, 0.06, 0.06, lasted=900),        # Under a tenth of a contract at the peak, however long.
            episode("2026-09-27T16:46:00+00:00", 2, 0.06, 0.5),                     # Half a contract, which live opens from 2026-10-07.
            # Live took the one contract at the peak, which left 0.29 fillable through the stretch.
            episode("2026-09-27T16:50:00+00:00", 2, 0.14, 0.29, take=1),
            episode("2026-09-27T17:00:00+00:00", 2, 0.08, 100, live=1)])  # During a game.
    out = report(tmp_path, monkeypatch, capsys, fill)
    assert ("\nfutures opportunities (0.1+ contracts in one order on levels returning 100%+ a year once the edge lasted 0.1s, "
            "paying 24h+ out), last 12 hours\n"
            "  5 episodes could have taken 291$ in one order each and locked in 10.67$\n") in out
    assert [r[:3] for r in table(out, "futures opportunities by kind")] == [["nfl", "winner", "5"]]
    # Sizes to the hundredth live trades in, the half contract among them.
    assert [r[2:7] for r in table(out, "futures largest opportunities")] == [
        ["6.0", "600.000s", "100", "94", "6.00"], ["3.0", "0.004s", "100", "97", "3.00"], ["1.5", "600.000s", "100", "98", "1.50"],
        ["14.0", "600.000s", "1", "1", "0.14"], ["6.0", "600.000s", "0.5", "0", "0.03"]]
    assert ("\nin-play opportunities (0.1+ contracts in one order on levels at 2c+ once the edge lasted 0.1s, games, matches, and races under way, "
            "paying within 24h), last 12 hours\n  none\n") in out


def test_in_play_opportunities_are_the_games_under_way_paying_within_a_day(tmp_path, monkeypatch, capsys):
    def fill(conn):
        database.insert_opportunities(conn, [
            episode("2026-09-27T13:00:00+00:00", 0.1, 0.06, 100, live=1, pair_id=3, lasted=0.05),   # In play, but gone in 0.05s.
            episode("2026-09-27T13:10:00+00:00", 0.2, 0.08, 50, live=1, pair_id=3, lasted=1.5),     # In play, paying in 5 hours.
            episode("2026-09-27T13:20:00+00:00", 0.2, 0.50, 900, live=1, pair_id=3, lasted=2.0, pm_changed=0),  # On Kalshi's change too.
            episode("2026-09-27T13:30:00+00:00", 0.9, 0.06, 100, pair_id=3, lasted=1.5),            # Before it, paying in 22 hours.
            episode("2026-09-27T14:00:00+00:00", 2, 0.10, 100, pair_id=3),                          # Paying in two days.
            episode("2026-09-27T15:00:00+00:00", 0.5, 0.10, 100, live=1, pair_id=1)])    # A future that pays too soon.
    out = report(tmp_path, monkeypatch, capsys, fill, modes=summary.MODES["all"])
    assert ("\nin-play opportunities (0.1+ contracts in one order on levels at 2c+ once the edge lasted 0.1s, games, matches, and races under way, "
            "paying within 24h), last 12 hours\n") in out
    assert "  at 2c or more for 2.000s at the median, 2.000s at the 90th percentile, 2.000s at the longest\n" in out
    assert [r[:5] for r in table(out, "in-play opportunities by kind")] == [["nfl", "spread", "2", "2.000s", "2.000s"]]
    assert [r[3] for r in table(out, "in-play largest opportunities")] == ["2.000s", "1.500s"]
    assert ("\nfutures opportunities (0.1+ contracts in one order on levels returning 100%+ a year once the edge lasted 0.1s, "
            "paying 24h+ out), last 12 hours\n  none\n") in out
    assert out.index("\nfutures opportunities") < out.index("\nin-play opportunities")


def test_episodes_that_peaked_in_a_legs_venues_weekly_maintenance_are_left_out(tmp_path, monkeypatch, capsys):
    # Thursday, October 1, 2026: Kalshi's maintenance from 07:00 to 09:00 UTC, Polymarket US's from 10:00 to 12:00.
    def fill(conn):
        database.insert_opportunities(conn, [
            episode("2026-10-01T07:30:00+00:00", 0.2, 0.40, 900, live=1, pair_id=3, lasted=60),     # Kalshi's book froze.
            episode("2026-10-01T09:30:00+00:00", 0.2, 0.08, 50, live=1, pair_id=3, lasted=1.5),     # Both trade.
            episode("2026-10-01T10:30:00+00:00", 0.2, 0.30, 500, live=1, pair_id=3, lasted=30)])    # Polymarket US's.
    out = report(tmp_path, monkeypatch, capsys, fill)
    assert "  1 episodes could have taken 46$ in one order each and locked in 4.00$\n" in out
    assert [r[3] for r in table(out, "in-play largest opportunities")] == ["1.500s"]


def test_a_sport_filter_keeps_only_its_pairs_episodes_and_trades(tmp_path, monkeypatch, capsys):
    def fill(conn):
        database.insert_opportunities(conn, [episode("2026-09-27T13:00:00+00:00", 2, 0.06, 100),
                                             episode("2026-09-27T14:00:00+00:00", 2, 0.07, 50, pair_id=2)])
    out = report(tmp_path, monkeypatch, capsys, fill, sports=("nba",))
    assert "  1 episodes could have taken" in out and [r[:2] for r in table(out, "futures opportunities by kind")] == [["nba", "champion"]]
    assert "\nlive futures trades: 0 in all, 0 in the last 12 hours" in out


def test_each_market_shows_under_its_heading_its_opportunities_then_paper_then_live(tmp_path, monkeypatch, capsys):
    out = report(tmp_path, monkeypatch, capsys, lambda conn: None, modes=summary.MODES["all"])
    heads = ["===== futures =====", "futures opportunities", "paper futures trades: ", "paper futures open trades: ",
             "live futures trades: ", "live futures open trades: ",
             "===== in-play =====", "in-play opportunities", "paper in-play trades: ", "paper in-play open trades: ",
             "live in-play trades: ", "live in-play open trades: "]
    assert [out.index(f"\n{head}") for head in heads] == sorted(out.index(f"\n{head}") for head in heads)


def test_a_markets_trades_show_by_kind_largest_open_ones_opened_by_outcome_then_the_open_trades(tmp_path, monkeypatch, capsys):
    def fill(conn):
        open_trades(conn)
        # One settled in the window, whose legs no table lists by venue any more.
        t = held_trade(conn, signal=INSIDE, held=(10, 10), cost=(4.5, 4.7), profit=0.80)
        s = Settlement(t.id, INSIDE, mode="live")
        s.record("yes", "yes", 10.0, INSIDE)
        s.record("no", "no", 0.0, INSIDE)
        database.insert_settlement(conn, s)
        database.insert_order(conn, Order(trade_id=t.id, venue="kalshi", contract_id="k", purpose="open", action="buy", outcome="yes",
                                          quantity=10, limit_price=0.45, client_id="c1", sent_at=INSIDE, status="filled", filled=10,
                                          dollars=4.5, book_ts=INSIDE))
    out = report(tmp_path, monkeypatch, capsys, fill)
    heads = ["live futures trades: ", "  net ", "live futures by kind, last 12 hours", "live futures largest trades, last 12 hours",
             "live futures open trades opened in the last 12 hours, newest first", "live futures by outcome, last 12 hours",
             "live futures open trades: ", "  opened in the last hour", "  resolving first"]
    assert [out.index(f"\n{head}") for head in heads] == sorted(out.index(f"\n{head}") for head in heads)
    assert "settled legs" not in out and "orders" not in out


def test_live_money_shows_each_venue_read_now_with_the_kalshi_shards(capsys):
    def unreachable():
        raise RuntimeError("401 unauthorized")

    balances = summary.read_live_balances({"kalshi": lambda: (92.0, {0: 0.0, 1: 0.0, 2: 0.0, 3: 92.0}), "polymarket_us": unreachable})
    assert balances == {"kalshi": (92.0, {0: 0.0, 1: 0.0, 2: 0.0, 3: 92.0}), "polymarket_us": "not read (401 unauthorized)"}
    summary.print_live_money(balances)
    # The shards live trading uses, and any other holding money.
    assert capsys.readouterr().out.splitlines()[1:] == [
        "live money on the venues, read now", "  kalshi 92.00$ (shard 0 0.00$, shard 3 92.00$)", "  polymarket_us not read (401 unauthorized)"]


def open_trades(conn, mode="live", pair_id=1):
    """
    Add open trades of the mode on the pair: (held a side, yes cost, no cost, locked in, flattening, signal, pays at).
    One opened half an hour before NOW settling in December, one opened ten hours before it paying on September 27,
    already past and waiting on a venue, one opened two days before it paying October 6, one opened ten days before it
    paying October 4 that lost 2 cents flattening, and one in the window flattened to nothing, which holds nothing.
    """
    for held, yes_cost, no_cost, profit, hedge, signal, pays_at in (
            (5, 2.5, 2.25, 0.25, 0.0, "2026-09-27T23:30:00+00:00", "2026-12-20T00:00:00+00:00"),
            (5, 2.0, 2.7, 0.30, 0.0, "2026-09-27T14:00:00+00:00", "2026-09-27T20:00:00+00:00"),
            (5, 2.0, 2.7, 0.30, 0.0, "2026-09-26T00:00:00+00:00", "2026-10-06T00:00:00+00:00"),
            (5, 3.0, 1.7, 0.30, -0.02, "2026-09-18T00:00:00+00:00", "2026-10-04T20:45:00+00:00"),
            (0, 0.0, 0.0, 0.0, -0.05, INSIDE, INSIDE)):
        database.insert_trade(conn, Trade(mode=mode, pair_id=pair_id, trade="t", signal_ts=signal, edge=0.06, quantity=5,
                                          yes_venue="kalshi", yes_contract="k", yes_polarity="yes", yes_limit=0.5,
                                          no_venue="polymarket_us", no_contract="p", no_polarity="yes", no_limit=0.45,
                                          pays_at=pays_at, yes_held=held, no_held=held, yes_cost=yes_cost, no_cost=no_cost,
                                          profit=profit, hedge_pnl=hedge, status="filled"))


def test_open_trades_are_summed_up_with_when_they_opened_and_resolve(tmp_path, monkeypatch, capsys):
    out = report(tmp_path, monkeypatch, capsys, open_trades)
    lines = out.split("\nlive futures open trades: ")[1].splitlines()
    # The capital of each window is what its trades still hold.
    assert lines[:4] == ["4",
                         "  opened in the last hour  1 using  4.75$",
                         "  opened in the last day   2 using  9.45$",
                         "  opened in the last week  3 using 14.15$"]
    assert lines[4] == "  capital 18.85$, held on kalshi 9.50$, polymarket_us 9.35$"
    # 1.13$ on 18.85$ is 6.0%. A year's rate weighted by capital: 23.1% on 4.75$ over 83 days, 9,319% on 4.70$ over
    # 0.25 days, 233% on 4.70$ over 10 days, and 129% on 4.70$ over 16.9 days.
    assert lines[5] == "  expected to return 1.13$ in profit, 6.0% on capital, 2,419.7% a year"
    # The average is weighted by capital: about a quarter of it on each date, which comes to October 22.
    assert lines[6] == "  resolving first 2026-09-27, on average 2026-10-22, last 2026-12-20"
    # Each one opened in the 12 hours of the window, newest first. The one flattened to nothing is not open.
    assert table(out, "live futures open trades opened in the last 12 hours, newest first") == [
        ["1", "the", "bet", "2026-09-27", "23:30", "5/5", "4.75", "0.25", "5.3", "23.1", "2026-12-20"],
        ["2", "the", "bet", "2026-09-27", "14:00", "5/5", "4.70", "0.30", "6.4", "9,319.1", "2026-09-27"]]


def test_open_trades_keep_to_the_mode_and_sports(tmp_path, monkeypatch, capsys):
    def fill(conn):
        open_trades(conn, "paper", pair_id=1)
        open_trades(conn, "live", pair_id=2)
    out = report(tmp_path, monkeypatch, capsys, fill, modes=summary.MODES["all"], sports=("nba",))
    assert "\npaper futures open trades: none" in out
    assert out.split("\nlive futures open trades: ")[1].startswith("4\n")
    assert [r[:3] for r in table(out, "live futures open trades opened in the last 12 hours, newest first")] == [["6", "other", "bet"], ["7", "other", "bet"]]


def test_open_trades_none_opened_in_the_window_list_no_table(tmp_path, monkeypatch, capsys):
    out = report(tmp_path, monkeypatch, capsys, lambda conn: held_trade(conn, signal=BEFORE, profit=0.25))
    assert "open trades opened in the last" not in out
    # The open trades still close the market, as its last lines.
    assert out.split("\nlive futures open trades: ")[1].splitlines()[:2] == ["1", "  opened in the last hour  0 using 0.00$"]


def test_contract_counts_show_to_the_hundredth_without_float_noise():
    # Matched contracts summed to 0.21000000000000002 in the live outcomes on 2026-10-02.
    assert [summary.contracts(v) for v in (0.21000000000000002, 284532, 630.0, 13.6, None)] == ["0.21", "284,532", "630", "13.6", "0"]


def test_a_long_list_wraps_onto_indented_lines_without_splitting_an_item(capsys):
    sports = [f"sport{i} {i * 1000:,}" for i in range(20)]
    summary.print_listed("5,365 pairs, last matched 2026-10-04 01:12:45 UTC", sports)
    summary.print_listed("feed drops, last 24 hours", [])
    lines = capsys.readouterr().out.splitlines()
    wrapped = lines[1:lines.index("feed drops, last 24 hours")]
    assert lines[0] == "5,365 pairs, last matched 2026-10-04 01:12:45 UTC" and len(wrapped) > 1
    assert all(line.startswith("  ") and len(line) <= summary.WIDTH for line in wrapped)
    assert " ".join(line.strip() for line in wrapped) == ", ".join(sports)
    assert lines[-1] == "  none"


def test_outcomes_show_filled_then_partial_then_failed(tmp_path, monkeypatch, capsys):
    def fill(conn):
        for status, matched in (("failed", 0), ("filled", 5), ("partial", 2)):
            database.insert_trade(conn, Trade(mode="live", pair_id=1, trade="t", signal_ts=INSIDE, edge=0.06, quantity=5,
                                              yes_venue="kalshi", yes_contract="k", yes_polarity="yes", yes_limit=0.5,
                                              no_venue="polymarket_us", no_contract="p", no_polarity="yes", no_limit=0.45,
                                              pays_at="2026-12-20T00:00:00+00:00", yes_held=matched, no_held=matched, matched=matched,
                                              status=status))
    out = report(tmp_path, monkeypatch, capsys, fill)
    assert [r[0] for r in table(out, "live futures by outcome, last 12 hours")] == ["filled", "partial", "failed"]
    assert sorted(["sent", "failed", "partial", "filled", "new"], key=summary.by_status) == ["filled", "partial", "failed", "sent", "new"]


def test_feed_drops_follow_the_pairs_after_a_blank_line(tmp_path, monkeypatch, capsys):
    report(tmp_path, monkeypatch, capsys, lambda conn: None)
    summary.print_gaps(database.read_only(tmp_path / "t.sqlite"), SINCE, 12)
    out = capsys.readouterr().out
    assert out == "\nfeed drops, last 12 hours\n  none\n"


def test_trades_show_the_futures_and_the_games_in_play_apart_with_the_largest_first(tmp_path, monkeypatch, capsys):
    def fill(conn):
        for pair_id, held, cost in ((1, 5, 2.5), (3, 2, 1.0), (3, 8, 4.0)):     # A future, then two trades on a game under way.
            database.insert_trade(conn, Trade(mode="live", pair_id=pair_id, trade="t", signal_ts=INSIDE, edge=0.06, quantity=10,
                                              yes_venue="kalshi", yes_contract="k", yes_polarity="yes", yes_limit=0.5,
                                              no_venue="polymarket_us", no_contract="p", no_polarity="yes", no_limit=0.45,
                                              pays_at="2026-12-20T00:00:00+00:00", yes_held=held, no_held=held,
                                              yes_cost=cost, no_cost=cost, matched=held, profit=0.06 * held, status="partial"))
    out = report(tmp_path, monkeypatch, capsys, fill)
    assert "\nlive futures trades: 1 in all, 1 in the last 12 hours" in out and "\nlive in-play trades: 2 in all, 2 in the last 12 hours" in out
    assert [r[:3] + r[-6:] for r in table(out, "live futures largest trades, last 12 hours")] == [
        ["1", "the", "bet", "5", "10", "5.00", "0.30", "0.30", "2026-12-20"]]
    # The most capital first.
    assert [(r[0], r[-6]) for r in table(out, "live in-play largest trades, last 12 hours")] == [("3", "8"), ("2", "2")]
    assert out.index("\nlive futures open trades: ") < out.index("\n===== in-play =====")


def test_totals_add_up_each_markets_trades_since_the_first_settled_and_open(tmp_path, monkeypatch, capsys):
    def fill(conn):
        # Settled ten days after it opened, balanced: 9.20$ in, 0.80$ back.
        t = held_trade(conn, held=(10, 10), cost=(4.5, 4.7), profit=0.80)
        s = Settlement(t.id, "2026-10-07T11:00:00+00:00", mode="live")
        s.record("yes", "yes", 10.0, s.settled_at)
        s.record("no", "no", 0.0, s.settled_at)
        database.insert_settlement(conn, s)
        held_trade(conn, profit=0.25, pays_at="2026-10-17T11:00:00+00:00")      # Open, 4.75$ expected to make 0.25$ over 20 days.
        held_trade(conn, signal=INSIDE, held=(0, 0), cost=(0, 0), hedge=-0.05, status="failed")   # Flattened to nothing.
        held_trade(conn, held=(5, 5), status="sent")                            # Still in flight, left out.
        held_trade(conn, mode="paper", profit=9.0)                              # Paper's, apart.
        # A game's, settled six hours after it opened, one more on the yes, which won: 1.40$ in, 2.00$ back.
        t = held_trade(conn, pair_id=3, signal=INSIDE, held=(2, 1), cost=(0.9, 0.5), profit=0.05, status="partial")
        s = Settlement(t.id, "2026-09-28T02:00:00+00:00", mode="live")
        s.record("yes", "yes", 2.0, s.settled_at)
        s.record("no", "yes", 0.0, s.settled_at)
        database.insert_settlement(conn, s)
    report(tmp_path, monkeypatch, capsys, fill)
    summary.print_totals(database.read_only(tmp_path / "t.sqlite"), "live", summary.MARKETS, ())
    out = capsys.readouterr().out
    # Futures: 1.00$ on 13.95$ is 7.2%. Its dollar-days are 9.20$ x 10 + 4.75$ x 20 = 187, so a year makes 365 / 187 of it,
    # 195.2%. In play: 0.60$ on 1.40$, 42.9%, over 1.40$ x 0.25 days, 62,571.4% a year.
    assert table(out, "live totals since the first trade, on 2026-09-27, settled and open") == [
        ["futures", "1", "1", "13.95", "1.00", "7.2", "195.2"],
        ["in-play", "1", "0", "1.40", "0.60", "42.9", "62,571.4"]]
    summary.print_totals(database.read_only(tmp_path / "t.sqlite"), "live", summary.MARKETS, ("nba",))
    assert capsys.readouterr().out == "\nlive totals since the first trade: none\n"


def test_a_market_filter_shows_only_the_futures_or_only_the_games_in_play(tmp_path, monkeypatch, capsys):
    out = report(tmp_path, monkeypatch, capsys, lambda conn: None, markets=summary.MARKET_CHOICES["in-play"])
    assert "\nin-play opportunities" in out and "\nlive in-play trades: " in out and "futures" not in out
    out = report(tmp_path / "b", monkeypatch, capsys, lambda conn: None, markets=summary.MARKET_CHOICES["futures"])
    assert "\nfutures opportunities" in out and "\nlive futures trades: " in out and "in-play" not in out


def test_opportunities_on_a_database_the_live_process_has_not_brought_up_to_date_say_so(tmp_path, monkeypatch, capsys):
    def fill(conn):
        conn.execute("ALTER TABLE opportunities DROP COLUMN take_size")     # As before step 13.
    out = report(tmp_path, monkeypatch, capsys, fill, markets=summary.MARKET_CHOICES["futures"])
    assert "), last 12 hours\n  not kept yet, until the live process restarts on this code\n\nlive futures trades: " in out


def test_opportunities_on_a_database_from_before_step_14_are_shown_without_its_polymarket_us_change(tmp_path, monkeypatch, capsys):
    def fill(conn):
        database.insert_opportunities(conn, [episode("2026-09-27T13:00:00+00:00", 2, 0.06, 100)])
        conn.execute("ALTER TABLE opportunities DROP COLUMN pm_changed")    # As before step 14.
    out = report(tmp_path, monkeypatch, capsys, fill, markets=summary.MARKET_CHOICES["futures"])
    assert "), last 12 hours\n  1 episodes could have taken 94$ in one order each and locked in 6.00$\n" in out


def held_trade(conn, mode="live", pair_id=1, signal=BEFORE, held=(5, 5), cost=(2.5, 2.25), profit=0.0, hedge=0.0, status="filled",
               yes=("kalshi", "k", "yes"), no=("polymarket_us", "p", "yes"), pays_at="2026-12-20T00:00:00+00:00"):
    """
    Add a trade of the mode on the pair, opened at signal, holding held on its yes and no legs at cost, each leg on
    (venue, contract, polarity), and return it. By default Kalshi's k holds the bet's yes and Polymarket US's p its no.
    """
    t = Trade(mode=mode, pair_id=pair_id, trade="t", signal_ts=signal, edge=0.06, quantity=max(held),
              yes_venue=yes[0], yes_contract=yes[1], yes_polarity=yes[2], yes_limit=0.5,
              no_venue=no[0], no_contract=no[1], no_polarity=no[2], no_limit=0.45, pays_at=pays_at,
              yes_filled=held[0], no_filled=held[1], yes_held=held[0], no_held=held[1], matched=min(held),
              yes_cost=cost[0], no_cost=cost[1], profit=profit, hedge_pnl=hedge, status=status)
    database.insert_trade(conn, t)
    return t


def test_netting_pairs_a_contracts_yes_with_its_no_on_the_same_venue():
    assert summary.paid_back([
        ("kalshi", "k", "yes", "yes", 20), ("polymarket_us", "p", "yes", "no", 20),         # k's yes and p's no.
        ("polymarket_us", "p", "yes", "yes", 3), ("kalshi", "k", "yes", "no", 2.5),         # p's yes and k's no, a later trade.
        # Through a contract paying on the bet's no: the bet's yes is q's no, and its no q's yes.
        ("polymarket_us", "q", "no", "yes", 4), ("polymarket_us", "q", "no", "no", 1.01),
        ("kalshi", "m", "yes", "yes", 7), ("polymarket_us", "m", "yes", "no", 7),           # One id on two venues, which never net.
        ("kalshi", "n", "yes", "yes", 0.004), ("kalshi", "n", "yes", "no", 9)]) == {        # Under the hundredth contracts trade in.
        "kalshi": 2.5, "polymarket_us": 4.01}


def test_live_open_capital_leaves_out_what_venue_netting_already_paid_back(tmp_path, monkeypatch, capsys):
    def fill(conn, mode="live"):
        open_trades(conn, mode)     # 20 of k's yes on Kalshi and 20 of p's no on Polymarket US, for 18.85$.
        # A later trade the other way round: 3 of p's yes on Polymarket US and 2 of k's no on Kalshi.
        held_trade(conn, mode, held=(3, 2), cost=(1.35, 1.0), profit=0.10,
                   yes=("polymarket_us", "p", "yes"), no=("kalshi", "k", "yes"))
    out = report(tmp_path, monkeypatch, capsys, fill)
    lines = out.split("\nlive futures open trades: ")[1].splitlines()
    # The venues paid back 3 pairs on Polymarket US and 2 on Kalshi, a dollar each, out of the 21.20$ the trades cost.
    assert lines[4] == "  capital 21.20$, 5.00$ of it already paid back by netting, so 16.20$ still held on kalshi 8.50$, polymarket_us 7.70$"
    # 1.23$ on the 16.20$ still held is 7.6%, and the rate a year is on it too: 456.55$ a year on it is 2,818%.
    assert lines[5] == "  expected to return 1.23$ in profit, 7.6% on capital, 2,818.2% a year"
    # Paper's books do not net, so its rates are on the whole 21.20$.
    out = report(tmp_path / "b", monkeypatch, capsys, lambda conn: fill(conn, "paper"), modes=("paper",))
    lines = out.split("\npaper futures open trades: ")[1].splitlines()
    assert lines[4:6] == ["  capital 21.20$, held on kalshi 10.50$, polymarket_us 10.70$",
                          "  expected to return 1.23$ in profit, 5.8% on capital, 2,153.5% a year"]


def test_the_net_adds_the_second_dollar_of_nested_trades_both_of_whose_legs_paid(tmp_path, monkeypatch, capsys):
    def fill(conn):
        for payouts in ((10.0, 10.0), (10.0, 0.0)):          # A nested trade landing between its lines, and one that did not.
            t = held_trade(conn, held=(10, 10), cost=(4.5, 4.7), profit=0.80)
            s = Settlement(t.id, INSIDE, mode="live")
            s.record("yes", "yes", payouts[0], INSIDE)
            s.record("no", "no" if payouts[1] else "yes", payouts[1], INSIDE)
            database.insert_settlement(conn, s)
    out = report(tmp_path, monkeypatch, capsys, fill)
    assert ("\nlive futures trades: 2 in all, 0 in the last 12 hours\n"
            "  net 10.00$: 0.00$ locked in and hedged on the trades opened, 0.00$ on the exposed contracts of 0 trades settled, "
            "10.00$ more on 1 nested trades both of whose legs paid\n") in out


def test_the_net_adds_what_exposed_contracts_made_or_lost_at_settlement_in_the_window(tmp_path, monkeypatch, capsys):
    def fill(conn):
        held_trade(conn, signal=INSIDE, profit=0.30, hedge=-0.05)      # Opened in the window and still open.
        for held, cost, profit, status, payouts, at in (
                ((10, 10), (4.5, 4.7), 0.80, "filled", (10.0, 0.0), INSIDE),     # Balanced, which realizes its 0.80$.
                ((10, 6), (4.5, 2.82), 0.48, "partial", (10.0, 0.0), INSIDE),    # 4 more on the yes, which won: 4 x 0.55$.
                ((10, 6), (4.5, 2.82), 0.48, "partial", (0.0, 6.0), INSIDE),     # The same, but the no won: 4 x -0.45$.
                ((10, 6), (4.5, 2.82), 0.48, "partial", (10.0, 0.0), BEFORE),    # Settled before the window.
                ((3, 0), (1.5, 0.0), 0.0, "failed", (0.0, None), INSIDE)):       # Only the yes, which lost, the no holding nothing.
            t = held_trade(conn, held=held, cost=cost, profit=profit, status=status)
            s = Settlement(t.id, at, mode="live")
            s.record("yes", "no" if payouts[0] == 0 else "yes", payouts[0], at)
            if payouts[1] is not None:
                s.record("no", "no", payouts[1], at)
            database.insert_settlement(conn, s)
        # A game's, settled in the window: 1 more on the yes, which won, 1 x 0.55$.
        t = held_trade(conn, pair_id=3, held=(2, 1), cost=(0.9, 0.5), profit=0.05, status="partial")
        s = Settlement(t.id, INSIDE, mode="live")
        s.record("yes", "yes", 2.0, INSIDE)
        s.record("no", "yes", 0.0, INSIDE)
        database.insert_settlement(conn, s)
    out = report(tmp_path, monkeypatch, capsys, fill)
    # 0.25$ locked in and hedged, and 2.20$ - 1.80$ - 1.50$ at settlement on the three exposed trades settled in the window.
    assert ("\nlive futures trades: 6 in all, 1 in the last 12 hours\n"
            "  net -0.85$: 0.25$ locked in and hedged on the trades opened, -1.10$ on the exposed contracts of 3 trades settled\n") in out
    assert [r[-1] for r in table(out, "live futures by outcome, last 12 hours")] == ["0.25"]    # Its net is what was locked in and hedged.
    # A market with none opened in the window still shows what settled in it.
    assert ("\nlive in-play trades: 1 in all, 0 in the last 12 hours\n"
            "  net 0.55$: 0.00$ locked in and hedged on the trades opened, 0.55$ on the exposed contracts of 1 trades settled\n") in out
    # And one with neither shows no net.
    out = report(tmp_path / "b", monkeypatch, capsys, lambda conn: None)
    assert "\nlive futures trades: 0 in all, 0 in the last 12 hours\n\nlive futures open trades: none" in out and "  net " not in out
