"""
Nest the lines of one bet inside each other, so that a ladder of lines
is priced and traded as well as each line alone.

The catalog states every bet with a line as a number of its subject's
going over the line: a game's points, the margin of the side a spread
names, a player's yards or goals, a team's wins in a season. Of two lines
of one bet, then, going over the higher means going over the lower too,
and holding yes on the lower line with no on the higher pays at least a
dollar a pair whatever happens: the lower line's yes when the number goes
over both, the higher line's no when it goes over neither, and both, two
dollars, when it lands between them. Over 46.5 points with not over 48.5
pays two dollars on 47 or 48 points and one on any other score. So when
the lower line's yes and the higher line's no cost less than a dollar
together, fees included, the difference is locked in as surely as on the
two sides of one line, and the chance of the second dollar comes free.

A nested pair is stored as a pair of its own, with an id the trades and
episodes refer to, a kind of the bet's kind with '_nested' after it, and
a label of the lower line's bet with the higher line after it, 'nfl total
2026-10-11 PHI@JAX 42.5 nested 45.5'. Its members are the contracts of
the two lines on either venue, each kept in the nest_members table with
the side it supplies: the lower line's the yes, the higher line's the no,
see database.load_pairs() and pricing.best_trade(). Its legs are always
on two venues, as on a game's pair, since two lines of one venue are
priced by the same traders.

A line nests whether both venues list it or one alone does, which the
venues often do, one listing 42.5 and 44.5 and the other 43.5: a pair
needs the same line on both venues, a nested pair only a line on each, so
a line one venue alone lists is recorded for its nested pairs, see
database.load_recording_targets(). Each line is nested with the next STEPS
lines above it that give its legs two venues, on the futures and on the
games from the day before the catalog's refresh: the further apart two
lines are, the likelier the number lands between them and the dearer the
pair, so an edge across a wide gap is rare. Only lines ending in a half
are nested, so that no number lands on one and no venue's rule for a push
comes into it, and only kinds whose statement is a number going over the
line, see nests_kind(). A match the venues date a day apart is one match,
as when pairing, see match.near_dates(). From 2026-10-12, at the user's
asking: that day the lines both venues list made 11,325 nested pairs,
and with the lines one venue alone lists 23,178, 3,003 of them futures,
for which some 4,000 more Kalshi contracts and 3,000 more Polymarket US
ones are recorded.
"""

from collections import defaultdict
from catalog import match

STEPS = 2           # How many lines above it, of those giving its legs two venues, each line is nested with.
NESTED = "_nested"  # What a nested pair's kind adds to its bet's kind.
FAMILY = ("kind", "season", "game_date", "team_a", "team_b", "subject")     # A bet's identity but for its line.


def nests_kind(kind):
    """
    Whether a kind's statement is its subject's number going over the line,
    which nests one line inside another: a spread or total of any part of a
    game, a total of its maps, games, or corners, a player's number, or a
    season's. Not a round a fight ends in, or a price Bitcoin touches by a
    time, whose lines do not nest.
    """
    return (kind in ("spread", "total") or kind.endswith(("_spread", "_total"))
            or kind.startswith(("total_", "player_", "season_")))


def half(line):
    """
    Whether a line ends in a half, so no number lands on it.
    """
    return abs(line % 1 - 0.5) < 1e-9


def family(bet):
    """
    The bet a line belongs to: its identity but for the line.
    """
    return tuple(bet[f] for f in FAMILY)


def nest(bets, sport, since=None):
    """
    The nested pairs of a sport's bet rows, as load_bets gives them, see
    the module docstring, as [(nested Pair, lower line's bet rows, higher
    line's bet rows)], given since, a date, only those of the futures and
    of the games on or after it, as a game played before cannot trade. The
    nested pair's members are both lines' bets, for the venues and the count
    it is stored with, and its flags are the notes on them, see
    match.flags().
    """
    if sport in match.NEAR_DATE_SPORTS:
        bets = match.near_dates(bets)
    ladders = defaultdict(lambda: defaultdict(list))        # The bet's family maps each of its lines to the bets on it.
    for b in bets:
        if b["line"] is None or not nests_kind(b["kind"]) or not half(b["line"]):
            continue
        if since and b["game_date"] and b["game_date"] < since:
            continue
        ladders[family(b)][b["line"]].append(b)
    nested = []
    for lines in ladders.values():
        ordered = sorted(lines)
        for i, low in enumerate(ordered):
            wide = lines[low]
            higher = [high for high in ordered[i + 1:] if len({b["venue"] for b in wide + lines[high]}) > 1]
            for high in higher[:STEPS]:
                narrow = lines[high]
                pair = match.make_pair(wide + narrow, sport)        # As the lower line's pair, but for the nest's label and kind.
                pair.label, pair.kind = f"{pair.label} nested {high}", pair.kind + NESTED
                nested.append((pair, wide, narrow))
    return nested
