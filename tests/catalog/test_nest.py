"""
Tests for nesting a bet's lines inside each other.
"""

from catalog import nest


def bet(venue, line, kind="total", subject=None, game_date="2026-10-11", team_a="PHI", team_b="JAX", polarity="yes"):
    """
    A bet row as load_bets returns it, on a game by default.
    """
    return {"venue": venue, "contract_id": f"{venue[0]}{line}", "kind": kind, "season": 2027, "game_date": game_date,
            "team_a": team_a, "team_b": team_b, "subject": subject, "line": line, "polarity": polarity,
            "close_time": "2026-10-11T21:00:00+00:00"}


def ladder(*lines, venues=("kalshi", "polymarket_us"), **fields):
    """
    A bet listed at each line on the venues given.
    """
    return [bet(venue, line, **fields) for line in lines for venue in venues]


def nested(bets, sport="nfl", **kw):
    """
    Each nested pair's label and its lines' contracts, the yes side's then the no side's.
    """
    return [(p.label, [b["contract_id"] for b in wide], [b["contract_id"] for b in narrow]) for p, wide, narrow in nest.nest(bets, sport, **kw)]


def test_each_line_nests_with_the_next_two_above_it_yes_on_the_lower_and_no_on_the_higher():
    assert nested(ladder(48.5, 42.5, 45.5, 51.5)) == [
        ("nfl total 2026-10-11 PHI@JAX 42.5 nested 45.5", ["k42.5", "p42.5"], ["k45.5", "p45.5"]),
        ("nfl total 2026-10-11 PHI@JAX 42.5 nested 48.5", ["k42.5", "p42.5"], ["k48.5", "p48.5"]),
        ("nfl total 2026-10-11 PHI@JAX 45.5 nested 48.5", ["k45.5", "p45.5"], ["k48.5", "p48.5"]),
        ("nfl total 2026-10-11 PHI@JAX 45.5 nested 51.5", ["k45.5", "p45.5"], ["k51.5", "p51.5"]),
        ("nfl total 2026-10-11 PHI@JAX 48.5 nested 51.5", ["k48.5", "p48.5"], ["k51.5", "p51.5"])]
    p, _, _ = nest.nest(ladder(42.5, 45.5), "nfl")[0]
    assert (p.kind, p.sport, p.game_date, p.line, p.venues, len(p.members)) == ("total_nested", "nfl", "2026-10-11", 42.5,
                                                                                ["kalshi", "polymarket_us"], 4)


def test_a_line_one_venue_alone_lists_nests_with_the_other_venues_lines_and_two_of_one_venue_do_not():
    # Kalshi lists 42.5 and 44.5, Polymarket US 43.5 alone: no line is on both venues, so there is no pair, but yes over
    # 42.5 on Kalshi with no over 43.5 on Polymarket US, or yes over 43.5 there with no over 44.5 or 46.5 on Kalshi, nests.
    # Kalshi's 42.5 with its own 44.5 or 46.5 would put both legs on one venue, so it nests with 43.5 alone.
    bets = ladder(42.5, 44.5, 46.5, venues=("kalshi",)) + ladder(43.5, venues=("polymarket_us",))
    assert nested(bets) == [("nfl total 2026-10-11 PHI@JAX 42.5 nested 43.5", ["k42.5"], ["p43.5"]),
                            ("nfl total 2026-10-11 PHI@JAX 43.5 nested 44.5", ["p43.5"], ["k44.5"]),
                            ("nfl total 2026-10-11 PHI@JAX 43.5 nested 46.5", ["p43.5"], ["k46.5"])]


def test_only_lines_of_one_bet_nest():
    # Two teams' spreads are two bets, and a season's win total nests with its own lines alone.
    bets = (ladder(3.5, kind="spread", subject="PHI") + ladder(6.5, kind="spread", subject="JAX")
            + ladder(9.5, 10.5, kind="season_wins", subject="SEA", game_date=None, team_a=None, team_b=None))
    assert [label for label, _, _ in nested(bets)] == ["nfl season_wins 2027 SEA 9.5 nested 10.5"]


def test_only_half_lines_of_kinds_going_over_the_line_nest():
    assert nested(ladder(2, 3, kind="round_of_victory", subject="jones"), "ufc") == []          # A round a fight ends in.
    assert nested(ladder(1.5, 2.5, kind="round_of_victory", subject="jones"), "ufc") == []
    assert nested(ladder(46, 48)) == []                                                        # A number could land on them.
    assert all(nest.nests_kind(kind) for kind in ("spread", "total", "first_half_spread", "team_total", "total_maps",
                                                    "player_receiving_yards", "season_wins", "games_spread"))
    assert not any(nest.nests_kind(kind) for kind in ("game_winner", "hit_before", "dip_before", "round_of_victory", "champion"))


def test_a_match_the_venues_date_a_day_apart_nests_as_one():
    kalshi = ladder(20.5, venues=("kalshi",), kind="total_games", game_date="2026-10-03", team_a="lu", team_b="li")
    polymarket_us = ladder(21.5, venues=("polymarket_us",), kind="total_games", game_date="2026-10-02", team_a="lu", team_b="li")
    assert nested(kalshi + polymarket_us, "tennis") == [("tennis total_games 2026-10-02 lu@li 20.5 nested 21.5", ["k20.5"], ["p21.5"])]


def test_a_game_played_before_the_day_given_is_not_nested_and_a_future_always_is():
    bets = ladder(42.5, 45.5) + ladder(9.5, 10.5, kind="season_wins", subject="SEA", game_date=None, team_a=None, team_b=None)
    assert [label for label, _, _ in nested(bets, since="2026-10-12")] == ["nfl season_wins 2027 SEA 9.5 nested 10.5"]
    assert len(nested(bets, since="2026-10-11")) == 2
