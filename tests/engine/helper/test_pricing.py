"""
Tests for pricing one side of a bet through a contract and both sides across a group.
"""

import pytest
from db.models import Book
from engine.helper import config, pricing

PM_FEES = {"feeCoefficient": 0.0695}
NO_PM_FEES = {"feeCoefficient": 0}
NO_K_FEES = {"fee_type": "quadratic", "fee_multiplier": 0}
REAL_K_FEES = {"fee_type": "quadratic", "fee_multiplier": 1}


def member(venue, contract_id, polarity="yes"):
    return {"venue": venue, "contract_id": contract_id, "polarity": polarity}


def test_ladder_depends_on_which_side_the_contract_pays():
    q = Book("kalshi", "k", "t", bids=[[0.53, 100]], asks=[[0.54, 50]])
    assert pricing.ladder(q, "yes", "yes") == [(0.54, 50)]     # Hold yes through a yes contract, buy it.
    assert pricing.ladder(q, "yes", "no") == [(0.47, 100)]     # Hold no through a yes contract, buy the other side.
    assert pricing.ladder(q, "no", "no") == [(0.54, 50)]       # Hold no through a no contract, buy it.
    assert pricing.ladder(q, "no", "yes") == [(0.47, 100)]


@pytest.mark.parametrize("polarity, side", [("yes", "yes"), ("yes", "no"), ("no", "no"), ("no", "yes")])
@pytest.mark.parametrize("selling", [False, True])
def test_book_level_finds_the_book_level_each_ladder_level_came_from(polarity, side, selling):
    q = Book("kalshi", "k", "t", bids=[[0.53, 100], [0.52, 10]], asks=[[0.54, 50], [0.57, 5]])
    levels = (pricing.sell_ladder if selling else pricing.ladder)(q, polarity, side)
    found = [(*pricing.book_level(polarity, side, price, selling), size) for price, size in levels]
    book_side = found[0][0]
    assert book_side == ("bids" if selling == (side == polarity) else "asks")
    assert [(price, size) for _, price, size in found] == [tuple(level) for level in getattr(q, book_side)]


def test_depth_stops_where_the_edge_falls_under_the_floor():
    fee = ("polymarket_us", {"feeCoefficient": 0})
    leg_a = [(0.45, 1), (0.46, 100), (0.50, 100)]     # Hold yes: 8, 7, then 3 cents against 0.47 on the other leg.
    leg_b = [(0.47, 100)]
    assert pricing.depth(leg_a, leg_b, fee, fee, 0.05) == (0.46, 0.47, 100)      # The third level is under 5 cents.
    assert pricing.depth(leg_a, leg_b, fee, fee, 0.08) == (0.45, 0.47, 1)        # Only the top level clears 8 cents.
    assert pricing.depth(leg_a, leg_b, fee, fee, 0.09) == (None, None, 0)        # Nothing does.
    assert pricing.depth(leg_a, leg_b, fee, fee, 0.05, most=1) == (0.45, 0.47, 1)       # The top level holds the one asked for.
    assert pricing.depth(leg_a, leg_b, fee, fee, 0.05, most=5) == (0.46, 0.47, 100)     # Five need the second.


def test_the_edge_for_a_return_a_year_returns_just_that():
    assert pricing.edge_for_annual(50, 365) == pytest.approx(1 / 3)       # A third of a dollar on the two thirds both legs cost, in a year.
    for days in (1 / 24, 4.25, 27.25, 180):
        assert pricing.annual_pct(pricing.edge_for_annual(50, days), days) == pytest.approx(50)


def test_live_takes_its_in_play_edge_on_a_game_under_way_and_on_a_future_the_edge_returning_its_rate_a_year():
    assert pricing.live_min_edge(True, 0.1) == config.LIVE_IN_PLAY_MIN_EDGE
    assert pricing.live_min_edge(False, 27.25) == pytest.approx(0.0695, abs=0.0001)    # 6.95 cents returns 100% a year over 27 days.
    assert pricing.annual_pct(pricing.live_min_edge(False, 27.25), 27.25) == pytest.approx(config.MIN_ANNUAL_PCT)
    assert pricing.live_min_edge(False, None) == config.MIN_EDGE          # A bet that gives no payout.


def test_live_holds_an_edge_until_it_has_lasted_a_tenth_of_a_second():
    since = "2026-09-22T17:59:59.960000+00:00"                        # When the edge reached live's least edge.
    assert pricing.live_hold(since, "2026-09-22T18:00:00+00:00") == pytest.approx(0.06)
    assert pricing.live_hold(since, "2026-09-22T18:00:00.060000+00:00") == 0             # A tenth of a second on.
    assert pricing.live_hold(since, "2026-09-22T18:00:05+00:00") == 0


def test_polymarket_us_just_changed_only_when_its_book_reached_us_at_the_pricing():
    k, pm = member("kalshi", "k"), member("polymarket_us", "us")
    now = "2026-09-22T18:00:00+00:00"
    books = {("kalshi", "k"): Book("kalshi", "k", now, [[0.53, 10]], [[0.54, 10]]),
             ("polymarket_us", "us"): Book("polymarket_us", "us", "2026-09-22T17:59:59.900000+00:00", [[0.44, 10]], [[0.45, 10]])}
    assert not pricing.polymarket_us_just_changed(pm, k, books, now)     # Kalshi's change brought this pricing.
    assert pricing.polymarket_us_just_changed(k, pm, books, "2026-09-22T17:59:59.900000+00:00")
    assert not pricing.polymarket_us_just_changed(pm, k, {}, now)        # No book at all.
    assert pricing.polymarket_us_just_changed(k, member("kalshi", "k2"), books, now)     # No leg there.


def test_fillable_counts_the_contracts_and_profit_on_the_levels_at_the_floor_or_more():
    members = [member("kalshi", "k"), member("polymarket_us", "us")]
    # Yes costs 0.40 on Polymarket US. No costs 0.47 on Kalshi for 40, 13 cents, 0.57 for 60 more, 3 cents, and 0.59 for
    # 50 more, 1 cent.
    books = {("kalshi", "k"): Book("kalshi", "k", "t", [[0.53, 40], [0.43, 60], [0.41, 50]], [[0.99, 1]]),
             ("polymarket_us", "us"): Book("polymarket_us", "us", "t", [[0.39, 200]], [[0.40, 200]])}
    fee_infos = {("kalshi", "k"): NO_K_FEES, ("polymarket_us", "us"): NO_PM_FEES}
    yes, no = members[1], members[0]
    assert pricing.fillable(yes, no, books, fee_infos, 0.05) == pytest.approx((40, 5.2))
    assert pricing.fillable(yes, no, books, fee_infos, 0.02) == pytest.approx((100, 7.0))
    assert pricing.fillable(yes, no, books, fee_infos, 0.005) == pytest.approx((150, 7.5))
    assert pricing.fillable(yes, no, books, fee_infos, 0.14) == (0.0, 0.0)


def test_positive_depth_walks_both_ladders_while_the_edge_is_positive():
    leg_a = [(0.40, 10), (0.41, 10)]
    leg_b = [(0.50, 5), (0.58, 100)]
    fee = ("polymarket_us", NO_PM_FEES)
    top_edge, size, profit, worth_size, worth_profit = pricing.positive_depth(leg_a, leg_b, fee, fee, 0.05)
    assert top_edge == pytest.approx(0.10)
    assert size == 20
    assert profit == pytest.approx(5 * 0.10 + 5 * 0.02 + 10 * 0.01)
    assert (worth_size, worth_profit) == (5, pytest.approx(5 * 0.10))     # Only the top step is worth 5 cents.


def test_the_levels_worth_the_minimum_edge_stop_at_the_first_one_under_it():
    fee = ("polymarket_us", NO_PM_FEES)
    # Steps of 10, then 2, then 8 cents: the 8 below the 2 is not counted, since an order sweeps from the top down.
    _, size, _, worth_size, worth_profit = pricing.positive_depth([(0.40, 5), (0.48, 5), (0.40, 5)], [(0.50, 15)], fee, fee, 0.05)
    assert (size, worth_size, worth_profit) == (15, 5, pytest.approx(0.5))


def test_positive_depth_stops_at_zero_edge_and_handles_empty_ladders():
    fee = ("polymarket_us", NO_PM_FEES)
    assert pricing.positive_depth([(0.5, 10)], [(0.5, 10)], fee, fee, 0.05) == (0.0, 0.0, 0.0, 0.0, 0.0)
    assert pricing.positive_depth([], [(0.5, 10)], fee, fee, 0.05) == (-1.0, 0.0, 0.0, 0.0, 0.0)


def test_positive_depth_leaves_the_ladders_as_they_were():
    leg_a, leg_b = [(0.40, 10)], [(0.50, 5)]
    fee = ("polymarket_us", NO_PM_FEES)
    pricing.positive_depth(leg_a, leg_b, fee, fee, 0.05)
    assert (leg_a, leg_b) == ([(0.40, 10)], [(0.50, 5)])


def test_sweep_takes_a_share_of_each_level_skips_levels_too_small_and_stops_at_the_limit():
    levels = [(0.40, 1), (0.41, 10), (0.45, 100), (0.60, 100)]
    fee = ("polymarket_us", NO_PM_FEES)
    # Half of 1 is no whole contract, so the top level is skipped. Then 5 at 0.41 and the rest at 0.45, and 0.60 is over the limit.
    assert pricing.sweep(levels, 30, *fee, share=0.5, limit=0.50) == (30, pytest.approx(5 * 0.41 + 25 * 0.45))
    assert pricing.sweep(levels, 500, *fee, share=0.5, limit=0.50) == (55, pytest.approx(5 * 0.41 + 50 * 0.45))
    # Selling takes the fee off the proceeds instead of adding it.
    pm = ("polymarket_us", {"feeCoefficient": 0.0695})
    bought, sold = pricing.sweep([(0.5, 10)], 10, *pm), pricing.sweep([(0.5, 10)], 10, *pm, selling=True)
    assert bought[1] - 5.0 == pytest.approx(5.0 - sold[1]) and bought[1] > 5.0


def test_a_sales_limit_is_the_least_it_takes():
    bids = [(0.44, 2), (0.43, 2), (0.40, 50)]                         # A sell ladder, best first.
    assert list(pricing.takes(bids, 10, limit=0.43, selling=True)) == [(0.44, 2), (0.43, 2)]
    assert pricing.sweep(bids, 10, "polymarket_us", NO_PM_FEES, limit=0.43, selling=True) == (4, pytest.approx(2 * 0.44 + 2 * 0.43))


def test_reach_is_the_deepest_level_a_sweep_takes_from():
    buying = [(0.40, 1), (0.41, 10), (0.45, 100), (0.60, 100)]
    assert pricing.reach(buying, 5, share=0.5) == 0.41            # The 1-lot gives half a contract, so the 0.41 level fills all 5.
    assert pricing.reach(buying, 30, share=0.5) == 0.45
    assert pricing.reach([(0.44, 100), (0.40, 100)], 80, share=0.5) == 0.40     # A sale reaches down to its lowest price.
    assert pricing.reach([(0.40, 1)], 5, share=0.5) is None


def test_best_trade_picks_the_cheapest_leg_on_each_side_across_venues():
    members = [member("kalshi", "k"), member("polymarket_us", "us")]
    books = {("kalshi", "k"): Book("kalshi", "k", "t", [[0.53, 100]], [[0.54, 100]]),
              ("polymarket_us", "us"): Book("polymarket_us", "us", "t", [[0.44, 100]], [[0.45, 100]])}
    fee_infos = {("kalshi", "k"): NO_K_FEES, ("polymarket_us", "us"): NO_PM_FEES}
    yes, no, edge, size, profit, worth_size, _ = pricing.best_trade(members, books, fee_infos)
    # Yes is cheapest at Polymarket's 0.45 ask. No is cheapest at Kalshi, one minus its 0.53 bid.
    assert (yes["venue"], no["venue"]) == ("polymarket_us", "kalshi")
    assert edge == pytest.approx(1 - 0.45 - 0.47)
    assert size == worth_size == 100


def test_best_trade_uses_a_no_contract_for_yes_exposure():
    members = [member("kalshi", "k_yes", "yes"), member("polymarket_us", "us_no", "no")]
    books = {("kalshi", "k_yes"): Book("kalshi", "k_yes", "t", [[0.40, 100]], [[0.60, 100]]),
              ("polymarket_us", "us_no"): Book("polymarket_us", "us_no", "t", [[0.55, 100]], [[0.70, 100]])}
    fee_infos = {("kalshi", "k_yes"): NO_K_FEES, ("polymarket_us", "us_no"): NO_PM_FEES}
    yes, no, edge, *_ = pricing.best_trade(members, books, fee_infos)
    # Yes through the no contract's bid costs 0.45, cheaper than the yes contract's 0.60 ask.
    # No through the yes contract's bid costs 0.60, cheaper than the no contract's 0.70 ask.
    assert (yes["contract_id"], no["contract_id"]) == ("us_no", "k_yes")
    assert edge == pytest.approx(1 - 0.45 - 0.60)


def test_best_trade_never_puts_both_legs_on_one_venue():
    # k_no is cheapest on both sides, and k_yes, also on Kalshi, holds no for less than Polymarket US does, but the legs go
    # on two venues: k_no with Polymarket US on whichever side works out better, here no at one minus its 0.38 bid.
    members = [member("kalshi", "k_yes", "yes"), member("kalshi", "k_no", "no"), member("polymarket_us", "us", "yes")]
    books = {("kalshi", "k_yes"): Book("kalshi", "k_yes", "t", [[0.40, 100]], [[0.60, 100]]),
              ("kalshi", "k_no"): Book("kalshi", "k_no", "t", [[0.55, 100]], [[0.56, 100]]),
              ("polymarket_us", "us"): Book("polymarket_us", "us", "t", [[0.38, 100]], [[0.58, 100]])}
    fee_infos = {("kalshi", "k_yes"): NO_K_FEES, ("kalshi", "k_no"): NO_K_FEES, ("polymarket_us", "us"): NO_PM_FEES}
    yes, no, edge, *_ = pricing.best_trade(members, books, fee_infos)
    assert (yes["contract_id"], no["contract_id"]) == ("k_no", "us")
    assert edge == pytest.approx(1 - 0.45 - 0.62)
    # Without a member on another venue there is no trade at all.
    assert pricing.best_trade(members[:2], books, fee_infos) is None


def test_on_a_future_both_legs_may_be_two_contracts_of_one_venue_with_the_same_rules():
    # Polymarket US lists a win total twice, under the same rules: yes at 0.40 on one and no at 1 - 0.53 on the other, 13 cents.
    members = [dict(member("polymarket_us", "pm"), rules_digest="r"), dict(member("polymarket_us", "pm2"), rules_digest="r"),
               member("kalshi", "k")]
    books = {("polymarket_us", "pm"): Book("polymarket_us", "pm", "t", [[0.39, 100]], [[0.40, 100]]),
             ("polymarket_us", "pm2"): Book("polymarket_us", "pm2", "t", [[0.53, 100]], [[0.54, 100]]),
             ("kalshi", "k"): Book("kalshi", "k", "t", [[0.45, 100]], [[0.62, 100]])}
    fee_infos = {("polymarket_us", "pm"): NO_PM_FEES, ("polymarket_us", "pm2"): NO_PM_FEES, ("kalshi", "k"): NO_K_FEES}
    yes, no, edge, *_ = pricing.best_trade(members, books, fee_infos, one_venue=True)
    assert (yes["contract_id"], no["contract_id"], edge) == ("pm", "pm2", pytest.approx(0.13))
    # Without one_venue, as on a game, the no leg goes to Kalshi, at 1 - 0.45.
    yes, no, edge, *_ = pricing.best_trade(members, books, fee_infos)
    assert (yes["contract_id"], no["contract_id"], edge) == ("pm", "k", pytest.approx(0.05))
    # Rules that differ, or none, keep the legs on two venues.
    for other in ("other rules", None):
        members[1]["rules_digest"] = other
        assert pricing.best_trade(members, books, fee_infos, one_venue=True)[1]["contract_id"] == "k"


def test_a_nested_pair_takes_yes_on_the_lower_line_and_no_on_the_higher_on_two_venues():
    # Over 9.5 wins and over 10.5 wins, a season's two lines on each venue, as database.load_pairs() gives them.
    members = [dict(member(v, f"{v[0]}{line}"), side=side, rules_digest="r")
               for line, side in ((9.5, "yes"), (10.5, "no")) for v in ("kalshi", "polymarket_us")]
    books = {("kalshi", "k9.5"): Book("kalshi", "k9.5", "t", [[0.37, 100]], [[0.38, 100]]),
             ("polymarket_us", "p9.5"): Book("polymarket_us", "p9.5", "t", [[0.39, 100]], [[0.40, 100]]),
             ("kalshi", "k10.5"): Book("kalshi", "k10.5", "t", [[0.45, 100]], [[0.47, 100]]),
             ("polymarket_us", "p10.5"): Book("polymarket_us", "p10.5", "t", [[0.35, 100]], [[0.36, 100]])}
    fee_infos = {key: NO_K_FEES if key[0] == "kalshi" else NO_PM_FEES for key in books}
    # Yes over 9.5 at Polymarket US's 0.40 and no over 10.5 at one minus Kalshi's 0.45 bid lock in 5 cents. Yes over 10.5
    # at 0.36 is cheaper, but holds the wrong side of the nest, and yes over 9.5 on Kalshi at 0.38 with Kalshi's no over
    # 10.5 would lock in 7 cents with both legs on one venue, which a nested pair never has, even on a future.
    for one_venue in (False, True):
        yes, no, edge, *_ = pricing.best_trade(members, books, fee_infos, one_venue=one_venue)
        assert (yes["contract_id"], no["contract_id"], edge) == ("p9.5", "k10.5", pytest.approx(0.05))


def test_same_rules_needs_two_contracts_paying_on_one_side_with_rules():
    a, b = dict(member("polymarket_us", "pm"), rules_digest="r"), dict(member("polymarket_us", "pm2"), rules_digest="r")
    assert pricing.same_rules(a, b)
    assert not pricing.same_rules(a, a)                                         # One contract is no pair.
    assert not pricing.same_rules(a, dict(b, polarity="no"))                    # Paying on opposite sides.
    assert not pricing.same_rules(dict(a, rules_digest=None), dict(b, rules_digest=None))


def test_best_trade_ignores_a_crossed_book_with_no_partner():
    # A lone book whose bid is above its ask is a feed glitch, not free money.
    members = [member("polymarket_us", "pm", "yes")]
    books = {("polymarket_us", "pm"): Book("polymarket_us", "pm", "t", [[0.46, 100]], [[0.36, 100]])}
    assert pricing.best_trade(members, books, {("polymarket_us", "pm"): NO_PM_FEES}) is None


def test_best_trade_returns_none_without_two_quoted_sides():
    members = [member("kalshi", "k")]
    books = {("kalshi", "k"): Book("kalshi", "k", "t", [[0.53, 100]], [])}
    assert pricing.best_trade(members, books, {("kalshi", "k"): NO_K_FEES}) is None


def test_fees_are_charged_once_per_level_on_buys_and_sells():
    kalshi = ("kalshi", {"fee_type": "quadratic", "fee_multiplier": 1})
    assert pricing.sweep([(0.5, 100)], 100, *kalshi) == (100, pytest.approx(50 + 1.75))                  # Not 100 fees of 2 cents.
    assert pricing.sweep([(0.5, 100)], 100, *kalshi, selling=True) == (100, pytest.approx(50 - 1.75))
    # Two levels are two trades, each rounded on its own, up to the hundredth of a cent on Kalshi, and to the cent on Polymarket US.
    assert pricing.sweep([(0.5, 10), (0.6, 10)], 20, *kalshi) == (20, pytest.approx(5 + 0.175 + 6 + 0.168))
    polymarket_us = ("polymarket_us", {"feeCoefficient": 0.0695})
    assert pricing.sweep([(0.5, 10), (0.6, 10)], 20, *polymarket_us) == (20, pytest.approx(5 + 0.17 + 6 + 0.17))


def test_edges_use_the_unrounded_fee_per_contract():
    kalshi = ("kalshi", {"fee_type": "quadratic", "fee_multiplier": 1})
    edge, size, profit, worth_size, _ = pricing.positive_depth([(0.45, 100)], [(0.47, 100)], kalshi, kalshi, 0.05)
    expected = 1 - 0.45 - 0.47 - 0.07 * 0.45 * 0.55 - 0.07 * 0.47 * 0.53
    assert edge == pytest.approx(expected)          # About 4.53 cents. Rounding each fee up to 2 cents would have said 4.
    assert (size, profit, worth_size) == (100, pytest.approx(100 * expected), 0)     # Under 5 cents.


def test_a_book_whose_market_is_not_trading_is_no_book():
    book = Book("kalshi", "K", "2026-10-07T08:41:50+00:00", [[0.5, 1]], [[0.6, 1]], halted="paused")
    assert not pricing.fresh(book, "2026-10-07T08:41:51+00:00") and not pricing.fresh(book, "2026-10-07T08:41:51+00:00", aging=False)
    assert pricing.fresh(Book("kalshi", "K", "2026-10-07T08:41:50+00:00", [[0.5, 1]], [[0.6, 1]]), "2026-10-07T08:41:51+00:00")
