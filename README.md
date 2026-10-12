# PredictionArbitrageTrader

A bot that trades arbitrage between the two US prediction markets, Kalshi
and Polymarket US. When holding *yes* on a bet through one venue and *no*
through the other costs less than a dollar after fees, buying both locks
in the difference whatever happens.

- **Venues:** Kalshi and Polymarket US.
- **Modes:** paper (simulated fills against the real order books), live
  (real orders), or both at once.
- **Bets:** futures (titles, awards, season leaders and totals, election
  races) and single events (games, matches, fights, races).
- **Deployment:** one process on an EC2 instance in us-east-1, trading
  every sport live: the futures, and the games once they are under way.

## Contents

- [How it works](#how-it-works)
- [Trading rules](#trading-rules)
- [Live trading safeguards](#live-trading-safeguards)
- [Money and settlement](#money-and-settlement)
- [Catalog](#catalog)
- [Venue clients](#venue-clients)
- [Database](#database)
- [Tools](#tools)
- [Running it](#running-it)
- [Deployment](#deployment)
- [Layout](#layout)

## How it works

The code in `src/` is two programs that share the venue clients (`api/`),
the database layer (`db/`), and small helpers (`common/`):

- **The catalog** (`catalog/`) works out what can be traded. It fetches
  each sport's open markets from both venues, restates each contract as a
  venue neutral `Bet`, and pairs the bets both venues list.
- **The engine** (`engine/run.py`) trades the pairs. It follows every
  paired contract's order book, prices each pair as its books change,
  sends orders when a pair shows enough edge, and settles the trades when
  the contracts resolve. It refreshes the catalog every hour.

### Terms

| Term | Meaning |
|---|---|
| Contract | One tradable market on one venue, seen from its Yes side. |
| Bet | What a contract is about, in venue neutral terms: kind, season, game date, teams, subject, and line. |
| Pair | The contracts on both venues that state one bet, its **members**. |
| Nested pair | Yes on one line of a bet with no on a higher line of it, see [Nested pairs](#nested-pairs). |
| Side | `yes` or `no`. Holding both sides of a bet pays exactly a dollar a pair. |
| Polarity | The side of the bet a contract pays on. Buying the contract holds that side, and buying its opposite, at one minus its bid, holds the other. |
| Book | One contract's order book at one moment: the best five levels of bids and asks. |
| Ladder | What holding one side through one contract costs, level by level, cheapest first. |
| Edge | A dollar less what holding both sides costs, fees included, per contract. |
| Episode | A stretch while a pair's edge stays positive, stored as an **Opportunity**. |
| Leg | One side of a trade: its contract, its order, and what it holds. |
| Exposed | A trade holding more of one side than the other. |
| Flatten | Sell an exposed trade's excess back on its own venue. |
| Payout | When a trade's money comes back: a game's kickoff plus the sport's expected length (`GAME_HOURS`) and half an hour to settle, or a future's close. |
| Shard | One of Kalshi's exchange shards. Each sport trades on one, and each shard's cash is its own. |
| Mode | `paper` or `live`. Every trade is stored with its mode. |
| Desk | One mode's executor, money, and settler. |

### Processes

The engine runs as three processes, the main one and one feed process per
venue, plus a fourth during the hourly catalog refresh:

```
  Kalshi websocket              Polymarket US websockets
          |                                |
          v                                v
  kalshi feed process           polymarket_us feed process
          |                                |
          +---------------+----------------+
                          |   changed books and gaps, over pipes
                          v
  main process:  Streams -> Recorder -> Scanner -> a Desk per mode
                            (books in   (stores     (executor and
                             memory)     episodes)   settler)
```

A feed process keeps its venue's connections and full books, and sends
the main process the best five levels of each changed book, plus any gaps
in the connection. When the main process falls behind, it sends only the
newest book of each contract. `--set feed_processes=false` runs the feeds
in the main process instead.

The engine follows every contract in a pair or nested pair: a game's from
up to 7 days out (`GAME_WINDOW_DAYS`) until 5 hours after kickoff
(`RECORD_HOURS`), whatever its close time says, and a future's until its
close.

### From a book change to a trade

1. **Parse** (`api/kalshi.py`, `api/polymarket_us.py`). The feed process
   applies each websocket message to its copy of the book, with the
   venue's own time for the change.
2. **Hand over** (`market/feeds.py`). The newest book of each contract
   goes down the pipe to the main process.
3. **Remember** (`market/record.py`). The recorder keeps each contract's
   newest `Book` and tells the scanner when a best bid or ask changes.
   While a paper order is in flight on a contract, every book also goes on
   its `Tape` (`market/tape.py`).
4. **Price** (`market/scan.py`, `helper/pricing.py`). For each pair the
   contract is in, the scanner finds the cheapest yes and the cheapest no
   across the pair's members, and the edge of buying both, fees included.
5. **Offer** (`market/scan.py`). A positive edge opens or extends an
   episode. While it lasts, each desk is offered it until that desk
   trades, live first.
6. **Trade** (`trading/executor.py`). The executor checks the
   [trading rules](#trading-rules), sizes the trade, sends the orders, and
   flattens any uneven fill.
7. **Settle** (`money/settle.py`). The settler asks the venues how the
   contracts resolved and pays each leg.

### Schedule

| Every | What runs |
|---|---|
| 1 second | The session tick: open episodes are priced again, so stale ones end, and exposed trades are retried. |
| 15 seconds | Live balances are read from the venues. |
| 30 seconds | A settlement pass, and Kalshi's exchange status, shard by shard. |
| 1 minute | A status line per venue: changes received, how far behind the venue the books arrive, and markets not trading. |
| 5 minutes | Live positions on each venue are compared with the open live trades. |
| 10 minutes | Each component logs a summary. |
| 1 hour | The catalog refresh, in a child process; the Kalshi key's attestation check; a settlement check of each open future. |

## Trading rules

### What each desk trades

| | Paper | Live |
|---|---|---|
| Futures | No | Paying 24 hours or more out (`MIN_PAYOUT_HOURS`) |
| Single events | Before and during, paying within 24 hours (`MAX_PAYOUT_HOURS`) | With `--live-in-play`, once under way, paying within 24 hours |
| Least edge | 2 cents (`MIN_EDGE`), and 100% a year until payout (`MIN_ANNUAL_PCT`) before a game starts | On a future, the edge that returns 100% a year until payout; on a game under way, 2 cents (`LIVE_IN_PLAY_MIN_EDGE`) |
| How long the edge must last | No wait | 0.1 seconds, unbroken (`LIVE_HOLD_SECONDS`) |
| Most contracts a trade | As many as the books and the cash allow | 20 on a game under way (`LIVE_IN_PLAY_CONTRACTS`); no cap on a future |
| Fewest contracts a trade | 1, in whole contracts | 0.1, in hundredths (`LIVE_MIN_CONTRACTS`) |
| Order of the legs | Both at once before a game, Polymarket US first once under way | Polymarket US first |
| Fills | Simulated against the venue's book when the order would arrive | Real immediate or cancel limit orders |

With `--execute both`, paper trades the single events and live the
futures, and with `--live-in-play` both take the games under way.
`--not-live` names sports whose futures live leaves alone.

### Checks before every trade

- **Current books.** A Polymarket US leg trades only on a book newer than
  the Kalshi leg's last change, by the venues' own clocks, or once that
  change is 0.3 seconds old (`CONFIRM_SECONDS`). Polymarket US's books
  reach us about 85 ms after the venue changes them, Kalshi's about 12,
  so a Kalshi move can briefly sit beside a stale Polymarket US price.
  Live counts from the Kalshi change as of when the edge began.
- **Fresh books.** Once a game may have started, a book unchanged for a
  minute is neither priced nor traded (`MAX_BOOK_AGE`), since its market
  may have closed. A future's books are used however old, until the
  contract's close time.
- **Venues trading.** Nothing is opened or sold back with a leg on a venue
  in its weekly maintenance (Thursdays, Kalshi 3 to 5 AM and Polymarket US
  6 to 8 AM Eastern), on a Kalshi shard whose exchange has stopped, or on
  a market its venue says is paused, closed, or decided. A market that
  turned a live order away as not trading is left alone for ten minutes
  (`CLOSED_MARKET_SECONDS`), or until its venue says it trades.
- **Two venues.** A trade's legs are on two venues, since two contracts
  of one venue are priced by the same traders. The exception is a future
  whose two contracts on one venue have the same rules word for word
  (`pricing.same_rules`), as Polymarket US lists each football win total
  twice. A nested pair's legs are always on two venues.
- **One trade a contract at a time.** A signal with a leg on a contract
  that another trade in flight is using is turned down (`busy`), since a
  contract can be in a pair and in its line's nested pairs.

### Sizing and sending

The executor walks both ladders together and sets each leg's limit at the
deepest level that still keeps the least edge, so an order sweeps every
level above that floor. On a live future each level swept must itself
return 100% a year. The trade asks for all the size those levels show
(`FILL_SHARE`), as far as the free cash on each venue pays for, fees
included. On Kalshi only the cash on the market's shard counts. The cash
is reserved and the trade stored before any order goes out.

With a leg on each venue, Polymarket US's order goes first, and Kalshi's
only once it has answered, for what it filled, and not at all if it
filled nothing (`fill_legs`). A Polymarket US quote that is already gone
then leaves nothing to sell back. On a game under way, live first raises
Kalshi's limit so it may give up half the edge left after Polymarket US's
fill (`LIVE_IN_PLAY_ALLOWANCE`, `follow_limit`), to the cent below, no
higher than 0.99, and as far as the shard's cash allows. An immediate or
cancel order fills at the best price there is, so the raise costs nothing
when Kalshi's price has not moved.

On a game, one episode makes one trade. On a future, a trade that matched
and left nothing to flatten gives the episode back, so an edge that lasts
is traded again (`Executor.again`).

### Flattening

When the legs fill unevenly, the excess is sold back on its own venue at
once (`sell_excess`). Buying the missing side on the other venue might
cost less, but would tie the money up until payout.

- A sale goes no lower than half of what the contracts cost
  (`MIN_SALE_SHARE`). Below that the contracts are kept, as the bet may
  still pay.
- A live sale goes no deeper than the books said it would reach, and the
  rest waits for the next tick.
- Whatever stays exposed is tried again every second until it is flat or
  settles. A sale that filled nothing waits a minute
  (`SALE_RETRY_SECONDS`), and one turned away for lack of cash waits until
  the venue's cash grows.
- A trade is let go a day past its payout time
  (`FLATTEN_PAST_PAYOUT_HOURS`), for contracts that never resolve.
- A restart reads the exposed trades back from the database.

### Paper fills

Paper orders (`trading/paper.py`) are timed and filled as live ones
would be:

- Each order's trip to the venue and back is drawn from a lognormal fitted
  to the live orders (`PAPER_ORDER_MS`): Kalshi about 12 ms there and 8
  back, Polymarket US about 59 there for an opening order, 24 for a sale,
  and 31 back.
- The order fills against the book the venue had when it would have
  arrived, read from the contract's tape, after waiting up to
  `PAPER_FEED_SECONDS` for a later book to show nothing is missing.
- Paper remembers what it took from each level and takes it off the books
  it later trades against.
- When live runs beside it, each live order leaves a footprint of what it
  took (`trading/footprints.py`), which paper adds back to later books, so
  paper sees the books as they would be without live's orders. Paper
  waits up to 2 seconds for the answers of live orders sent before its
  own.

## Live trading safeguards

### Brakes

The brakes are in `trading/brakes.py`.

| When | What happens |
|---|---|
| An order's outcome cannot be known: a timeout, a dropped connection, a venue error, or an unreadable answer | Its trade is set aside: no more orders go out for it, and the log names the order to look up. Trading goes on. |
| 3 of the last 20 orders had unknown outcomes (`LIVE_UNKNOWN_LIMIT`, `LIVE_ORDER_WINDOW`) | Halt: no orders of any kind, flattening included. |
| One venue refused its last 3 orders (`LIVE_REJECT_LIMIT`) | Halt, as above. |
| The trades decided in the last 6 hours lost more than 10% of the live money, net (`LIVE_RESULT_HOURS`, `LIVE_MAX_LOSS_SHARE`) | Halt new trades. Flattening goes on. |

- **Refused** means the venue would not take the order: not authorized,
  a bad price, or too many requests. An order that found nothing at its
  price is *unfilled*, one turned away for lack of cash *unfunded*, and
  one turned away because its market or exchange was not trading
  *closed*. None of these is a refusal.
- **Live money** is the cash on both venues and what the open live trades
  hold at cost, less a dollar for each pair the venues have already paid
  back by netting a contract's yes and no.
- A trade is **decided** once its legs hold the same number of contracts,
  or once it settles.
- The rules read the orders and trades tables, so a restart does not reset
  them. The loss rule is checked after each trade, each settlement, and
  each retry that sold something.
- A halt is logged, emailed, and written to `data/live_halt.txt`. It holds
  across restarts until a human has checked the venues and deleted the
  file. What is held is still settled.

### Alerts

`trading/notify.py` emails a human, through the SMTP server in
`data/email.json`, when:

- live trading halts;
- the cash on Polymarket US, or on a Kalshi shard in `LIVE_SHARDS`, falls
  under $5 (`LIVE_LOW_CASH`), once until it is back over;
- the Kalshi key's location attestation is two days from lapsing, and
  again once it has lapsed, since Kalshi then refuses the key for sports
  markets. It is read every hour.

Every alert is also stored in the `alerts` table.

### Position checks

Every 5 minutes (`LIVE_POSITION_SECONDS`) the live executor compares each
venue's positions with what the open live trades hold. A difference of a
hundredth of a contract or more is logged for a human to look into with
`tools/repair_fills.py`.

## Money and settlement

Both modes keep money the same way: free cash per venue, reserved for an
order before anything is awaited, so two signals in the same moment
cannot spend the same dollars.

- **Paper** (`money/paper.py`) starts each venue with $10,000
  (`PAPER_START_BALANCE`). Every cash movement is a `Ledger` row with the
  balance it left, so a restart reads the newest row.
- **Live** (`money/live.py`) reads each venue's balance every 15 seconds,
  which also keeps its HTTPS connection warm, and again right after a
  payout, and applies our own fills in between. Kalshi's cash is kept per
  shard. Nothing is traded before the first reading.

Nothing moves money between venues. The venue holding a winning leg is
paid the dollar, so the balances drift apart, and live emails when one
runs low, for a human to move money by hand.

### Kalshi shards

| Shard | Sports | Share of live cash (`LIVE_SHARDS`) |
|---|---|---|
| 0 | Football, hockey, soccer and national teams, motorsport, UFC, darts, esports, elections | 80% |
| 3 | Baseball, basketball, tennis | 20% |

`tools/kalshi_shards.py --apply` moves the cash to that split and sets
Kalshi's own target split to match. Kalshi does not refill a shard that
orders spend down, so run it again when one runs low.

### Settlement

The settler (`money/settle.py`) asks the venues how the held contracts
resolved:

- a game's, every 30 seconds from kickoff;
- a future's, once an hour before its payout time
  (`FUTURES_SETTLE_CHECK_SECONDS`), since the venues settle many futures
  early, and every 30 seconds after it. The hourly checks are spread
  across the passes.

A leg holding yes is paid what the venue says a yes contract paid, from 0
to 1, and a leg holding no the rest of the dollar. The result is stored as
`yes` or `no` for a whole dollar or nothing, or otherwise as the decimal a
yes contract paid, such as `0.6` for a drawn match. An exposed trade is
settled as it stands. The settler skips a trade while a sale to flatten it
is in flight, and a settled trade is no longer flattened.

## Catalog

`catalog/pipeline.py` runs the steps below in one call. The engine runs it
every hour in a child process, so new games enter the pairs while it
trades. A refresh of every sport takes about eight minutes.

### Sports

| Group | Sport keys |
|---|---|
| North American leagues | `nfl`, `ncaaf`, `mlb`, `nhl`, `nba`, `wnba`, `ncaab` |
| Club soccer | `epl`, `laliga`, `seriea`, `bundesliga`, `ligue1`, `ligamx`, `mls`, `ucl`, `uel` |
| National teams | `intl` (men), `intlw` (women) |
| Matches between two people | `tennis`, `darts`, `ufc` |
| Races | `f1`, `nascar` |
| Esports | `cs2`, `lol`, `valorant`, `dota2`, `r6`, `ow` |
| Elections | `politics` |

### Bets

| Sports | Futures | Single events |
|---|---|---|
| North American leagues | Champions, conference and division winners, seeds, playoffs, best and worst records, season win totals, awards, stat leaders, player season totals | Winner, spread, total, team total, player props |
| Club soccer | Champions, top 2, 4, and 6, relegation, last place, the Champions League's league phase, goal and assist leaders | Result, spread, total, both teams to score, and exact score, each for the match and its halves; corners |
| National teams | None, since Kalshi alone lists the tournaments | As club soccer |
| Tennis, darts, UFC | Year-end No. 1, world champion, title holders | Winner, games and sets spreads and totals, a set's winner, the score in sets, going the distance, the winning round |
| Races | Drivers' champions, F1's top constructor | Race winner |
| Esports | None | Match winner, map winner, total maps |
| Elections | House and Senate control; each Senate, governor, and House race, by party or candidate | None |

### Steps

1. **Fetch** (`fetch.py`) pulls each sport's open markets into the
   `contracts` table. Kalshi is read through its public REST catalog, only
   the series listed in `SPORTS` or recognized by the classifier, and
   each series gives the shard its markets trade on. Polymarket US is read
   through its gateway, one call per tag.
2. **Classify** (`classify/`, one parser per venue) restates each
   contract as a `Bet`. A future leaves the game date and teams empty.
   Contracts no parser understands are counted and left out.
3. **Match** (`match.py`) groups the bets with the same identity into a
   `Pair`, labeled by sport and bet, such as
   `nfl spread 2026-09-20 CAR@ATL ATL 3.5`. A pair exists only when both
   venues list the bet.
4. **Nest** (`nest.py`) builds the [nested pairs](#nested-pairs).

`notes.py` flags kinds whose settlement rules differ between the venues,
as a warning to read rather than a bar: a postponed game, overtime, a
retired tennis player, a fight's draw, a darts walkover, a drawn esports
match, a driver who does not finish, a tie for an award or a lead, when a
title holder or ranking is read, and when an election race or chamber
counts as won.

### Matching rules

- **Teams** are resolved through `teams.py` and a file per sport in
  `aliases/`, since leagues reuse codes (DAL is the Cowboys and the
  Mavericks). Esports teams go by a normalized name, and a name the
  venues spell differently is not paired.
- **People** are matched on a key that ignores accents, punctuation, and
  suffixes, so *Ronald Acuña Jr.* is *Ronald Acuna*. The few names the
  venues spell apart are in `PLAYER_ALIASES`. A market on no one, Kalshi's
  *Vacant*, is never paired.
- **Lines** are made strict, so *1000+* and *over 999.5* are one bet.
- **Seasons** are the one a future settles in.
- **Dates** can differ by a day between the venues for tennis, darts, UFC,
  esports, and national teams, which they date by different clocks. A
  date only one venue lists joins the other venue's date a day off, while
  two meetings on days in a row stay apart.
- **Left out:** doubleheaders and two esports meetings of the same teams
  on one date, since a game is its date and teams; and party bets on races
  in California, Washington, and Alaska, where the venues read a party's
  win differently, so only candidates are paired there.

### Nested pairs

Every bet with a line is stated as a number going over the line. So of
two lines of one bet, yes on the lower and no on the higher pays at least
a dollar a pair, and two when the number lands between them: over 42.5
points with not over 45.5 pays $2 on 43, 44, or 45 points and $1 on any
other score.

- A nested pair is a pair of its own, such as
  `nfl total 2026-10-11 PHI@JAX 42.5 nested 45.5`, of kind
  `total_nested`. Its members are kept in the `nest_members` table with
  the side each supplies: the lower line's the yes, the higher line's the
  no.
- Each line nests with the next two lines above it (`STEPS`) that give
  its legs two venues, so Kalshi's 42.5 can nest with Polymarket US's
  43.5. A line only one venue lists is followed for its nested pairs.
- Only lines ending in a half nest, so no number lands on a line, and only
  kinds stated as a number going over a line (`nests_kind`): spreads and
  totals of games and their parts, team totals, maps, games, corners, and
  player and season numbers.
- Futures, and games from the day before the refresh on, are nested.

## Venue clients

`api/bookstream.py` is the websocket loop both venues share. One
connection carries every wanted contract, and contracts are added without
reconnecting. A dropped connection reconnects at once, backing off only
on repeated drops. The stretch until the new subscription is confirmed is
stored as a gap, and that connection's books are dropped until they are
sent again.

| | Kalshi (`api/kalshi.py`) | Polymarket US (`api/polymarket_us.py`) |
|---|---|---|
| Signing | RSA-PSS | Ed25519 |
| Books | Changes, on one connection for the whole catalog, through `external-api`, Kalshi's hosts for API traders | Whole books, in requests of up to 100 markets, 10 requests a connection. There is no unsubscribe, so new contracts go to a connection with room, or a new one. |
| Market state | A lifecycle channel says when a market pauses, closes, or is decided, and the markets call after a reconnect | Carried with every book |
| Results | The market's result, or its settlement value when the result is scalar | The long side's price once the market resolves, one call per event, half a second apart (`RESULTS_SLEEP`) |
| Also | Cash per shard, moving cash between shards, the key's attestation date, the exchange's status by shard | An order is priced on the long side, so a short order at p is sent at 1 − p |

Both clients also:

- send immediate or cancel limit orders and turn the answer into an
  `Answer`, the same for both venues. A Polymarket US order is looked up
  when its answer does not say how it ended;
- count contracts to the hundredth, as both venues fill orders in pieces
  that small (`orders.exact`);
- read the account's balances and positions.

A Kalshi sale is an ordinary order, not reduce-only: each venue keeps one
position per market across all our trades, so a sale for one trade can be
a purchase for the account. Trading calls go over kept HTTPS connections
(`api/http.py`) and are never retried, since an order sent twice trades
twice.

## Database

One SQLite file, `data/prediction_arbitrage_trader.sqlite`. Each table has
one writer:

| Table | Written by | Holds |
|---|---|---|
| `contracts`, `bets`, `pairs` | the catalog | each venue's markets, their bets, and the pairs |
| `nest_members` | the catalog | each nested pair's contracts and the side each supplies |
| `gaps` | the recorder | each stretch a feed connection was down |
| `opportunities` | the scanner | one row per episode, with what one live order could have taken (`take_size`) |
| `trades` | the executors | every trade, paper and live, and whether its game was under way (`in_play`) |
| `orders` | the live executor | every real order, with the times of the book it went out on |
| `settlements` | the settler | each leg's result, payout, and time |
| `ledger` | paper money | every paper dollar in and out |
| `alerts` | the notifier | every email the live process sends |
| `twins` | nothing | an in-play test's live trades beside their paper twins, read by `tools/in_play_test.py` |

Trades and settlements carry a `mode`, so paper and live never mix. The
models are in `db/models.py`, the schema in `db/schema.sql`, changes for
existing databases as numbered steps in `db/migrations.py`, and the reads
and writes in `db/database.py`. Order books are not stored, so the
database grows only with the episodes, trades, and orders.

## Tools

| Tool | What it does |
|---|---|
| `tools/summary.py` | A report on the database, below. |
| `tools/live_check.py` | Reads both venues' balances and when the Kalshi key's attestation lapses, without trading. `--email` sends a test email. |
| `tools/kalshi_shards.py` | Says how it would split the Kalshi cash between the shards by `LIVE_SHARDS`. `--apply` moves it. |
| `tools/repair_fills.py` | Reads the stored Polymarket US answers again as the client now reads them, works out again the trades whose orders read differently, and compares the trades with the venues' positions. Writes nothing without `--apply`; stop the engine first. `--trade N` works out a named trade again from its orders. |
| `tools/latency_report.py` | How far behind the venues the books ran, from the status lines in `data/record.log`, how long the real orders took, and how long the edges lasted. |
| `tools/feed_check.py` | Compares ways of following the books side by side on the same markets, without trading. |
| `tools/in_play_test.py` | Reports the `twins` table. |

`summary.py` reports on the last `--hours` (24 by default):

1. The database's size, each sport's pairs, and feed drops.
2. For the futures, then the games in play (`--market` picks one):
   - the opportunities as live would take them: what one order could have
     taken and locked in, and how long the edge lasted, by sport and kind,
     and the largest;
   - for each mode (`--mode` picks one): the window's net, the trades by
     kind, the largest, the open trades opened in the window, the trades
     by outcome, and a few lines on all the open trades: their capital,
     less what netting on the venues has paid back of live's, the profit
     they are expected to return and its rate a year, and when they
     resolve.
3. Each mode's totals since its first trade, for the futures and the games
   apart: trades settled and open, capital, return, and rate a year.
4. The paper money, and the live balances read from the venues with
   Kalshi's shards (`--no-live` leaves those out).

`--sport` narrows everything to some sports.

## Running it

Python 3.12 or newer:

```bash
pip3 install -r requirements.txt
python3 -m pytest tests -q
```

Build a sport's catalog and run the engine, from `src/`:

```bash
cd src
python3 -m catalog.pipeline --sport nfl
python3 -m engine.run --sport nfl
```

### Engine options

| Option | Effect |
|---|---|
| `--sport nfl,ncaaf` | The sports to trade, comma separated, or `all`. One run trades them all from one pool of money. |
| `--execute paper\|live\|both` | The desks to run. `paper` by default. |
| `--live-in-play` | With `live` or `both`, live also trades the games under way. |
| `--not-live nfl` | Sports whose futures live leaves alone. `none` by default. |
| `--no-trade` | Scan without trading. |
| `--no-scan` | Only follow the books, to check the connections. |
| `--seconds 120` | Stop after that many seconds. |
| `--skip-refresh` | Start from the stored catalog instead of refreshing it first. |
| `--catalog-minutes 60` | Minutes between catalog refreshes. 0 never refreshes. |
| `--set NAME=VALUE` | Override a setting for this run, such as `--set min_edge=0.03`. Repeatable. |

The settings are in `src/engine/helper/config.py`. Those starting with
`PAPER_` are read only by paper trading, those starting with `LIVE_` only
by live trading, and the rest by both. A run logs the commit it runs and
every setting when it starts.

### Keys and email

The venue keys and the email settings go in `data/`, which is gitignored:

| File | For |
|---|---|
| `kalshi_key_id.txt`, `kalshi_private_key.pem` | Kalshi |
| `polymarket_us_key_id.txt`, `polymarket_us_secret_key.txt` | Polymarket US |
| `email.json` | Alert emails |

The feeds need the keys, and live trading needs them with trading
permission. `email.json` looks like this:

```json
{"host": "smtp.gmail.com", "port": 587, "user": "me@gmail.com",
 "password": "an app password", "from": "me@gmail.com", "to": ["me@gmail.com"]}
```

### Before a live run

From `src/`, check the balances, the key's attestation, and the email, and
split the Kalshi cash across its shards:

```bash
python3 -m tools.live_check --email
python3 -m tools.kalshi_shards --apply
```

### Reports

```bash
python3 src/tools/summary.py --hours 24
python3 src/tools/summary.py --mode paper --sport epl,ucl
python3 src/tools/summary.py --mode live --market in-play
python3 src/tools/latency_report.py --hours 4
python3 src/tools/in_play_test.py
cd src && python3 -m tools.feed_check --seconds 300 --markets 100
```

## Deployment

- **Instance:** a c7a.large in us-east-1, where Polymarket US's exchange
  runs, about 10 ms from Kalshi's API in us-east-2. A signed round trip
  takes about 35 ms to Kalshi and 30 ms to Polymarket US.
- **Service:** the systemd service `prediction-arbitrage-trader` runs
  `python3 -m engine.run --sport all --execute live --live-in-play` from
  `~/PredictionArbitrageTrader/src`. It starts on boot and restarts on any
  exit, and the feed and catalog child processes stop with it.
- **Keys:** copied to the instance's `data/` by `scp` only.
- **Deploying:** `git pull` on the instance, then the tests, then a
  service restart only if they pass. A restart refreshes the catalog for
  about eight minutes before it subscribes to the books, and reads the
  exposed trades back from the database.
- **Operating:** `commands.txt`, gitignored and kept on the operator's
  machine, holds the commands to check, deploy, and operate the instance.

## Layout

```
src/
  api/        venue clients and the shared websocket book stream
  catalog/    fetch, classify (one parser per venue), match, nest, notes, pipeline
  common/     paths, time and json helpers, the venue list and maintenance windows, the sports in groups, the logger,
              the timer background work runs on, how a child process starts, quantiles
  db/         models, the SQLite schema and its migrations, reads and writes
  engine/     run, the process that wires the components together
    components/
      market/    record, feeds, streams, scan, tape, exchange: the venues' books, which markets trade, and the edges between them
      trading/   executor (what paper and live share), paper, live, footprints, brakes, notify
      money/     balances (what paper and live share), paper, live, settle
    helper/      config (the settings a run is tuned by), game (when a game is played and when its bets pay out), pricing, fees
  tools/      summary report, live_check, kalshi_shards, repair_fills, latency_report, feed_check, in_play_test
tests/        mirrors src, run with pytest, configured in pyproject.toml
  support/    helpers the tests share, and the streams and refreshes a child process can run
commands.txt  operating the AWS instance, gitignored, kept locally
```

### Where to look

| To change | Look in |
|---|---|
| what a venue's API returns | `api/kalshi.py`, `api/polymarket_us.py` |
| how a websocket connects and reconnects | `api/bookstream.py` |
| which markets are understood | `catalog/classify/` |
| how bets are paired | `catalog/match.py` |
| how a bet's lines are nested | `catalog/nest.py` |
| where the venues' rules differ | `catalog/notes.py` |
| fees | `engine/helper/fees.py` |
| how an edge is priced | `engine/helper/pricing.py` |
| game timing | `engine/helper/game.py` |
| when each venue is in maintenance | `common/venues.py` |
| which markets and exchanges are not trading | `engine/components/market/record.py`, `engine/components/market/exchange.py` |
| which sports are treated alike | `common/sports.py` |
| every tunable number | `engine/helper/config.py` |
| when a trade is taken and sized | `engine/components/trading/executor.py` |
| how an order fills | `engine/components/trading/paper.py`, `engine/components/trading/live.py` |
| live trading games in play, Polymarket US first | `engine/components/trading/executor.py` (`fill_legs`), `engine/components/trading/footprints.py` |
| when live trading halts | `engine/components/trading/brakes.py` |
| the email alerts | `engine/components/trading/notify.py` |
| the Kalshi cash on each shard | `tools/kalshi_shards.py` |
| how far behind the feeds run | `tools/latency_report.py`, `tools/feed_check.py` |
| how the processes are wired | `engine/run.py` |
| the report on the database | `tools/summary.py` |
| live records against the venues' positions | `tools/repair_fills.py` |
