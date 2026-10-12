"""
Golden cases for the Kalshi classifier, built from real contract rows.
"""

from catalog.classify import kalshi


def row(series, event, ticker, **fields):
    """
    A Kalshi contract row with the fields the classifier reads.
    """
    r = {"venue": "kalshi", "sport": "nfl", "series_id": series, "event_id": event, "contract_id": ticker,
         "title": "", "outcome": "", "market_type": None, "line": None, "start_time": None, "close_time": None}
    r.update(fields)
    return r


def bet_fields(bet):
    """
    The fields that matter for matching, as a tuple.
    """
    return (bet.kind, bet.season, bet.game_date, bet.team_a, bet.team_b, bet.subject, bet.line, bet.polarity)


def test_split_codes_handles_codes_of_any_length_and_refuses_to_guess():
    assert kalshi.split_codes("PHIATL", "mlb") == ("PHI", "ATL")
    assert kalshi.split_codes("SDMIL", "mlb") == ("SD", "MIL")
    assert kalshi.split_codes("XXYY", "mlb") == (None, None)


def future(series, event, ticker, close, **fields):
    return kalshi.classify(row(series, event, ticker, close_time=close, **fields))


def test_team_futures_name_the_team_and_take_the_season_their_settlement_falls_in():
    assert bet_fields(future("KXSB", "KXSB-27", "KXSB-27-KC", "2027-02-14T23:30:00+00:00")) == (
        "champion", 2027, None, None, None, "KC", None, "yes")
    assert bet_fields(future("KXNFL1SEED", "KXNFL1SEED-AFC26", "KXNFL1SEED-AFC26-BAL", "2027-01-11T15:00:00+00:00")) == (
        "conf_top_seed", 2027, None, None, None, "BAL", None, "yes")      # The event names the year the season starts.
    assert future("KXNFLAFCEAST", "KXNFLAFCEAST-27", "KXNFLAFCEAST-27-BUF", "2027-01-11T15:00:00+00:00").kind == "division_champion"
    assert bet_fields(future("KXMLB", "KXMLB-26", "KXMLB-26-LAD", "2026-11-01T04:00:00+00:00", sport="mlb"))[:6] == (
        "champion", 2026, None, None, None, "LAD")
    assert bet_fields(future("KXNCAAFACC", "KXNCAAFACC-26", "KXNCAAFACC-26-CLEM", "2026-12-06T15:00:00+00:00", sport="ncaaf"))[:6] == (
        "conf_champion", 2027, None, None, None, "CLEM")                  # A college season ends the year after it starts.
    assert future("KXNCAAFSECQ", "KXNCAAFSECQ-26", "KXNCAAFSECQ-26-UGA", "2026-12-06T04:00:00+00:00", sport="ncaaf").kind == "reach_conf_title_game"
    assert future("KXNHL", "KXNHL-27", "KXNHL-27-TB", "2027-07-01T14:00:00+00:00", sport="nhl").subject == "TBL"


def test_playoff_rounds_and_series_winners():
    conf = future("KXNFLROUNDQUAL", "KXNFLROUNDQUAL-27CONF", "KXNFLROUNDQUAL-27CONF-BAL", "2027-01-25T15:00:00+00:00")
    div = future("KXNFLROUNDQUAL", "KXNFLROUNDQUAL-27DIV", "KXNFLROUNDQUAL-27DIV-BAL", "2027-01-25T15:00:00+00:00")
    series = future("KXMLBSERIES", "KXMLBSERIES-26PHIATLWC", "KXMLBSERIES-26PHIATLWC-PHI", "2026-10-14T18:00:00+00:00", sport="mlb")
    assert (conf.kind, div.kind) == ("reach_conf_final", "reach_divisional_round")
    assert bet_fields(series) == ("wild_card_series", 2026, None, "ATL", "PHI", "PHI", None, "yes")     # The teams in a fixed order.
    assert future("KXMLBSERIES", "KXMLBSERIES-26PHIATLDS", "KXMLBSERIES-26PHIATLDS-PHI", "2026-10-14T18:00:00+00:00", sport="mlb") is None


def test_awards_name_the_player_from_the_subtitle():
    judge = future("KXMLBALMVP", "KXMLBALMVP-26", "KXMLBALMVP-26-AJUD", "2026-12-08T15:00:00+00:00", sport="mlb", outcome="Aaron Judge")
    witt = future("KXMLBALMVP", "KXMLBALMVP-26", "KXMLBALMVP-26-RWIT", "2026-12-08T15:00:00+00:00", sport="mlb", outcome="Bobby Witt Jr.")
    assert bet_fields(judge) == ("al_mvp", 2026, None, None, None, "aaron judge", None, "yes")
    assert witt.subject == "bobby witt"
    assert future("KXNFLMVP", "KXNFLMVP-27", "KXNFLMVP-27-X", "2027-03-14T15:00:00+00:00", outcome="") is None


def test_season_totals_name_the_team_from_the_event_and_keep_the_strict_line():
    wins = future("KXNFLWINS", "KXNFLWINS-27ARI", "KXNFLWINS-27ARI-2", "2027-01-18T05:00:00+00:00", line=1.5)
    points = future("KXNHLSEASONPTS", "KXNHLSEASONPTS-27ANA", "KXNHLSEASONPTS-27ANA-70", "2027-04-18T14:00:00+00:00", sport="nhl", line=69.5)
    assert bet_fields(wins) == ("season_wins", 2027, None, None, None, "ARI", 1.5, "yes")
    assert bet_fields(points) == ("season_points", 2027, None, None, None, "ANA", 69.5, "yes")


def test_skips_games_unknown_series_and_missing_lines_and_settlement_times():
    assert kalshi.classify(row("KXNFLGAME", "KXNFLGAME-26SEP20CARXYZ", "KXNFLGAME-26SEP20CARXYZ-XYZ")) is None    # No such team.
    assert kalshi.classify(row("KXNFLRECYDS", "KXNFLRECYDS-26SEP20", "KXNFLRECYDS-26SEP20-X")) is None
    assert future("KXNFLWINS", "KXNFLWINS-27BUF", "KXNFLWINS-27BUF-10", "2027-01-18T05:00:00+00:00") is None
    assert kalshi.classify(row("KXSB", "KXSB-27", "KXSB-27-KC")) is None       # No close time, so no season.
    assert future("KXSB", "KXSB-27", "KXSB-27-XYZ", "2027-02-14T23:30:00+00:00") is None


def test_leaders_and_title_holders_name_the_person_and_their_event_the_stat():
    yards = future("KXLEADERNFLRYDS", "KXLEADERNFLRYDS-27", "KXLEADERNFLRYDS-27-JSMITHNJIGBA11", "2027-02-01T15:00:00+00:00",
                   outcome="Jaxon Smith-Njigba")
    boot = future("KXEPLLEADER", "KXEPLLEADER-27GOAL", "KXEPLLEADER-27GOAL-EHAALA9", "2027-06-01T15:00:00+00:00", sport="epl",
                  outcome="Erling Haaland")
    sec = future("KXNCAAFSECLEADER", "KXNCAAFSECLEADER-26PASSYDS", "KXNCAAFSECLEADER-26PASSYDS-MISSTCHA", "2026-12-31T15:00:00+00:00",
                 sport="ncaaf", outcome="Trinidad Chambliss")
    homers = future("KXMLBLEADERPLAYOFF", "KXMLBLEADERPLAYOFF-26HR", "KXMLBLEADERPLAYOFF-26HR-KTUCKER30", "2026-12-01T15:00:00+00:00",
                    sport="mlb", outcome="Kyle Tucker")
    ufc = future("KXUFCLHEAVYWEIGHTTITLE", "KXUFCLHEAVYWEIGHTTITLE-26", "KXUFCLHEAVYWEIGHTTITLE-26-CULB", "2026-12-31T17:00:00+00:00",
                 sport="ufc", outcome="Carlos Ulberg")
    f1 = future("KXF1", "KXF1-26", "KXF1-26-KA", "2026-12-08T15:00:00+00:00", sport="f1", outcome="Andrea Kimi Antonelli")
    assert bet_fields(yards) == ("receiving_yards_leader", 2027, None, None, None, "jaxon smith njigba", None, "yes")
    assert (boot.kind, boot.subject) == ("goals_leader", "erling haaland")
    assert (sec.kind, sec.season) == ("sec_passing_yards_leader", 2027)
    assert (homers.kind, homers.season) == ("postseason_home_runs_leader", 2026)
    assert (ufc.kind, ufc.season, f1.kind, f1.subject) == ("light_heavyweight_champion", 2027, "drivers_champion", "kimi antonelli")
    assert future("KXUFCHEAVYWEIGHTTITLE", "KXUFCHEAVYWEIGHTTITLE-26", "KXUFCHEAVYWEIGHTTITLE-26-VAC", "2026-12-31T17:00:00+00:00",
                  sport="ufc", outcome="Vacant") is None
    assert future("KXEPLLEADER", "KXEPLLEADER-27SAVES", "KXEPLLEADER-27SAVES-X", "2027-06-01T15:00:00+00:00", sport="epl",
                  outcome="David Raya") is None       # An event key the table does not have.


def test_a_players_season_total_keeps_the_strict_line():
    yards = future("KXNFLSEASONRECYDS", "KXNFLSEASONRECYDS-27C1000", "KXNFLSEASONRECYDS-27C1000-JCHASE1", "2027-01-31T15:00:00+00:00",
                   outcome="Ja'Marr Chase", line=999.5)
    assert bet_fields(yards) == ("season_receiving_yards", 2027, None, None, None, "jamarr chase", 999.5, "yes")


def test_team_futures_by_their_event_and_the_teams_new_leagues():
    top4 = future("KXEPLTOP", "KXEPLTOP-27TOP4", "KXEPLTOP-27TOP4-MCI", "2027-06-14T15:00:00+00:00", sport="epl")
    points = future("KXEPLTEAMPOINTS", "KXEPLTEAMPOINTS-27", "KXEPLTEAMPOINTS-27-ARS70", "2027-05-30T15:00:00+00:00", sport="epl", line=69.5)
    final = future("KXUCLROUND", "KXUCLROUND-27FINAL", "KXUCLROUND-27FINAL-MCI", "2027-05-06T15:00:00+00:00", sport="ucl")
    apertura = future("KXLIGAMX", "KXLIGAMX-27APER", "KXLIGAMX-27APER-TOL", "2027-01-02T15:00:00+00:00", sport="ligamx")
    worst = future("KXNBARECORD", "KXNBARECORD-27WORST", "KXNBARECORD-27WORST-WAS", "2027-05-01T15:00:00+00:00", sport="nba")
    conference = future("KXNCAAFCONF", "KXNCAAFCONF-26", "KXNCAAFCONF-26-B10", "2027-02-01T15:00:00+00:00", sport="ncaaf")
    assert bet_fields(top4) == ("top_4", 2027, None, None, None, "MCI", None, "yes")
    assert (points.kind, points.subject, points.line) == ("season_points", "ARS", 69.5)
    assert (final.kind, final.subject, apertura.kind, apertura.subject) == ("reach_final", "MCI", "apertura_champion", "TOL")
    assert (worst.kind, worst.subject, conference.kind, conference.subject) == ("worst_record", "WAS", "champion_conference", "big_ten")
    plzen = future("KXUEL", "KXUEL-27", "KXUEL-27-VIK", "2027-05-26T15:00:00+00:00", sport="uel")
    viking = future("KXUCL", "KXUCL-27", "KXUCL-27-VIK", "2027-06-05T15:00:00+00:00", sport="ucl")
    assert (plzen.subject, viking.subject) == ("VIK", "VIK")      # One code, a team in each competition's own aliases.
    assert future("KXNCAAFCONF", "KXNCAAFCONF-26", "KXNCAAFCONF-26-OTHER", "2027-02-01T15:00:00+00:00", sport="ncaaf") is None


def election(series, event, ticker, outcome=""):
    return kalshi.classify(row(series, event, ticker, sport="politics", outcome=outcome))


def test_elections_name_the_race_and_the_party_or_candidate():
    house = election("CONTROLH", "CONTROLH-2026", "CONTROLH-2026-D", "Democratic Party")
    senate = election("SENATEGA", "SENATEGA-26", "SENATEGA-26-R", "Mike Collins")
    osborn = election("SENATENE", "SENATENE-26", "SENATENE-26-DOSB", "Dan Osborn")
    seat = election("HOUSEAZ1", "HOUSEAZ1-26", "HOUSEAZ1-26-D", "Amish Shah")
    race = election("KXHOUSERACE", "KXHOUSERACE-NY17-26", "KXHOUSERACE-NY17-26-R", "Mike Lawler")
    at_large = election("KXHOUSERACE", "KXHOUSERACE-WYAL-26", "KXHOUSERACE-WYAL-26-R", "Chuck Gray")
    governor = election("GOVPARTYMI", "GOVPARTYMI-26", "GOVPARTYMI-26-MD", "Mike Duggan")
    assert bet_fields(house) == ("house_control", 2026, None, None, None, "D", None, "yes")
    assert (senate.kind, senate.season, senate.subject) == ("senate_race", 2026, "GA R")
    assert (osborn.subject, seat.subject, race.subject, at_large.subject) == ("NE dan osborn", "AZ-01 D", "NY-17 R", "WY-AL R")
    assert (governor.kind, governor.subject) == ("governor_race", "MI mike duggan")
    assert election("SENATEGA", "SENATEGA-28", "SENATEGA-28-D").season == 2028


def test_a_top_two_state_pairs_only_candidates():
    assert election("KXHOUSERACE", "KXHOUSERACE-CA06-26", "KXHOUSERACE-CA06-26-R", "Republican party") is None
    assert election("KXHOUSERACE", "KXHOUSERACE-CA06-26", "KXHOUSERACE-CA06-26-KKIL", "Kevin Kiley").subject == "CA-06 kevin kiley"
    assert election("KXGOVAK", "KXGOVAK-26", "KXGOVAK-26-CBIS", "Click Bishop").subject == "AK click bishop"
    assert election("SENATEXX", "SENATEXX-26", "SENATEXX-26-D") is None      # No such state.


def test_a_race_reads_its_date_with_the_month_in_full():
    # 'September' in full, not a shortened month.
    rules = "If Lando Norris finishes in first in the main race originally scheduled for September 6, 2026 at the 2026 Italian Grand Prix"
    race = kalshi.classify(row("KXF1RACE", "KXF1RACE-ITA26", "KXF1RACE-ITA26-NOR", sport="f1", outcome="Lando Norris", rules=rules))
    assert bet_fields(race) == ("race_winner", 2026, "2026-09-06", None, None, "lando norris", None, "yes")
