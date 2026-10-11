# SportsArb

A bot that looks for cross-venue arbitrage between the two US prediction
markets, Kalshi and Polymarket US, and trades what it finds, on paper, with
real money, or both at once. It covers the NFL, college football, MLB,
NHL, NBA, WNBA, and men's college basketball, the Premier League, La Liga,
Serie A, the Bundesliga, Ligue 1, Liga MX, MLS, the Champions League and
Europa League, national teams' soccer, men's and women's, Formula 1,
NASCAR, UFC, tennis, darts, esports, Counter-Strike 2, League of Legends,
Valorant, Dota 2, Rainbow Six Siege, and Overwatch, and the 2026 US
elections; Bitcoin was covered too until October 10. It follows two kinds
of bet: futures, titles, awards, a season's leaders, season totals, and
election races, which live trades, and the bets on one event, games,
matches, fights, and races, which live trades once they are under way and
paper before and while they are played. When the cheapest way to hold
*yes* on one venue and the cheapest way to hold *no* on the other add up
to less than a dollar after fees, buying both locks in the difference
whatever happens.

The whole thing runs on an EC2 instance in us-east-1, as one process with
each venue's feed in a child process of its own: it follows every order
book change for thousands of contracts, prices every cross-venue pair on
every change, sends orders when a pair shows an edge worth the time its
money is tied up, and settles the trades when the contracts resolve. Live
trades futures, where an edge lasts long enough for our orders to reach it,
and paper the bets on one event paying within a day, where faster traders
may take the edges first, to see how they would do, its orders timed as
live ones are. By default the orders are paper. With `--execute live` or
`--execute both` it sends real ones, and with `--live-in-play` live trades
the games under way too, an edge of two cents or more, at most 20
contracts a trade. Polymarket US's order goes first, and live takes an
edge, a game's or a future's, once it has lasted a tenth of a second.
From October 4 to 5 the service on the instance
traded every sport's futures and the elections live, with paper on every
event beside it, and the in-play test below. From October 5 it is set to
trade live alone: every sport's futures, the elections' included, and
every game under way
(`--execute live --live-in-play --not-live none`). On October 6 live
traded the futures alone for a time, the games in play switched off
(`--execute live --not-live none`), until the games came back under the
in-play rules below.

## How it works

Everything lives in `src/`, in two programs.

**The catalog** (`src/catalog`) works out what can be traded. It fetches
each sport's open futures, and the elections', from both venues, restates
each one as a `Bet` in venue neutral terms, and pairs up the bets both
venues list. The pairs go into the database. You can
run it on its own (`python3 -m catalog.pipeline`), and the live process
reruns it every hour.

**The live process** (`src/engine/run.py`) trades the pairs. It follows
every paired contract's order book, prices each pair as its books change,
trades when a pair shows enough edge for the time until it pays, and
settles the trades when the contracts resolve.

Both share the venue clients in `src/api`, the database layer in `src/db`,
and small helpers in `src/common`. This section walks the code in the order
data moves through it. [Part by part](#part-by-part) says what each file
does in more detail, and [Layout](#layout), at the end, where to look for
what.

### The words the code uses

- **Contract**: one tradable market on one venue, seen from its Yes side.
- **Bet**: what a contract is about, in venue neutral terms: kind, game,
  teams, subject, line. Two contracts with the same bet fields are the
  same bet.
- **Pair**: the contracts on both venues that describe one bet. Its
  contracts are its **members**.
- **Side**: `yes` or `no`, the two outcomes of a bet. Holding both sides
  pays exactly a dollar a pair, whichever way the game goes.
- **Polarity**: which side of the bet a contract pays on. You hold the
  side a contract pays on by buying the contract, and the other side by
  buying its opposite, at one minus its bid.
- **Book**: one contract's order book at one moment, a few price levels
  of bids and asks, from the Yes side (`db/models.py`).
- **Ladder**: what holding one side through one contract costs, level by
  level, fees left out, cheapest first (`pricing.ladder`).
- **Edge**: a dollar less what holding both sides costs, fees included,
  per contract. Positive edge is the arbitrage.
- **Episode**: a stretch while a pair's edge stays positive. Each ends up
  as an **Opportunity** in the database.
- **Leg**: one side of a trade: the contract it goes through, the order
  sent, and what it holds after (`db/models.py`).
- **Exposed**: a trade whose legs filled unevenly, so it holds more of
  one side than the other and is not a sure dollar any more.
- **Flatten**: fix an exposed trade by selling the excess back on its own
  venue.
- **Payout**: when a trade's money comes back, the latest of its members':
  half an hour after its game's expected end, or a future's close.
- **Shard**: Kalshi keeps each sport's markets on an exchange shard whose
  cash is its own, football and hockey on shard 0, baseball and basketball
  on 3, crypto on 2. An order spends only its market's shard's cash.
- **Mode**: `paper` or `live`. Paper fills are simulated against the real
  books, live ones are real orders. Every trade is stored with its mode.
- **Desk**: everything one mode needs: its money, its executor, and its
  settler.

### The processes

With feed processes on (the default), the live process is three
processes, one per venue feed plus the main one, and a fourth while the
hourly catalog refresh runs:

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

A child keeps its venue's connections and full books, and sends the main
process only the best five levels of each changed book, plus any gaps.
When the main process falls behind, a child sends only the newest book of
each contract, not a backlog. `--set feed_processes=false` runs the feeds
inside the main process instead. The catalog refresh runs in a child
process of its own too. It briefly holds some 300 MB, and a child gives
all of it back when it ends, where a thread of the main process kept
about 50 MB of each refresh for good.

### The life of a book update

Follow one Kalshi order book change from the wire to a trade.

1. **Parse** (`api/kalshi.py`, `KalshiBookStream.handle` and `apply`).
   The feed child reads the websocket message, applies the change to its
   copy of the book, and passes the best levels on, with Kalshi's own
   time for the change. The base class,
   `api/bookstream.py`, handles connecting, subscribing, and reconnecting.
   It also records each stretch without a connection as a gap.
2. **Hand over** (`engine/components/market/feeds.py`). `VenueFeed`
   stamps the time the book arrived, and the `Outbox` holds the newest
   book of each contract until its thread sends it down the pipe.
3. **Receive** (`FeedProcess.receive`, then `Streams.on_book` in
   `market/streams.py`). The main process drops books of contracts the
   catalog has since removed and passes the rest on.
4. **Remember** (`Recorder.on_book` in `market/record.py`). The recorder
   keeps the book as the contract's newest `Book`, with our time for it
   and the venue's. If the best bid or ask changed, it tells the scanner.
   While a paper order is in flight on the contract, the book also goes on
   the contract's `Tape` (`market/tape.py`), every one in turn, so the
   order can find the book the venue had when it would have arrived.
5. **Price** (`Scanner.on_book` and `update` in `market/scan.py`, with
   `pricing.best_trade`). For every pair the contract belongs to, the
   scanner finds the cheapest way to hold yes and the cheapest way to
   hold no across the pair's members, on two different venues, or on a
   future two contracts of one venue with the same rules. Then it works
   out the edge of buying both. A member whose venue is in its weekly
   maintenance is left out (`venues.is_maintenance`).
6. **Follow the episode**. A positive edge opens an `Episode` or extends
   it and keeps its peak. When the edge ends, the episode is stored as an
   Opportunity. While it lasts, each desk's executor is offered it, until
   that executor takes a trade.

One live game brought up to 1,700 Kalshi changes a second. That is why
steps 1 and 2 run in the feed processes, and only a change to a best
level reaches the scanner.

Polymarket US differs in one way: each book message carries the whole
book, so the child replaces its copy rather than patching it. Both venues
say when they made each change, so the status line every minute says how
far behind the venue each feed's books reached us, which the executor
checks before it trades, below.

A market can stop trading while its book stays up, and even moves:
paused, closed, or decided. Each feed says so when its venue does, and
when the market trades again (`BookStream.set_state`), and the word goes
down the pipe in order with the gaps. Polymarket US gives each market's
state with every book, and Kalshi says it on its market lifecycle channel,
which the same connection follows. The recorder marks such a book halted
(`Book.halted`), and a halted book is not priced or traded
(`pricing.fresh`), so its pairs' episodes end and no desk is offered them.
The status line counts the markets not trading, by venue and why.

### The life of a trade

1. **Decide** (`Executor.signal` in `trading/executor.py`). The executor
   takes the signal when the edge is at least `MIN_EDGE`, for live on a
   future the edge that returns `MIN_ANNUAL_PCT` a year until it pays and
   on a game under way `LIVE_IN_PLAY_MIN_EDGE` (`min_edge`), for live the
   pair is a future, or with `--live-in-play` a game under way (`plays`),
   the bet pays when the desk trades it (`pays_in_time`), a future
   `MIN_PAYOUT_HOURS` or more away and a game within `MAX_PAYOUT_HOURS`,
   and the edge returns `MIN_ANNUAL_PCT` a year or more until then, asked
   of no game under way from 2026-10-05 (`pays_enough`), for live the edge
   has stayed at its least edge or more for `LIVE_HOLD_SECONDS`, by the
   scanner's episode (`hold`, `Scanner.edge_since`), and a Polymarket US
   leg's book is current (`confirm_wait`): newer than the Kalshi leg's last
   change, by the venues' own clocks, or else that change is
   `CONFIRM_SECONDS` old, for live the Kalshi change as of when the edge
   reached its least edge (`opened`, `Scanner.edge_books`).
   Otherwise the edge waits, and the scanner offers it again at the next
   change, or the moment the wait ends (`recheck`), not at the next tick up
   to a second later. `quantity_for` walks both ladders together through
   the levels that keep that edge and sets each leg's limit at the deepest
   one, live on a game under way stopping at `LIVE_IN_PLAY_CONTRACTS`
   (`most`). It then asks for `FILL_SHARE` of what those levels show, but
   no more than the cash each venue can spend, on Kalshi the cash on the
   market's shard, live as on paper, each leg's fee at its limit included
   (`order_cost`). The cash is reserved, fees too, and the trade is stored
   before any order goes out.
2. **Fill** (`run_trade`). With a leg on each venue, live's Polymarket US
   order goes first and its Kalshi order only once that has answered, for
   what it filled, and not at all when it filled nothing (`fill_legs`,
   `lead`). Paper does so on a game under way, and sends both at once
   before one, as either does with both legs on one venue.
   - Paper (`trading/paper.py`): each order takes a trip there and back
     drawn from what the live orders took, and fills against the book the
     venue had when it would have arrived, by the venue's own clock, from
     the contract's tape.
   - Live (`trading/live.py`): each order is a real immediate or cancel
     limit order through the venue client's `place_order`. It is stored in
     the orders table before it is sent, with the times of the book it went
     out on (`book_at`, the venue's for its last change, and `book_ts`,
     ours for when that reached us), and again with the answer.
3. **Flatten** (`flatten`). If the legs filled unevenly, `sell_excess`
   sells the excess back on its own venue. Buying the missing side on the
   other venue might cost less, but would tie the money up until the bet
   pays, where a sale frees it at once.
4. **Keep trying** (`retry`, every tick). Whatever stays exposed is tried
   again against newer books until it is flat or it settles, however long
   the game outlasts its payout time, which is only an estimate: a market
   that has stopped trading sells nothing. Only a trade a day past its
   payout time (`FLATTEN_PAST_PAYOUT_HOURS`) is let go, for contracts that
   never resolve. A sale that filled nothing waits a minute
   (`SALE_RETRY_SECONDS`), and one the venue turned away for lack of cash
   waits until the cash there has grown. A restart reads exposed trades
   back from the database by the same rule (`reload_exposed`).
5. **Settle** (`Settler` in `money/settle.py`). From kickoff, every 30
   seconds, the settler asks the venues how the held contracts resolved.
   It pays each winning leg a dollar a contract into the desk's money,
   stores the `Settlement`, and tells the executor to stop flattening that
   trade.

### What happens when

`Session.tick` runs once a second:

- the scanner prices every open episode again, so episodes whose books go
  stale, or whose venue begins its weekly maintenance, end;
- the recorder lets a market that turned a live order away as not trading
  be traded again once `CLOSED_MARKET_SECONDS`, ten minutes, have passed;
- every 30 seconds the exchange status reads whether Kalshi trades, shard
  by shard (`EXCHANGE_STATUS_SECONDS`, `market/exchange.py`);
- every hour the attestation watch reads when the Kalshi key's location
  attestation lapses;
- each desk reads the live balances every 15 seconds, retries exposed
  trades, and starts a settlement pass every 30 seconds, and the live desk
  emails once when the cash on Polymarket US, or on either Kalshi shard it
  trades on, falls under `LIVE_LOW_CASH`;
- every 5 minutes the live desk reads each venue's positions and logs any
  contract the venue holds more or less of than the live trades say
  (`LIVE_POSITION_SECONDS`);
- a status line is logged every minute, and each component's summary
  every ten.

Every hour each sport's catalog is refreshed in a child process, and the
new pairs' contracts are added to the running feeds without reconnecting.

### Where things are stored

One SQLite file, `data/sportsarb.sqlite`. Each table is written by one
place (see the top of `db/database.py`):

| table | written by |
|---|---|
| contracts, bets, pairs | the catalog |
| gaps | the recorder, when a feed connection drops |
| opportunities | the scanner, one row per episode, with what one order could have had as live takes it (`take_size`) and whether that moment came with a change of Polymarket US's book (`pm_changed`) |
| trades | the executors, paper and live, with whether the game was under way (`in_play`) |
| orders | the live executor, one row per real order, with the times of the book it went out on |
| settlements | the settler |
| ledger | paper money, every dollar in and out |
| alerts | every email the live process sends: low cash, halts, and the Kalshi key's attestation |
| twins | the in-play test of 2026-10-04 to 10-05, each of its 200 live trades in play, its paper twin, and the order its orders went in; nothing writes to it now |

Trades and settlements carry a `mode`, `paper` or `live`, so the two modes
never mix. Every table has a model in `db/models.py` and its schema in
`db/schema.sql`. Changes to a table for databases that already exist are
numbered steps in `db/migrations.py`, and `db/database.py` holds the reads
and writes.

## Part by part

### Catalog (`src/catalog`)

**fetch.py** pulls a sport's open markets from both venues into the
`contracts` table, its futures and its games, matches, or races.
Our sport keys are `nfl`, `ncaaf`, `mlb`, `nhl`, `nba`, `wnba`, `ncaab`,
`epl`, `laliga`, `seriea`, `bundesliga`, `ligue1`, `ligamx`, `mls`, `ucl`,
`uel`, `intl` and `intlw`, the men's and women's national teams, `f1`,
`nascar`, `ufc`, `tennis`, `darts`, `cs2`, `lol`, `valorant`, `dota2`,
`r6`, and `ow`, an esports title each, and `politics`. `crypto`, Bitcoin's
futures and 15 minute windows, was left out from October 10, its windows'
trades having filled nothing on Polymarket US. Of the thousands of series
Kalshi lists it takes only those classified, by ticker, each sport's
futures listed in `SPORTS` and its event series added from the classifier's
tables by their prefix, and for elections, which have a series per state or
district, by the shapes the classifier reads, `SENATEGA` or `HOUSEAZ1`.
Kalshi's list of series, every category of it, is read once and kept ten
minutes, so a refresh of every sport reads it once. Kalshi is read through
its public REST catalog, paged under the rate limit, and each series brings
the exchange shard its markets trade on: shard 0 for football, hockey,
soccer, national teams' too, motorsport, UFC, darts, esports, and
elections, and shard 3 for baseball, basketball, and tennis, crypto
being on shard 2. Polymarket US is read through
its gateway, one call per tag, deduplicated across tags, baseball through
the `mlb` tag, since `baseball` brings Korean and Japanese league games
too, the men's national teams through `intf`, `unl`, `cnl`, and `afcq`,
the friendlies, the UEFA and CONCACAF Nations Leagues, and the Africa Cup
of Nations qualifiers, the women's through `uwwcq`, their World Cup
qualifiers, and each esports title through its own. A game's contract
starts at its event's kickoff: an event is a game when it has a game id,
the venue's own or Sportradar's, and a futures market on one is a race's
when the event runs two days or less, and otherwise an award's, which has
no start.

**classify/** turns each contract into a `Bet`, a venue neutral statement
of what the contract is about: kind, season, the game's date and its two
sides for a bet on one event, subject, and a line. A future leaves the
date and sides empty, which is how the desks tell the two apart. Each venue
has its own parser, since the two describe the same thing very differently.
The bets on one event:

- Games of teams, in the five original sports, the WNBA, and men's college
  basketball: the winner, stated as the away team winning with Kalshi's
  home team market its complement, the spread, the total, a team's total,
  and player props, the player and an *at least N* line. Kalshi's event
  ticker holds the date and both teams, `KXNFLGAME-26SEP20CARATL`, and
  Polymarket US's event slug, `nfl-car-atl-2026-09-20`, whose markets'
  types say the kind. A spread on Polymarket US is the first team covering
  its signed line, so a positive line is the second team not winning by
  more than it, a market's *no* side. A doubleheader's games are left out,
  since a game is its date and teams.
- Soccer matches, in every league: the result, a club or the tie, the
  spread, the total, both teams to score, the exact score, each for the
  match and its halves, and the corners, all over 90 minutes and stoppage
  time on both venues. National teams' matches read the same, though Kalshi
  gives each competition its series, `KXINTLFRIENDLY`, `KXUEFANL`,
  `KXCONCACAFNL`, and `KXAFCON` for the men and `KXFIFAW` for the women,
  and Polymarket US its tag. Their tournaments' winners are on Kalshi
  alone, so they have no futures here.
- Esports matches, a sport for each title: the match's winner, a map's, a
  League of Legends or Dota 2 game being a map, and the total maps played.
  Kalshi's series are named alike, `KXCS2GAME`, `KXCS2MAP`, and
  `KXCS2TOTALMAPS`, the event ticker the date, the Eastern start time, and
  both teams' codes, `KXCS2MAP-26OCT061000GOTMEL-2`, and Polymarket US's
  event slug `cs2-mel-goth-2026-10-06`, both dated as the match was first
  scheduled. The teams change too often for code files, so a team is its
  name, `teams.team_key`, lower case letters and digits without the words
  one venue adds, *Team Falcons* being *Falcons* and *Rounds.gg* *Rounds*,
  read from the event title and a winner's subtitle on Kalshi and the event
  title and a map's title on Polymarket US. A name a venue spells apart,
  *Betclic Apogee* for *Apogee*, is not paired. Two meetings of the same
  teams on one date, as a tournament's lower bracket can bring, are left
  out like a doubleheader. The tournaments' winners are not read yet, since
  nothing in their tickers and slugs names the same tournament on both
  venues.
- Matches between two people, in tennis, darts, and the UFC: the winner,
  the games and sets spreads and totals, a set's winner, the score in sets,
  whether a fight goes the distance, and the round it is won in. The two
  are named by their full names, the key's words in order, since Kalshi
  writes *Wang Cong* where Polymarket US writes *Cong Wang*; Kalshi's title
  for a winner gives only last names, so its two markets' subtitles name
  them.
- Races: F1's and NASCAR's winner and F1's top constructor, a race being
  its date, from Kalshi's rules and Polymarket US's slug.

For a future, Kalshi gives each kind a series of its own, `KXSB` the Super
Bowl, or an event of its own within one, `KXEPLTOP-27TOP4` the Premier
League's top four, and the market ticker ends in the team, `KXSB-27-KC`.
Polymarket US tells kinds apart by the shape of the event slug with its
sport's prefix taken off and its date written as D, `nfl-afceast-D-w` being
a division winner, looked up with its prefix first where two sports share a
shape that means different things, `ucl-D-lastplace`. The kinds:

- Team futures: champions, conference and division winners, top seeds,
  playoff places and rounds, best and worst records, the last undefeated
  and winless teams, college football's unbeaten teams and the conference
  that wins its title, and in soccer the top two, four, and six,
  relegation, last place, and the Champions League's league phase.
- People: awards, a season's leaders in a stat, the NFL's, college
  football's by conference, the baseball postseason's, and soccer's goals
  and assists, and champions and title holders, F1 and NASCAR drivers, UFC
  title holders on December 31, the year-end No. 1 in men's and women's
  tennis, and darts' world champion. A person's market names them in its
  subtitle on Kalshi and its title on Polymarket US.
- Season totals: a team's wins or points at a line, and a player's
  passing, receiving, or rushing yards or touchdowns at a line, the line in
  the market on Kalshi and in the title or the event's shape on Polymarket
  US, `nfl-D-1000recyds` being 1,000 receiving yards or more.
- Elections: control of the House and the Senate, and each Senate,
  governor, and House race, by party, D or R, or by candidate. A race's
  season is its election year. In California and Washington, whose top two
  can be of one party, and Alaska, whose top four can, the venues read a
  party's win differently, Kalshi paying on any member of it taking the
  seat and Polymarket US on its nominee winning, so there only candidates
  are paired.

Team aliases are resolved through `teams.py` and `aliases/`, which has a
file for each sport, since leagues reuse codes (DAL is the Cowboys and the
Mavericks), and an empty one for a sport whose futures name only people,
and for an esports title, whose teams go by name. The new leagues' files
list each venue's codes apart, and the Champions League and Europa League
are two sports, since Kalshi's VIK is Viking in one and Plzen in the other.
The national teams' file, `intl.json`, which the women's share
(`SHARED_ALIASES`), was built from both venues' own team lists, open and
settled, joined by country: 163 countries both list, each with every code
each venue has used, Croatia being CRO and HRV on both, Chile CHI on Kalshi
and chl on Polymarket US, Malawi maw and mala there, and Malta mal. A
country only one venue has listed is left out until both do. Player names
are normalized to a key that ignores accents, punctuation, and suffixes,
reads a hyphen as a space, and turns letters like ø into o, so Kalshi's
*Ronald Acuña Jr.* is Polymarket US's *Ronald Acuna* and *Martin Ødegaard*
is *Martin Odegaard*, and the few names the venues spell apart are in
`PLAYER_ALIASES`. A market on no one, Kalshi's *Vacant* title, is never
paired. Lines are made strict, so *1000+* on one venue and *over 999.5* on
the other become the same bet. The venues number seasons differently, so a
future's season is the one it settles in, or on Polymarket US its event's
end when the slug's date is half a year earlier, since the Champions League
final's slug carries the wrong year. Polymarket US's futures do not always
use its games' team codes, gluing city and nickname, `bufbil`, or borrowing
Kalshi's, `gsw`, so a future's team is the code whose team its market title
could name. Contracts no parser understands are counted and left out.

**match.py** groups a sport's bets whose identity agrees into a `Pair`,
whose label starts with the sport, for example `nfl champion 2027 KC`, `nfl
spread 2026-09-20 CAR@ATL ATL 3.5`, or `politics senate_race 2026 GA D`. A
pair only exists when both venues list the bet, and it carries every
contract that expresses it. In tennis, darts, the UFC, esports, and
national teams' soccer the venues may date one match a day apart, Kalshi
and Polymarket US reading a match in Asia by different clocks, an esports
match late in the American evening Kalshi by Eastern time, and a national
team's Kalshi by UTC, Mexico against Chile at 02:30 UTC being October 6
there and October 5 on Polymarket US. A date only one venue lists for two
sides then joins the one a day off that only the other lists. Two sides
can also meet on days in a row, as in a darts round robin, so a date both
venues list, and two dates one lists, stay matches of their own. Kinds with
settlement rules that differ between venues carry a note from **notes.py**:
how each venue treats a postponed game, overtime, a retired tennis player,
a fight's draw, a darts walkover, a drawn esports match, which Polymarket
US pays at 50 cents and Kalshi's rules leave unsaid, and a driver who does
not finish; Polymarket US divides the
dollar among players or teams that tie for an award, a lead, or a record,
where Kalshi's rules do not always say; Kalshi reads a UFC title holder and
a tennis ranking at noon Eastern on December 31 and Polymarket US at 11:59
PM; and Kalshi pays a race on the party of whoever is sworn in, in January,
and control of a house on February 1, Polymarket US on the election. A note
is a warning to read, not a bar.

**pipeline.py** runs fetch, classify, and match in one call. The live
process runs it every hour in a child process, so new games and futures
enter the pairs while it runs. A refresh of every sport takes some eight
minutes,
the NFL's games and the elections the most of it. A dry run on October 4
paired some 5,000 game and event bets, 3,400 of them the NFL's, and 4,700
futures; 227 of the game pairs, every kind among them, were read against
both venues' own wording, and each named the same game, side, and line.

### Venue clients (`src/api`)

**bookstream.py** is the shared websocket loop: one connection per venue
carrying every wanted contract, a live book per contract, changes to the
wanted set applied without reconnecting, and a reconnect that is immediate
on the first drop and backs off only on repeated ones, with the stretch
until the new subscription is confirmed stored as a gap, and the books of
that connection's contracts dropped until it sends them again. Each change
is passed on with the venue's own time for it, when the message gives one,
and what the venue says of a market's trading is passed on as it changes,
kept across reconnects since a venue may not say it again.
**kalshi.py** signs each connection and request with RSA-PSS and holds the
whole catalog on one connection, on the hosts Kalshi dedicates to API
traders, `external-api`, where a signed call took 17 ms against 27 on the
old host. It also reads the cash on each exchange shard, moves cash between
shards and sets the split Kalshi keeps them to, for
`tools/kalshi_shards.py`, and reads when the key's location attestation
lapses, past which Kalshi refuses the key for sports markets. Its
connection also follows every market's lifecycle, a channel that takes no
tickers, and says when a market it carries is paused or trades again,
closes, or is decided. Since a connection lost may miss a pause's end, the
next one reads the markets still said not to trade from the markets call.
It also reads whether the exchange trades, shard by shard, from its
public status call.
**polymarket_us.py** signs with Ed25519 and subscribes to books in requests
of up to 100 slugs. The feed takes ten requests on a connection, so a
connection carries 1,000 markets, and has no unsubscribe, and every catalog
refresh that adds contracts spends more, however few it adds. So new
contracts go to a connection with requests left, a new connection opens
when none has any, and contracts a connection refuses anyway, as one
request too many, move to another. Updates are not batched, since batching
held our view of the books behind the venue's. Every book carries its
market's state, and one not open, suspended, halted, expired, and the
like, is said not to trade: 109 of 3,000 markets the recorder followed on
2026-10-07 came expired. Its trade feed was dropped
on September 30, since trades came no sooner than the book that showed
them. Both clients also report how a contract resolved, which the settler
uses, and carry the live trading calls: the account's balance, and an
immediate or cancel limit order whose answer they turn into an `Answer`,
the same for both venues. Polymarket US prices every order on the long
side, so a short side order at p is sent at 1 - p, and when its answer does
not say how an order ended, the order itself is looked up, since a returned
order id does not mean the order is done. Both venues trade in hundredths
of a contract: Polymarket US fills an order in pieces as small as one, 0.1
then 0.89 then 0.01 for a contract, and takes orders that small, and Kalshi
counts contracts to the hundredth too. So the pieces are added up and every
fill, holding, and order is counted to the hundredth (`orders.exact`). Both
clients also read the account's positions, which the live executor and
`tools/repair_fills.py` check the trades against. Kalshi turns away an
order whose market's shard lacks the cash, which counts as unfilled rather
than refused, since nothing traded. Polymarket US turns away an order the
account lacks the cash for, and Kalshi a sale it lacks the cash for, which
counts as `unfunded`, not refused either. A sale can need cash: each venue
keeps one position per market across all our trades, so selling the No one
trade holds, in a market where others hold more Yes, is buying Yes. So a
Kalshi sale is not reduce only, which would cancel such a sale unfilled
every time. An order either venue turned away because its market or the
exchange was not trading, Kalshi's `MARKET_NOT_ACTIVE` or
`trading_is_paused`, Polymarket US's exchange closed, counts as `closed`,
not refused either (`orders.closed`). Trading calls go over kept HTTPS
connections from **http.py**, since a new TLS connection costs round trips
a race cannot spare, and are never retried, since an order sent twice
trades twice. The field names come from the venues' published Python SDKs.

### Engine (`src/engine`)

**run.py** is the process that runs. Its `Session` wires the recorder, the
venue connections, the scanner, and a `Desk` for each mode it trades in
together, and a one-second timer ticks it, as [What happens
when](#what-happens-when) describes. A desk is one mode's executor, its
money, and its settler. `--execute` picks the desks: `paper`, the default,
`live`, or `both`, which runs a desk of each. The desks trade apart: the
paper desk the bets on one event, a pair with a game date, and the live
desk the futures, a pair without one, bar the sports given to `--not-live`,
none by default, whose futures are followed but traded by neither. On
one signal the live desk's real orders would take the contracts the paper
desk's simulated ones look for. Each desk still flattens and settles every
trade it holds. With `--live-in-play`, which needs `--execute live` or
`both`, the live desk is offered the bets on one event too, and trades them
once under way, see **trading/** below; with paper running too, both take
those signals, and paper is given back what live's orders took. The same
loop starts the hourly catalog refresh in a child process and applies the
result to the live connections. One
run trades every sport given to `--sport`, comma separated as in
`--sport nfl,ncaaf,mlb,nhl,nba`, or every one with `--sport all`, since the
money is one pool and a second process would spend the same dollars. How
long a game is expected to last is set for every sport in `GAME_HOURS`,
measured for the first five and an allowance for the rest, which for
Dota 2 has been short, see **trading/** below. The pieces it
wires together are in
`engine/components/`, in three folders by what they do: `market/` follows
the venues, `trading/` makes the trades, and `money/` keeps the cash. What
they share is in `engine/helper/`: the settings, game timing, pricing, and
fees.

**market/** follows the venues: their books and the edges between them.
**record.py** holds the newest book for every paired contract in memory,
five levels a side, which the scanner prices and the executors trade
against. Books are not stored, only the gaps when a venue's feed was down.
A game's contracts on both venues are followed until five hours after
kickoff, whatever their close times say, since Kalshi's close is its guess
at the final whistle, three hours in, which most games outlast. Every
minute its status line says, for each venue, how many changes came, how far
behind the venue they reached us, median and 90th percentile, and how much
of that was ours. **feeds.py** holds a venue's connections, one, or several
when the venue caps how much one connection may carry, and applies catalog
changes to them in place, each new contract going to a connection with
room. Each venue's feed runs in a child process of its own, so receiving,
parsing, and keeping the books use another core. The child passes each
changed book's best levels over a pipe, only the newest of a contract when
the main process falls behind, and a child that dies is started again, with
the stretch stored as a gap. **streams.py** gives each venue its feed and
passes the books to the recorder. With `--set feed_processes=false` every
feed runs in the main process instead.

**scan.py** prices every pair whose member's book just changed. Using
**pricing.py** it walks the ladders to find the cheapest way to hold yes
and the cheapest way to hold no across the pair's members, on two different
venues, including the venue's taker fee from **fees.py**, Kalshi's rounded
up to the hundredth of a cent and Polymarket US's to the cent, once per
order, as the venues charged our orders. Two contracts on
one venue, such as a game's two teams on Kalshi, are priced by the same
traders, and a gap between them is gone before both orders land: in the
in-play test 52 trades with both legs on Kalshi matched 5 of the 247
contracts they asked for, live and paper alike. A future's two legs may
still be two contracts of one venue with the same rules word for word
(`pricing.same_rules`, from a digest of each contract's rules kept with the
pairs): Polymarket US lists each college and pro football win total twice,
once in a market of every team's and once in the team's own, and from
2026-09-30 to 10-05 live traded 255 such pairs, locking in $103 on $978,
until the legs were kept off one venue altogether, to 2026-10-06. An
episode is a stretch where the net edge stays positive. When it ends it is
stored as an `Opportunity` with its legs, duration, peak edge, how many
contracts the recorded depth would have filled at the peak, and the return
on the capital tied up, annualized as if held until the bet pays out. While
an episode is open the scanner offers it to each executor on every update
until that executor takes a trade, and then not again, so one mispricing
makes one trade, on a game. On a future, from 2026-10-08, a trade that
matched something and left nothing to flatten gives the episode back once
it is done (`Executor.again`, `Scanner.offer_again`), so an edge that
lasts past the trade is traded again; one that matched nothing, its quote
likely gone, ends it as before. The live executor is offered it first.
The edge coming back after it has gone is a new episode. An episode also
keeps its longest stretch at `MIN_EDGE` or more, and the contracts that
stayed fillable through all of it, which is what an order sent any time in
the stretch could have had. From 2026-10-05 it keeps too what one order
could have had as live takes it: the contracts on the levels at live's
least edge or more, `pricing.live_min_edge()`, 2 cents on a game under way
and 100% a year on a future, counted only once the edge has lasted
`LIVE_HOLD_SECONDS` there, a game's from 2026-10-06 and a future's from
2026-10-07, and what they lock in, and whether that moment came with a
change of the Polymarket US leg's book (`pm_changed`), the only moment live
traded a game under way from 2026-10-05 to 10-06. The summary counts
opportunities by that, since live's own fill empties the levels it takes.
It is the best of the moments at the peak's edge, and of every moment while
none of those had offered `LIVE_MIN_CONTRACTS`, the fewest live opens a
trade with, a tenth of a contract, a whole one until 2026-10-07
(`Scanner.weigh_take`): a peak's first moment can offer a sliver of a
contract, and live trades a moment later at the same edge or a little less.
Until 2026-10-06 only the peak's first moment counted, and 21 of the 55
live futures trades that evening came from episodes kept with under a
contract, one of them filling 30. A book goes stale after a minute only
once its game may have started: a future's markets, and a game's before
kickoff, can rest unchanged for hours while they are open, so their books
are priced however old they are. So a future's member past its close time
is left out (`game.past_close`), since its market has stopped or its bet is
expected to be decided, while a game's is not: Kalshi gives a game's
contract the time it expects the game to end, which many outlast, and its
markets say themselves when they close. From 2026-10-08 a member is left
out too while its venue is in its weekly maintenance
(`venues.is_maintenance`), when its book may freeze, or go on changing,
though no order can trade at it, so an episode open as the window begins
ends then and none opens on the pair until it is over. Live has not traded
in the windows since October 1 (`Executor.trading`); the scanner had gone
on keeping episodes there.

**trading/** trades the signal. **executor.py** holds what paper and live
share, which is everything but how an order is filled. Live trades the
futures, which pay out a day or more away, and with `--live-in-play` the
games, matches, and races once under way that pay within 24 hours
(`MAX_PAYOUT_HOURS`), below. Paper trades the bets on one event
(`in_play`), before and while they are played, that pay within 24 hours
(`MAX_PAYOUT_HOURS`), to see how they would do. A signal needs a net edge
of at least two cents per contract (`MIN_EDGE`, five until 2026-10-04 16:03
UTC; live on a game under way, `LIVE_IN_PLAY_MIN_EDGE`, five cents from
2026-10-05 and two again from 2026-10-06), for live a payout at least 24
hours away (`MIN_PAYOUT_HOURS`), and a return of at least 100% a year on
the money it ties up until then (`MIN_ANNUAL_PCT`, 50% until 2026-10-06),
which is what weighs an edge against the time it ties the money up; the two
cents only keep out the noise of a cent or so. From 2026-10-05 a game under
way is asked no return a year: it pays within a day, where two cents
returns over 700% a year, so the rate would never turn one down. Two cents
clears 100% a year for a bet paying within 7 days, five cents within 19,
ten within 40, twenty within 91. One limit order is sent per leg, live's
Polymarket US order first with a leg on each venue, below. Both ladders are
walked together and each leg's limit is set at the deepest level that still
leaves the minimum edge, so an order sweeps every level above the floor
rather than only the top one. A trade asks for all of what those levels
show (`FILL_SHARE`, half until 2026-10-04), as far as the cash free on each
venue pays for, fees included, both legs from one venue's cash when they
share it, live as on paper, with no cap on contracts but live's in play,
below. Until 2026-10-10 the fees were left out, so a trade the cash sized
could be refused for its fee: Kalshi turned away trade 4003's 48.14
contracts at 53 cents on a shard holding about their 25.51$, for the 0.84$
fee, after Polymarket US's leg had filled, which was sold back for 2.09$
less. No cash is held back: trades may spend all that is free. Since
2026-10-05 live's orders on a future sweep only the levels that themselves
return `MIN_ANNUAL_PCT` a year until it pays, as the top must, not every
level down to `MIN_EDGE`: for a future four months out that stops at about
14 cents (`LiveExecutor.min_edge`, `pricing.edge_for_annual`). The two
cents never decided whether a future was traded, since every one pays 27
days or more out, where 100% a year needs 6.9 cents, and 50% needed 3.6.

In **paper.py** each order goes the way a live one does. It takes a trip
to its venue and a trip back, each drawn from a lognormal with the median
and 90th percentile the live orders took (`PAPER_ORDER_MS`). The trip there
ends at the venue's own time on the order, the clock it stamps its books
with: Kalshi 12 ms, Polymarket US 59 for an opening order and 24 for a
sale, and back 8 and 31. The order fills against the book the venue had at
that moment, the newest it made by then on the contract's tape. Our copy of
a book runs behind the venue's, Kalshi's some 12 ms and Polymarket US's
some 85, so the order waits until a book the venue made later has reached
us, which shows every change up to then has, or `PAPER_FEED_SECONDS` when
none comes. The answer comes back after the trip back, and a leg that
filled short is sold back on the books we had seen by then, as live would,
the sale meeting the venue's book when it would arrive. `FILL_SHARE` of
the visible size at a level is asked for, all of it, and no order is
rejected at random (`PAPER_REJECT_PROBABILITY`), since none of the live
ones was: of 1,183, three lacked the cash and three came in Kalshi's
maintenance, which paper turns away as live does. A paper order leaves the
venue's book as it was, so paper remembers what it took from each level
and takes it off the books it later sizes, fills, and sells into, until
the level shrinks below that or goes. In **live.py** each order is a real
immediate or cancel limit order, sent on a thread pool of its own, and
stored in the orders table before it is sent and again with the venue's
answer. Either way a leg that filled short is flattened at once by selling
the excess back on its own venue, even when buying the missing side on the
other venue would cost less, since a sale frees the money now and a
purchase would hold it until the bet pays. A sale goes no lower than half
of what the contracts cost (`MIN_SALE_SHARE`): below that it would give
away most of what was paid, so the contracts are kept, a bet that may
still pay out, until a bid worth taking comes. Whatever stays exposed is
tried again on every tick until it is flat or the settler settles it, past
the payout time we expected too: a game can outlast it with its market
trading, and a market that has stopped trading sells nothing. Until
2026-10-10 a trade was given up at its payout time. Live trade 4135, a
Dota 2 map signalled 56 minutes past its own, sold back 25 of the 50
contracts Polymarket US filled over Kalshi's none, and the other 25,
14.43$, were never offered again, though the pair traded again 29 seconds
later and twice more by 20:45 UTC. From 2026-10-05, 6 of 19 Dota 2 trades
in play, 1 of 8 LoL and 2 of 154 tennis were signalled past their own
payout time. A trade is now let go only a day past it
(`FLATTEN_PAST_PAYOUT_HOURS`), for contracts that never resolve. A sale
that filled nothing is tried again only a minute later
(`SALE_RETRY_SECONDS`), since a book can show a bid an order never
reaches. A sale its venue turned away for lack of cash is
tried again only once the cash there has grown, by a payout, a deposit, or
another sale, rather than sent again every second. A restart takes back
from the trades table whatever is still exposed, by the same rule, so a
crash or a deploy does not leave it unhedged. A live sale is
limited to the deepest price the books said it would reach, so a book that
moved leaves the rest for the next tick rather than filling far from its
price. Once a game has started, orders and flattening only use a book that
has changed within the last minute (`pricing.fresh`), as the scanner does,
since a market that has closed may stop changing rather than empty its
book, and its last book cannot be traded. Every trade is stored as soon as
it is sent and updated when it is done.

Both venues take orders and fill them in hundredths of a contract, so a
live trade opens in hundredths, with as little as `LIVE_MIN_CONTRACTS`, a
tenth of a contract, on a future or a game alike (`Executor.fewest`).
Until 2026-10-07 it opened whole contracts, at least one. A leg may fill to
the hundredth, 6.42 of 7 for one, and live trading counts it so: the other
leg's 0.58 over is sold back like any excess, in an order for 0.58 of a
contract. A Kalshi order sent after Polymarket US's, below, asks for what
that filled, to the hundredth. Paper trading keeps to whole contracts, as
its fills are worked out from the books (`Executor.step`). Every
`LIVE_POSITION_SECONDS`, 5 minutes, the live executor reads each venue's
positions and compares them with what the open live trades hold of each
contract (`check_positions`). A difference of a hundredth or more means the
records are wrong, so it is logged once and left to a human, see
`tools/repair_fills.py`. Nothing is compared while a trade or a flatten is
in flight, or when an order went out while the positions were read.

A leg on Polymarket US trades only on a current book. That venue's books
reached us about 85 ms after it changed them at the median, 160 at the 90th
percentile, and Kalshi's in about 12, so after a score Kalshi's new price
could sit next to Polymarket US's old one for a moment, an edge already
gone there. Before a trade the executor compares the venues' own times for
the two books (`confirm_wait`). A Polymarket US book newer than the Kalshi
book's last change already shows any reaction to it. An older one is
trusted only once that change is `CONFIRM_SECONDS`, 0.3 seconds, old, long
enough for a reaction on Polymarket US to have reached us, and until then
the edge waits for the scanner to offer it again at the next change, or
when the wait ends, when the executor asks the scanner to price the pair
again. A Kalshi leg has no such wait, since its feed is fast. Live, whose
Polymarket US order goes first, counts from the Kalshi change as of when
the edge reached its least edge, from 2026-10-07: a change since that
leaves the edge there does not start the wait again
(`LiveExecutor.opened`), the scanner keeping when each book had changed as
the edge began (`Scanner.edge_books`). A Polymarket US quote no longer
there costs it only an order that fills nothing, and in a game under way,
where Kalshi's books change many times a second, an edge could otherwise
wait as long as it lasted. Paper counts from the last change. Both venues
stop every Thursday for maintenance they publish, Kalshi from 3 to 5 AM
Eastern and Polymarket US from 6 to 8 AM, while a feed may go on sending
books, so no trade is opened, and no order sent to flatten one, with a leg
on a venue in its window (`venues.is_maintenance`). Kalshi says at any
other time whether it trades, shard by shard, read every 30 seconds
(`market/exchange.py`), and nothing is opened or sold back where it has
stopped either (`Executor.trading`). A market that has stopped on its own
is never offered, as its venue says (`market/record.py`), and one that
turned a live order away as not trading, `closed`, is left alone for ten
minutes, or until its venue says it trades (`Recorder.refuse`). Until
2026-10-07 such an order counted as a refusal, and three in a row would
have halted live trading. Live trading takes every sport the run does. A
Kalshi leg spends only the cash on its market's shard: football's,
hockey's, soccer's, the national teams' too, motorsport's, UFC's, darts',
esports', and politics' on shard 0, and baseball's, basketball's, and
tennis' on 3, crypto being on 2. So `tools/kalshi_shards.py` splits the
Kalshi cash between them, 80% to shard 0, where the football futures with
the long-lasting edges are, and 20% to shard 3, with nothing on shard 2
(`LIVE_SHARDS`). Until 2026-10-05 it was 90% to shard 0 and 10% to shard
3, with nothing on 2, and live had made no Bitcoin trade; from then until
2026-10-10 80/10/10, but in the two days before the change Bitcoin's 31
window trades filled nothing, its futures were held out, and its shard sat
untouched at 54.09$ while shard 3 ran dry; the same day Bitcoin left the
catalog. The live executor emails once when a shard in the split, or
Polymarket US, falls under $5 (`LIVE_LOW_CASH`), and again only after it
has been back over.

**Edges that last.** From 2026-10-05 04:07 UTC live traded an edge only
once it had stayed at `MIN_EDGE` or more for half a second, unbroken, as
the scanner's episode timed it, and the scanner offered it again when the
half second was up. In its first hours no in-play edge passed, the largest
lasting 6 to 110 ms, and the user dropped it with the in-play rules below:
live took a future's edge the moment it saw it, as paper does. From
2026-10-06 it is back for games under way, and from 2026-10-07, a tenth of
a second, for futures too, each at live's least edge on it, the rate a
year on a future and two cents on a game, below.

**Games in play.** With `--live-in-play` live also trades the games,
matches, and races under way, of every sport, Bitcoin's windows too until
2026-10-10: once the game has started by any member's kickoff, since a
Kalshi contract gives none, paying within 24 hours (`MAX_PAYOUT_HOURS`),
sized as any trade, by the books and the cash. Its Polymarket US order
goes first and its Kalshi order only once that has answered, for what it
filled, and not at all when it filled nothing (`Executor.fill_legs`);
paper does the same in play. Sent at once the Kalshi leg lands first, in
some 12 ms against Polymarket US's 59, and another trader may take the
Polymarket US quote away once ours trades, leaving the Kalshi leg to sell
back at a loss. Sent first, a Polymarket US leg that misses leaves nothing
to sell back, at the cost of Kalshi's going out some 80 ms later. From
2026-10-06 live sends a future's Polymarket US order first too: sent at
once, Kalshi's futures orders had taken something 99.4% of the time and
Polymarket US's 94%, and of 82 trades left on one leg 79 held Kalshi's,
which cost $12.85 to sell back or stayed exposed. Paper still sends both
at once on a game before it starts. With paper running
too, both take the same signals, and since our live orders are real and
take from the books paper's orders meet, each leaves a footprint
(**footprints.py**): from the venue's time on its answer and the book on
the tape just before then, what it took from each level. Paper adds that
back to every book the venue made after, until the level falls below what
our order left of it, as other takers or cancels would have taken ours
too, and waits up to 2 seconds for the answers of live orders sent before
its own. The brakes cover these trades as any live trade.

This follows the in-play test of 2026-10-04 17:49 to 10-05 01:27 UTC. Live
traded 200 games under way at no more than 5 contracts, each beside a
paper twin on the same signal with the same legs, limits, and size, the
trades with a leg on each venue sending both orders at once and Polymarket
US's first by turns. On those 147 trades live matched 111 of the 500
contracts asked for, paper 278: live's Polymarket US legs filled 56 times
and paper's 106, while their Kalshi legs filled alike, so paper is
optimistic in play, most of all on the NFL. Live's 69 sent Polymarket US
first made $3.84, matching about as many contracts as the 78 sent at once,
which lost $3.84 with 52 of them left on one leg to sell back, against 12.
The 52 trades with both legs on Kalshi lost $7.21, which is why the legs
are now on two venues, but for a future's two contracts of one venue with
the same rules, above. The `twins` table holds the test, and
`tools/in_play_test.py` reports it.

In play live's Polymarket US orders miss most when they buy No, selling
into the Yes bids: from the test through 2026-10-05 04:07 UTC 14 of 62
took something, against 57 of 103 buying Yes and 95% either way on the
futures. Polymarket US took each order that missed and let it expire at
once, nothing at its price, while its feed went on showing that price,
once for 18.8 seconds through four orders, all of which paper filled. A
market halted during a play with its last book still up would look like
that, since a book counts as current in play for up to `MAX_BOOK_AGE`, 60
seconds, without a change. Since 2026-10-05 every live order stores when
its book last changed, and the summary shows how often orders took
something by how long the book had been quiet, to tell.

**In-play rules.** Since 2026-10-06, at the user's asking, live on a game
under way takes an edge of two cents or more (`LIVE_IN_PLAY_MIN_EDGE`), and
sweeps the levels down to that, but only once the edge has stayed at
two cents or more, unbroken, for a tenth of a second
(`LIVE_HOLD_SECONDS`, half a second until 2026-10-07), as the scanner's
episode times it (`LiveExecutor.hold`, `Scanner.edge_since`): one that
ends sooner is never traded, and one still there when the time is up is
priced again then (`recheck`) and traded, whichever venue's book changed
last. The Polymarket US book must still be current (`confirm_wait`), a
separate wait timed from Kalshi's change as of when the edge reached two
cents: when Kalshi's change opened the edge, the Polymarket US leg waits
until that change is 0.3 seconds old (`CONFIRM_SECONDS`), unless a newer
Polymarket US book still shows the price, and a Kalshi change since that
leaves the edge at two cents or more does not start the wait again, as
until 2026-10-07 it did. So an edge Polymarket US's change opened is taken
at a tenth of a second, and one Kalshi's opened at 0.3. A future's edge
waits the same from 2026-10-07, at the rate a year rather than two cents,
taken at once until then, and from 2026-10-08 a future's edge offered
again after a trade waits as long again from when the trade was done. A
trade asks for no more than 20 contracts (`LIVE_IN_PLAY_CONTRACTS`), 100
from 2026-10-10 13:01 UTC to that evening, 50 from 2026-10-08, and 10
before, its limits going no deeper than the levels
that hold them, and a tenth of one or more, with no limit on how many such
trades. Each trade says whether its game was under way at the signal (the
trades table's `in_play`, null before 2026-10-05). Lasting does not make an
edge real: the two Bitcoin window episodes of 2026-10-05 that showed $5,304
stayed at two cents or more for one and two minutes, likely a book left
standing on a market no longer trading; when that book is Polymarket US's,
its order going first fills nothing and no Kalshi order is sent.

From 2026-10-05 to 10-06 the rules were others: five cents at once, but
only on the signal a change of the Polymarket US leg's book brought, not
one a change of the Kalshi book brought, a recheck, or the tick, at most 5
contracts a trade, for 200 trades. In seven hours of games they let one
episode through, an MLB game winner at five cents, and no trade was made.

Live trading has brakes, in **brakes.py**, sized for a test with about $100
on each venue. An order whose outcome cannot be known (a timeout, a dropped
connection, a venue failing on its side, or an answer that cannot be read)
sets its trade aside (`LiveExecutor.set_trade_aside`): no more orders are
sent for it, since what it holds is unknown, and the log says which order
to look up. No email goes out for it. Trading goes on. It halts, sending no
more orders of any kind, when the orders themselves fail:

- 3 of the last 20 orders had an unknown outcome, three in a row or a
  steady error rate. The halt names each of those orders, to look up;
- one venue refused its last 3 orders. A refusal is the venue answering
  that it will not take an order, so nothing traded: not authorized, not
  enough money, a bad price, or too many requests. An order that found
  nothing at its price is unfilled, not refused, as is one Polymarket US
  turned away for no liquidity or for being slow, one either venue turned
  away for lack of cash is unfunded, not refused: the venue works, and the
  low cash email tells a human, and one turned away because its market or
  the exchange was not trading is closed, not refused: the venue works,
  and the market is left alone until it trades again.

It halts new trades, but goes on flattening what is exposed, when the
live trades decided in the last 6 hours lost more than 10% of the live
money, net. If the orders then fail as above, flattening stops as well.
That is checked after each trade, each settlement, and each retry that
sold something, not after a retry that sold nothing, which runs every
second while a trade is exposed.

A trade is decided once its legs hold the same number of contracts, which
pay a dollar each whichever way the game goes, or once it settles. Each
rule reads the orders and trades tables, so a restart does not reset it,
and starts over after a halt. A halt is logged and emailed, and what is
held is still settled. It is also written to `data/live_halt.txt`, and
live trading stays halted across restarts, a crash or a deploy, until a
human has checked the venues and removed that file.

**notify.py** tells a human by email when live trading needs one: when it
halts, when the cash on a live venue or Kalshi shard runs low, and when
the Kalshi key's location attestation is two days from lapsing, and again
once it has, since past it Kalshi refuses the key for sports markets until
a person renews it on Kalshi. Its `AttestationWatch` reads the date every
hour, whether or not live trading is on, since the feed uses the same key,
and sends each attestation email once, even across restarts. Every alert
is stored in the alerts table and emailed in a background thread through
the SMTP server in `data/email.json`.

**money/** keeps the cash: paper money in **paper.py** and live money in
**live.py**, with what they share in **balances.py**, and the settler that
pays out trades in **settle.py**. Money has the same shape in both modes:
free cash per venue, `reserve` and `release` for an order in flight, and
`apply` for a cash movement. Each paper venue starts with $10,000. Money
for an order in flight is reserved before anything is awaited, so two
signals in the same moment cannot spend the same dollars. Every cash
movement is a `Ledger` row that records the balance it left behind,
starting with a `transfer_in` of each venue's opening balance, so the
ledger accounts for every dollar and a restart reads the newest row instead
of replaying history. From kickoff, every 30 seconds, the settler asks the
venues how the contracts of open trades resolved, pays the winning leg a
dollar a contract, and stores each leg's result, payout, and settlement
time as the trade's `Settlement`, which is what a tax return needs. A trade
still exposed on one side is settled as it stands, each leg paid for what
it holds. The settler skips a trade while an order to flatten it is in
flight, and once a trade settles the executor stops flattening it. The
venue holding a winning leg receives the whole dollar, so the balances
drift apart over time. Nothing moves money between venues: paper trades
until a venue's cash runs out, and live emails when one runs low, for a
human to move money by hand.

Live money has no ledger of ours. `LiveBalances` reads each venue's balance
every 15 seconds, which also keeps the venue's kept HTTPS connection warm
for the next order, and at once after a payout, and applies what our own
fills move in between, so a burst of trades does not spend the same dollars
twice. Nothing is traded before the first reading. Kalshi's cash on each
exchange shard comes in the same reading and is kept the same way, and an
order on a shard spends no more than that shard has free, whatever the
venue as a whole has. The settler settles live trades as it does paper
ones, storing each as a `live` `Settlement`, while the venue pays out on
its own.

### Tools

`src/tools/summary.py` prints a short report from the database: its size
and the pairs of each sport in one line, and feed drops. Then everything on
the futures under a heading of its own, then everything on the games in
play, or one of them with `--market futures` or `--market in-play`. Each
market starts with its opportunities as live takes them: an episode
counts when one order could have had `LIVE_MIN_CONTRACTS` or more, a
tenth of a contract, on the levels at live's least edge or more, the levels
returning `MIN_ANNUAL_PCT` a year on a future paying `MIN_PAYOUT_HOURS` or
more out, and those at `LIVE_IN_PLAY_MIN_EDGE` or more on a game, match,
or race under way paying within `MAX_PAYOUT_HOURS`, once the edge
has lasted `LIVE_HOLD_SECONDS` there, as live waits for it to, a future's
from 2026-10-07, the moment being the scanner's (`Scanner.weigh_take`).
Lasting does not make an edge real: two episodes of one Bitcoin window on
2026-10-05 showed $5,304 to be locked in at 18.7 and 51.8 cents for one
and two minutes, though both venues settle it on the same index. It shows
what those orders could have taken and
locked in at full size, how long the edge stayed at `MIN_EDGE` or more, in
seconds to the thousandth, at the median, the 90th percentile, and the
longest, the same by sport and kind, and the largest five. Every count of
contracts in the report is to the hundredth live trades in. An edge on less
than a tenth of a contract, a sliver of a Polymarket US level that can
last minutes, is left out, since live opens a trade with no less and so
could never take it. The tenth holds for the whole window, though live
opened whole contracts until 2026-10-07, so earlier days show episodes it
could not take then. Until 2026-10-05 an episode counted only with a whole
contract fillable through all of its longest stretch at `MIN_EDGE`, which
left out the very episodes live traded, since its own fill empties the
levels it takes: of the two futures it traded in the hour after the 15:14
UTC restart, one stretch kept 0.29 contracts and the other 0.01. Episodes
from before the scanner kept what one order could have had, and ones still
open, which are stored only once they end, are left out, and so are those
whose peak came while a leg's venue was in its weekly maintenance, which
the scanner kept until 2026-10-08. The market then
shows, for paper and then live, or one of them with `--mode paper` or
`--mode live`, the trades by outcome, filled, partial, then failed, and by
sport and kind in the window, the five holding the most capital, the legs
settled in it by venue, and the open trades: in a few lines, how many, how
many of them opened in the last hour, day, and week and the capital those
still hold, the capital they all hold on each venue and the profit they are
expected to return with its rate a year weighted by capital, and the first,
average, and last dates they resolve, the first maybe past and waiting on a
venue, the average weighted by capital; then a table of each open trade
opened in the window, newest first, with what it holds, its capital,
expected profit and rates, and when it pays. Live's trades end with its
real orders sent by venue and what came back, then its orders to open by
venue, the side they bought, and how long the venue had sent nothing for
the market when each went out (0-1, 1-5, 5-30, 30+ seconds), with how many
took something. The paper money from the ledger and the live balances read
from the venues now, with Kalshi's shards, come last. Each section is a
title line with its details on indented lines under it, and a long list,
such as the pairs of each sport, wraps at 100 characters. `--hours` sets
the window, `--sport nfl,ncaaf` narrows everything to some sports, and
`--no-live` leaves out the live balances.

`src/tools/live_check.py` reads both venues' balances with the keys in
`data/` and says when the Kalshi key's location attestation lapses, and
with `--email` sends a test email, without trading. Run it before a live
run.

`src/tools/kalshi_shards.py` splits the live Kalshi cash between the
exchange shards live trading uses by the percents `LIVE_SHARDS` gives them,
80% to shard 0 and 20% to shard 3, a shard left out, Bitcoin's 2, giving up
all it has. It reads each shard's cash and says what it would move, and
with `--apply` moves it, then sets Kalshi's own target split to the same
shares, which Kalshi keeps every 10 seconds, payouts included. It does not
refill a shard whose cash orders spend: at 50/50 on 2026-10-01, shard 0
had spent down to $6.94 of $51.77. So run it again when one shard runs
low. The money stays in the account, and nothing is traded.

`src/tools/repair_fills.py` repairs the live trades recorded under an
older reading of Polymarket US fills: before fills were added up, when an
order filled in pieces was stored as filled short or not at all, and
before they were counted to the hundredth, when 6.42 was stored as 6. It
reads every stored Polymarket US answer again as the client now does,
corrects the orders read differently, and works out each of their trades
again from its orders, as the executor does, and the live executor
sells back at its next start what a repaired trade holds on one side more
than the other. A bet whose orders sold more than it held is left for a
human. Last it compares what the trades hold with each venue's positions.
`--trade` names a trade whose record fell behind its orders for another
reason, to be worked out again from them too, as trade 48 was when a
sale of 0.42 of a contract went unrecorded. Without `--apply` it writes
nothing, so it also serves as a check that the live records match the
venues. Stop the recorder before `--apply`.

`src/tools/in_play_test.py` reports the in-play test of 2026-10-04 to
10-05: the live trades in play beside their paper twins on the same
signals, for those sending both orders at once and those sending Polymarket
US's first apart, how many of each filled in full, in part, on one leg, or
not at all, the contracts matched, how often each venue's leg filled of
those sent, what was locked in and what the sales back made, the result of
those settled, each venue's round trip, measured live and drawn on paper,
how often the two matched the same contracts, and the newest pairs one by
one, `--recent 20` of them.

`src/tools/latency_report.py` reports, over a stretch such as a game, how
far behind the venues the books ran, minute by minute from the status
lines in `data/record.log`, how long the real orders took there and back
and how many filled, how long the edges lasted, and how many edges waited
for a Polymarket US book to catch up. `src/tools/feed_check.py` compares
ways of following the books side by side on the same markets, without
trading: Polymarket US's full book with and without compression, its
best prices only feed, and its trade feed, and Kalshi's order book on its
old host and on `external-api`, each with and without compression.

## Deployment

The live process runs on a c7a.large in us-east-1, the region Polymarket
US's exchange runs in, about 10 ms from Kalshi's API hosts in us-east-2. A
signed round trip is about 35 ms to Kalshi and 30 ms to Polymarket US. A
systemd service, `sportsarb-recorder`, starts `python3 -m engine.run
--sport all --execute live` from
`~/SportsArb/src` on boot and restarts it on any exit, and the process
starts a child for each venue's feed and each catalog refresh, which stop
with it. The venue API keys live in `data/`, which is
gitignored, and are copied to the instance by `scp` only. Deploying is
`git pull` on the instance, the tests, and a service restart only if they
pass, which refreshes the catalog for some eight minutes and then
resubscribes. Each run logs the commit it runs and every setting when it
starts, so the log says what produced its results. The instance was first
placed in Mexico to reach polymarket.com, which was then dropped as a venue
for legal reasons in favor of Polymarket US, and moved to us-east-1.

`commands.txt` holds the commands used to check the data, deploy, and
operate the instance, with the instance's address, key, and ids written
into them. It is gitignored, so it lives only on the machine that operates
the instance.

The process is light. The main process held some 14,000 books in about
260 MB of memory before the games came back, which double what it follows
to some 22,000, and each feed process its own venue's books.
Books are not stored, so the database grows only with the episodes,
trades, and orders.

## Results so far

From the run that started on September 22, 2026, read after 43 hours.

**Coverage.** 14,972 Kalshi and 23,044 Polymarket US contracts fetched,
10,488 classified into bets, 2,452 pairs found across 17 kinds. Of those
658 are game markets (winner, spread, total), 128 are season futures
(champion, conference, division, top seed), and 1,666 are player props,
touchdowns and first touchdown, passing, rushing and receiving yards,
receptions, and a few others.

**Recording.** The feeds delivered 6.8 million Kalshi and 2.0 million
Polymarket US book updates, of which 2.3 million changed the top of book
and were written, a median of 52,000 rows an hour and a peak of 95,000.
Kalshi dropped the connection 3 times and Polymarket US 38 times, but
with immediate reconnect the total time without a subscription was 3
seconds.

**Opportunities.** The scanner saw 4,902 episodes of positive net edge.
They are short: 4% ended within a second, 19% within ten seconds, 56%
within a minute, and 55 seconds is the average. A few things stand out.

- The largest dollar edges are in season futures, where a 2 to 3 cent edge
  on a few thousand contracts is worth $25 to $30, but those tie up the
  capital for four months and return 6 to 8% a year, under the 10% target.
- Player props show the biggest edges per contract, up to 43 cents on a
  passing yards line, but with a few dollars of depth behind them. Props
  are where Polymarket US and Kalshi disagree most, and they resolve the
  same night, so close to half of the prop episodes beat 10% a year.
- Spreads are the opposite: half a cent of edge, but on $1,000 of depth for
  minutes at a time, worth $4 to $6 a piece.
- Direction is not symmetric. In 2,650 episodes the cheap leg was yes on
  Kalshi with the other side bought on Polymarket US, in 1,943 the reverse,
  and in 303 both legs were plain yes contracts.

**Paper trades.** The executor took its first trades on the Thursday
night props for Atlanta at Green Bay: 7 trades, 4 filled in full, 3 in
which the Polymarket US leg was gone by the time the order arrived
(about 40 to 100 ms later) and the Kalshi leg was sold back at a loss of
a few cents. Net result after hedges was +$0.07 on a few dollars of
capital. The edges on the props are real but the depth behind them is one
to five contracts, so the money is small and every fill is a race.

**The first live game.** Atlanta at Green Bay on September 24 was the
first game recorded in play, on a fresh database with $10,000 per venue,
a 2-cent minimum edge and 500 contracts per trade. The recorder held
1,700 Kalshi updates a second without a drop and wrote 550,000 rows in
the peak hour. The scanner saw 5,587 positive-edge episodes on the game's
pairs during play, and almost all were noise: 4,060 had under a cent of
edge and 2,860 of those lasted under a second. The money sat in 88
episodes of 10 cents or more, nearly all the same event: a scoring play or
a decisive stat, one venue reprices, the other keeps its old price for a
fraction of a second. Drake London's touchdown, later overturned, showed
as a 78-cent edge for 0.2 seconds.

The paper executor sent 398 trades in three hours and then stopped
because both balances were at zero. 247 filled in full, 45 partly, 106
not at all. Kalshi filled 65% of what was asked and Polymarket US 41%,
with simulated latency never above 150 ms, so it is the book moving
within a tenth of a second, not slow orders. Trades signalled at 2 to 3
cents lost money after hedging; the 44 at 10 cents or more made most of
the profit. When every trade had settled the two venues held $20,653, a
gain of $653, or 3.3% on the capital, in three hours. About a third of
the gain was luck: 2,161 contracts were left holding one side only,
because the flatten attempt found nothing it could take, and the
resolutions happened to go the right way.

That game set the current limits. Thin edges are not worth the race, so
the minimum is five cents. At 500 contracts the game wanted $31,000
against $20,000 available and the last quarter hour went untraded, so
the cap was set to 50, which fits the nine games of a Sunday early
window; trades are now sized by the books and the cash alone. Two
execution flaws it exposed are fixed: orders now sweep the levels above
the edge floor instead of only the top level, and a level too small for a
whole contract no longer ends a ladder walk, which is what had turned
most of the one-sided positions into "no book to flatten".

**The first real orders.** On September 28, the first game traded with real
money, only 4 of the 72 orders sent to Polymarket US filled, while Kalshi's
filled 50 times. Polymarket US's books reached us about 85 ms after the
venue changed them at the median, 160 at the 90th percentile, and Kalshi's
in about 12, so after a score an edge often paired Kalshi's new price with
Polymarket US's old one, already gone. Since then the Polymarket US feed is
no longer batched, a Polymarket US leg waits for a current book, and Kalshi
is reached on the hosts it dedicates to API traders. A trade feed, tried
for a day, was dropped, since trades came no sooner than the book that
showed them.

**Lasting edges.** The race after a score could not be won from retail
access: of 89 live trades on September 29, 79 were on edges gone within
0.2 seconds of the signal, and one filled both legs. So from September 30
the bot looks for edges that last and pay enough for the time their money
is tied up, which faster traders tend to leave alone. Futures came back
into the catalog, every episode records how long its edge stayed at five
cents or more, and the executors stopped trading games in play. They take
only bets paying a day or more away that return enough a year, at first
100% and from that evening 30%, sell back a missed leg rather than buy the
other side, and size by the books and the cash alone. At 100% a year one
edge in the first 24 hours of live trading qualified, and it lasted an
instant.

**The first lasting trades.** At 30% a year live trading took its first
trades within seconds, on college football season win totals that had
held an edge for hours, several of them two Polymarket US markets on the
same line. In under three hours it had put about $100, most of its
Polymarket US cash, into 16 open trades expected to make about $9, 8% on
the capital or 46% a year, paying out from November 28. Those first hours
also found a bug. Polymarket US fills an order in pieces as small as a
hundredth of a contract, and each piece was cut to whole contracts on its
own, so 18 of the first 32 orders there, every one filled in full, were
stored as filled short or not at all, and trades were flattened against
legs they did not hold, one of them three times over. Fills are now added
up first, the stored answers were read again to repair the trades, and the
11 contracts they held on one side more than the other were sold back.
Since both venues trade in hundredths of a contract, fills are now counted
to the hundredth, so an order for 7 that fills 6.42 is recorded as 6.42
and its other leg's 0.58 over is sold back like any excess, and the live
executor checks the venues' positions against the trades every 5 minutes.
The first such sale, 0.42 of a contract on October 1, could not go through:
Polymarket US nets positions per market, so selling that No was buying
Yes, and the $0.16 free there could not pay for it. Three such refusals in
a row halted live trading, so an order turned away for lack of cash now
counts as unfunded rather than refused, and waits for the cash to grow.
Later that night live trading halted again, at 3 AM Eastern, when Kalshi
closed for its weekly maintenance while its feed went on: three trades
bought the Polymarket US leg, Kalshi refused the other, and two of those
legs were sold back at a cent each. Neither venue is traded in its
published maintenance window now, and a sale to flatten no longer gives
away a contract for under half of what it cost. On October 2 the return a
trade must make went up from 30% a year to 50%. That day showed that
Kalshi, too, keeps one position per market. Two trades had bought 5 Pitt
Yes there where another held 51 No, so the account held 46 No, and when
their Polymarket US legs missed, each sale of that Yes, sent reduce only,
was cancelled unfilled: 113,000 times in 16 hours, as the book kept
showing a bid. A Kalshi sale is no longer reduce only, and a sale that
fills nothing waits a minute before the next.

On October 4 a trade went from asking for half of what the books showed to
all of it. Of the 231 live trades to then that had a paper twin, 222 were
sized by the share rather than the cash, at a median of 2 contracts, while
every Kalshi order filled in full, Polymarket US orders of 10 or more
filled in full 45 times in 46, and Kalshi's public trades showed another
taker buying the same side at our price within 10 seconds of us on 4 of
140 orders. Selling back legs that missed had cost $5.59 against $144.98
locked in. The same day the catalog dropped games for futures only, and it
grew from five sports to 21 and the elections, the new ones traded on paper
only at first:
some 4,400 sports pairs and 887 election pairs, about 5,300 contracts a
venue to follow. Election races pay out on Kalshi when the winner is sworn
in, in January, and control of a house on February 1, so at 50% a year an
election edge must be some 11 to 14 cents. Two hundred of the new pairs,
every kind among them, were read against both venues' own wording, and
each named the same team, person, race, or line on both.

Asking for all of what the books showed made the two desks race for it.
Both acted on the same signal, and the live desk's real order took the
thinner leg's whole level, nearly always on Polymarket US, before the paper
desk's simulated order read the book, which in the first half hour was
45 to 383 ms after the signal. Paper twins of live trades that filled went
from failing 19 times in 107 on October 3 to 10 times in 13. So from then
the desks trade apart. That evening every sport's futures went live, and
the catalog took the games back, now of every sport, and Bitcoin, its 15
minute windows and its price by a date: paper trades the games, matches,
fights, races, and windows, in play too and however soon they pay, and
live the futures, Bitcoin's held out until they have shown what they do.

The same day showed why one live trade in six had gone out some 157 ms
after its signal while the rest went out within 20. Every second a trade
stayed exposed, the retry to flatten it ended with the loss check, whose
query joined the orders to their trades with no index on the trade, so
SQLite built one over every order each time: some 150 ms with the 116,000
orders the October 1 bug had left, during which the orders of a trade the
same tick had decided waited their turn. Deleting those orders brought
every live order since under 65 ms. The orders table now has that index,
a retry checks the losses only when it sold something, and an edge waiting
for a Polymarket US book is priced again the moment the wait ends rather
than at the next tick.

**Paper timed as live orders are.** Paper had drawn each order's arrival
around 50 ms on Kalshi and 60 on Polymarket US, guesses set above the round
trips, and filled it against our newest book then. But that book is late:
both venues stamp each order with their own clock, the one they stamp their
books with, and the 1,096 live orders from October 1 to 4 that carry it
show a Kalshi order reached the venue 12 ms after we sent it and answered 8
ms later, and a Polymarket US one 59 ms after, its median hour by hour from
34 to 79, and answered 31 later. A Polymarket US order met the venue's book
of some 60 ms after the signal, while paper read the venue's book of some
20 ms before it, our copy being some 85 ms behind, so paper missed the very
changes an edge goes in. From October 4 paper keeps every book of the
contracts its orders are in flight on, and an order meets the one the venue
had when it would have arrived, once a later one shows nothing is still on
its way. The same day the minimum edge went from five cents to two, the
return a year doing the weighing, and paper kept to the bets paying within
a day.

**Paper started again.** On October 4 at 16:42 UTC paper was reset to no
open trades and 10,000 dollars on each venue, so its results are all under
the new rules. Nothing was deleted: its 548 trades and 2 settlements until
then carry the mode `paper_reset_2026-10-04`, which neither the paper desk
nor `summary.py` reads, 447 of them still open, and a `transfer_in` on
each venue's ledger, 869.54 dollars on Kalshi and 1,744.29 on Polymarket
US, brought its cash back to 10,000. To undo it, set those rows' mode back
to `paper` and remove the two ledger rows.

**Live short of cash.** By October 5 at 23:20 UTC live had made 747
futures trades, locking in $377.71 on what matched and losing $16.58 to
selling back legs that filled alone, nothing paid out yet: $3,016 held
until November to June, returning 12.3% on it, some 60% a year. Its 218
trades on games under way, the in-play test's among them, had lost $36.31,
all settled, and from the five cent rules on a Polymarket US book change
none was made: of 10,276 episodes in play in seven hours only one peaked
on such a change with a contract at five cents, the rest mostly Bitcoin
windows in their last minute and minor tennis, on Kalshi's changes. The
day before had spent $772 and $753 was left, Polymarket US's $324 to go
first, its legs costing about two thirds of the money. So on October 6,
at the user's asking, the games in play were switched off, the return a
year went from 50% to 100%, keeping the money for the better trades (47%
of what was held was in trades returning under 50%, opened under the
older rules, and the 9% returning 100% or more made 22% of the profit),
a future's Polymarket US order goes first, a future's legs may again be
Polymarket US's two listings of a win total, and the scanner counts what
one order could have had at more than the peak's first moment, all above.
Later that day the games in play came back, at two cents once the edge
had lasted half a second, whichever book changed last, at most 10
contracts a trade, with no limit on trades. On the Sunday of October 4,
2,035 NFL episodes in play stayed at two cents or more for half a second,
62 of them with a whole contract fillable throughout.

**A tenth of a second.** In its first eleven hours, from 15:37 UTC on
October 6, the half second let through 6 in-play trades. Of 44,351
episodes in play, 3,525 reached two cents, 2,069 stayed there for half a
second, and only 12 of those had a whole contract left after it, the rest
fractions of one, 0.09 on average on the NHL's, which last because no one
takes them. Polymarket US filled 2 of the 6, 11 contracts for 0.44$,
and missed 4, two of them on a Moldova and Slovakia exact score whose
Polymarket US book had sent nothing for 16 seconds, likely frozen after a
goal. Kalshi's orders went only after a fill, so nothing was left on one
leg. Of the episodes with a whole contract at two cents, 660 ended within
a tenth of a second, 205 between a tenth and a half, and 32 lasted
longer. In the in-play test of October 4, trades sending Polymarket US's
order first matched nothing on edges that ended within a tenth of a
second (17 trades, -1.00$), and made 1.92$ on those that lasted a tenth
to a half (26) and 3.01$ on a half to five seconds (15), while those past
five seconds, mostly frozen books, matched 1 contract of 37. So on October
7, at the user's asking, the wait became a tenth of a second.

**One rule for futures and games.** Later that day, at the user's asking,
futures began waiting a tenth of a second too, at the edge returning 100%
a year where a game's is two cents, so the two differ only in that, the
10 contracts, and the payout times. And the wait for Polymarket US's book
to catch up stopped starting again on every Kalshi change: in the eleven
hours of the half second, the log's ten-minute reports counted 826 pairs
waiting on a book, as Kalshi's books in a game change many times a
second, while with Polymarket US's order first a quote it no longer shows
costs only an order that fills nothing. It now counts from the Kalshi
change as of when the edge reached live's least edge.

**National teams and esports.** On October 6 the catalog took national
teams' soccer, the men's and the women's, and esports, a sport each for
Counter-Strike 2, League of Legends, Valorant, Dota 2, Rainbow Six Siege,
and Overwatch. That day's markets made 487 national team pairs, 394 of the
men's and 93 of the women's, and 342 esports pairs, 239 of them CS2's, 43
League of Legends', 22 Rainbow Six's, 18 Dota 2's, 17 Valorant's, and 3
Overwatch's: 125 of the 139 matches Kalshi listed had a Polymarket US match
by the teams' names and the date. All of them trade on Kalshi's shard 0.
Live trades them as any sport, their matches once under way.

**Markets that stop.** On October 7, at 08:41 UTC, live bought one
Polymarket US contract of a CS2 match and Kalshi turned the other leg away,
`MARKET_NOT_ACTIVE`: Kalshi had paused the market, and its book, still up,
had shown a two and a half cent edge for 132 seconds. The contract was
sold back three seconds later for 0.05$. It was the fourth such trade,
after three on October 1 in Kalshi's Thursday maintenance, 0.32$ in all,
small only because each was one to three contracts. A paused market's
book stops moving with the game, so its edge lasts and passes every wait,
and three such refusals in a row would have halted live trading. So that
day, at the user's asking, each venue's own word on its markets came in:
Polymarket US's state on every book and Kalshi's lifecycle channel, a
market paused, closed, or decided being left out until it trades again;
Kalshi's exchange status, shard by shard, every 30 seconds; a future
past a member's close time left out; and a market that turns an order
away as not trading, which counts as `closed` rather than refused, left
alone for ten minutes or until its venue says it trades. With Polymarket
US's order first, only a market Kalshi has stopped while Polymarket US's
trades can cost anything, the spread on what is sold back.

**Fractions of a contract.** Both venues take orders and fill them in
hundredths of a contract, and live already traded that way when a leg
filled in part, but it opened whole contracts. On October 7 the episodes
that had offered less than one contract at live's least edge, once the
edge had lasted the tenth of a second live waits, were counted: on
futures, over 49 hours from October 5 15:47 UTC, 94 of them, 15 contracts
and 4.94$; on games, over 25 hours from October 6 15:37, 2,676, 242
contracts and 11.08$, a median of 0.06 of a contract at 3.5 cents. The
leftover fractions of whole-contract trades added 2.27$ and 0.23$. With a
tenth of a contract the least, futures kept 2.22$ a day on about 18
trades and games 4.72$ on about 520, so from that day live opens with as
little as a tenth on both, at the user's asking (`LIVE_MIN_CONTRACTS`).
In play live had filled 2 of its 47 trades in that window, so there the
fractions add orders more than profit until those fill. The same check
found Kalshi charging fees to the hundredth of a cent, not up to the cent
as `fees.py` had it: one contract at 19 cents paid 0.0108$, and a
hundredth of a contract 0.0002$, as every Kalshi order had since live
trading began on September 30. Polymarket US rounds to the cent, so a
small order's fee rounds to nothing: 49 of its 67 fractional fills paid
none. `fees.py` was fixed to match. Edges were always priced on the
unrounded fee, so only what an order or a sale was expected to cost
changed, by under a cent an order.

**Trading on plays.** A second idea was to buy what a play settles, a
player's second hit settling "over 1.5 hits", from any order still
selling it below the settled price. On October 7 a probe that only
listened read MLB's free Stats API feed once a second and kept the books
of the game's 299 Kalshi and 377 Polymarket US contracts. In the first
fifteen minutes of play in Cleveland at Chicago, plays settled 20
contracts. 2 were already above 0.97$ before the play; 12 had no cheap
offer left by the time the feed marked the play over, the makers having
pulled their orders at the hit itself, 30 seconds after offering it at
62 to 64 cents; 6 were still cheap then and gone 0.4 to 1.5 seconds
later, the largest 259 contracts at 38 cents on Polymarket US. Our read
showed each play 6.2 seconds after it ended, so none was cheap by then.
The feed showed a pitch 10 to 13 seconds after it ended at the median, and
8 seconds even past MLB's web cache with a query string it had not seen,
so the delay is mostly MLB's own. A feed would have to beat the venues'
own official data, Sportradar's and Genius's, by under half a second, so
the idea was dropped and the probe removed. Its code is in the history
from commit 147f2a3.

**Fifty contracts in play.** From 2026-10-06 18:51 UTC, when a trade on a
game under way could ask for 10 contracts, to 02:30 on October 8, live
took 637 such trades. Those asking under a contract, 554 of them, filled
429, Polymarket US filling 77% of what they asked; those asking 1 to 9.99
filled 2 of 37, and those at the 10, 4 of 46, Polymarket US filling 16%
and 11%. Larger edges filled less too: Polymarket US's leg filled at all
on 84% of edges under three cents and 29% of those of ten or more. A
large book on a game in play is mostly gone by the time an order lands,
80 ms, though the book was under a tenth of a second old when it went:
the 42 capped trades that failed had 132 contracts on offer at the median
by the scanner, and none filled. And when Polymarket US did fill a larger
one, Kalshi often had moved too, missing 7 of the 10 between 1 and 9.99,
whose Polymarket US legs were then sold back. The 4 capped trades that
filled, all a long shot bought on one venue and its near-certain other
side on the other, had 330 contracts on offer for the 40 taken, 10.54$
against the 1.33$ locked in, at the best moment of each episode, which
may not have been the order's. On the user's asking the cap became 50
from October 8. The cash on a trade's venues still sizes it, and Kalshi's
baseball, basketball, and tennis shard keeps a tenth of its cash, about
31$ that day, so a baseball trade buying its near-certain side on Kalshi
stops short of 50.

**Maintenance episodes.** From 07:00 UTC on October 8, in Kalshi's
Thursday maintenance, its books froze while a tennis match, Bartunkova
against Muchova, went on moving on Polymarket US, and the scanner kept
episodes on its match and first set winners at 26 to 51 cents on about
3,800 contracts: the in-play opportunities showed 70 episodes worth
7,630$, none of which any order could have reached. Live refused to trade
then, as it does in either venue's window; from that day the scanner
leaves a member out while its venue is in maintenance, and the summary
leaves out the episodes kept in the windows before.

**A future's edge, traded again.** Until October 8 an episode made one
trade a desk, however long its edge lasted past it. Of the 134 live
futures trades from October 5 15:47 UTC to October 8 whose episodes the
scanner had stored, most edges ended with the trade, the median lasting
no time past it, as live's order emptied the levels it priced; 50 lasted
a second or more past it, 24 a minute, 12 ten minutes, and 5 an hour. 14
filled trades' episodes had offered more at their best moment than the
trade took, 77 contracts and 21.37$ more, but most of those ended with
the trade, the rest held back by cash or by whole contracts, and only 4
lasted a minute or more past it, the largest the Jaguars reaching the
conference final, 7 contracts taken of 47.86 offered, its edge lasting
457 seconds past the trade, up to about 12$ more. An edge that went away
and came back was already a new episode: 21 pairs had made 74 trades so.
So from October 8, at the user's asking, a future's trade that matched
something and left nothing to flatten gives its episode back once done,
and the edge is traded again once it has lasted a tenth of a second from
then. A trade that matched nothing ends the episode, so a quote that is
gone costs one order, not one an update, and a game's episode still makes
one trade: its edges last a fraction of a second, and its larger orders
mostly failed.

**A hundred contracts in play.** From October 8 05:55 UTC to October 10
05:55, with the cap at 50, live made 1,182 trades on games under way,
locking in 31.80$, every balanced one settling to the cent at what it
locked in. Polymarket US filled 71% of what trades asking under a contract
asked, 35% between 1 and 9.99, 19% between 10 and 49.99, and 7% at the 50.
Its leg filled and Kalshi's did not on 53 trades, sold back for 19.18$
less, 16.38$ of it on the 159 asking 10 or more, which locked in 19.06$;
those under 10 locked in 12.76$ for 2.80$. Of the 88 at the cap, 4 filled
in full for 14.58$, 7.48$ of it one Counter-Strike match at over ten
cents, and 2 filled only on Polymarket US, for 6.48$. Seven trades kept
contracts nothing would buy back at half their cost, six long shots bought
at one to five cents, 2.08$ lost, and a Dota 2 map's 25 of 50, which won
10.57$. On the user's asking the cap became 100 from October 10, a trade
sized by its cash leaving room for the fees from then too.

**Twenty contracts in play.** From October 6 15:34 UTC to October 10
17:55, at caps of 10, 50, and 100, live made 2,343 trades on games under
way for 26.67$. A cap of 10 throughout would have made some 22$, taking
each trade's first 10 contracts Polymarket US filled and Kalshi's fill up
to them, the locked-in profit and the hedges scaling with the contracts:
the 25 larger trades that matched locked in 34.28$ where 10 contracts
would have locked in some 10.5$, and the 14 Kalshi missed lost 23.90$
where 10 would have lost some 4.9$. The cap seldom held a trade back.
Of the 162 trades asking the cap, 10 matched it in full, and Polymarket
US filled nothing on 101 of the 115 asking 50, though the scanner showed
more than 50 contracts at two cents for nearly all of them. The two
largest results were one trade each: a Counter-Strike match matching 50
at 15 cents for 7.48$, and a Dota 2 map at 100 that Polymarket US filled
and Kalshi did not, sold back for 7.82$ less. On the user's asking the
cap became 20 from that evening.

The honest reading is that after fees the two venues are tightly priced
before kickoff and briefly, sharply mispriced after every scoring play.
The paper edge is real. Whether it is reachable is a question of whether
the stale quote is still there when a real order lands, which the paper
model assumes half the time and which only real orders can measure.

## Running it

Python 3.12 or newer. Install the three dependencies and run the tests:

```bash
pip3 install -r requirements.txt
python3 -m pytest tests -q
```

Build the catalog and run the live process locally, from `src/`:

```bash
cd src
python3 -m catalog.pipeline --sport nfl
python3 -m engine.run --sport nfl
```

`--sport ncaaf`, `--sport epl`, `--sport politics`, and the rest build or run one sport's markets, `--sport nfl,ncaaf,mlb` several
from one pool of money, and `--sport all` every one, as the instance does
with `--sport all --execute live --live-in-play --not-live none`.
`--no-trade` scans without trading, `--no-scan` only records, and
`--seconds 120` runs a short test. `--execute live` trades the futures with
real money and `--execute both` runs both desks, paper trading the bets on
one event. `--not-live politics` holds sports' futures out of live
trading, none by default, as with `--not-live none`.
`--live-in-play`, with `--execute live` or `both`, has live trade the games
under way too. The settings a run is tuned by, such as the minimum edge,
the annual return, and the starting balance, are in
`src/engine/helper/config.py`. Those only paper trading reads start with
`PAPER_`, those only live trading reads with `LIVE_`, and the rest hold for
both. `--set NAME=VALUE` overrides one for a run, for example
`python3 -m engine.run --sport nfl --set min_edge=0.03`. The run logs every
setting when it starts. The streams need venue keys in `data/`:
`kalshi_key_id.txt` and `kalshi_private_key.pem` for Kalshi,
`polymarket_us_key_id.txt` and `polymarket_us_secret_key.txt` for
Polymarket US. Live trading uses the same keys, which need trading
permission, and emails its alerts through `data/email.json`:

```json
{"host": "smtp.gmail.com", "port": 587, "user": "me@gmail.com",
 "password": "an app password", "from": "me@gmail.com", "to": ["me@gmail.com"]}
```

Before a live run, check the balances, the Kalshi key's attestation, and
the email, and split the Kalshi cash across its shards, from `src/`:

```bash
python3 -m tools.live_check --email
python3 -m tools.kalshi_shards --apply
```

Then read the reports:

```bash
python3 src/tools/summary.py --hours 24
python3 src/tools/summary.py --mode paper --sport epl,ucl
python3 src/tools/summary.py --mode live --market in-play
python3 src/tools/latency_report.py --hours 4
python3 src/tools/in_play_test.py
```

To compare ways of following the books during a game, from `src/`:

```bash
python3 -m tools.feed_check --seconds 300 --markets 100
```

## Layout

```
src/
  api/        venue clients and the shared websocket book stream
  catalog/    fetch, classify (one parser per venue), match, notes, pipeline
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

Where to look to change something:

| to change | look in |
|---|---|
| what a venue's API returns | `api/kalshi.py`, `api/polymarket_us.py` |
| how a websocket connects and reconnects | `api/bookstream.py` |
| which markets are understood | `catalog/classify/` |
| how bets are paired | `catalog/match.py` |
| where the venues' rules differ | `catalog/notes.py` |
| fees | `engine/helper/fees.py` |
| how an edge is priced | `engine/helper/pricing.py` |
| game timing | `engine/helper/game.py` |
| when each venue is in maintenance | `common/venues.py` |
| which markets and exchanges are not trading | `engine/components/market/record.py`, `engine/components/market/exchange.py` |
| which sports are treated alike: team sports, soccer, national teams, matches, races, esports | `common/sports.py` |
| every tunable number | `engine/helper/config.py` |
| when a trade is taken and sized | `engine/components/trading/executor.py` |
| the Kalshi cash on each shard | `tools/kalshi_shards.py` |
| how an order fills | `trading/paper.py`, `trading/live.py` |
| when live trading halts | `engine/components/trading/brakes.py` |
| the email alerts | `engine/components/trading/notify.py` |
| how far behind the feeds run | `tools/latency_report.py`, `tools/feed_check.py` |
| how the processes are wired | `engine/run.py` |
| the report on the database | `tools/summary.py` |
| live trading games in play, Polymarket US first | `trading/executor.py` (`fill_legs`), `trading/live.py`, `trading/footprints.py` |
| the in-play test of 2026-10-04 to 10-05 | `tools/in_play_test.py` |
| live records against the venues' positions | `tools/repair_fills.py` |
