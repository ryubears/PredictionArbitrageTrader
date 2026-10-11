-- The current schema of every table, in pipeline order. database.connect() runs it on every
-- connection, so a new table or index appears on its own. Changes to a table that already exists
-- go in migrations.py as a new step.

CREATE TABLE IF NOT EXISTS contracts (
    venue        TEXT NOT NULL,   -- 'kalshi' or 'polymarket_us'.
    contract_id  TEXT NOT NULL,   -- Kalshi market ticker, or Polymarket US market slug.
    market_id    TEXT NOT NULL,   -- Kalshi market ticker, or Polymarket US market id.
    event_id     TEXT NOT NULL,   -- Kalshi event ticker, or Polymarket US event slug.
    series_id    TEXT,            -- Kalshi series ticker, or Polymarket US series slug.
    sport        TEXT NOT NULL,   -- Our own sport key, for example 'nfl'.
    event_title  TEXT,
    title        TEXT NOT NULL,   -- The market question or title.
    outcome      TEXT NOT NULL,   -- 'Yes', a team name, 'Over', and so on.
    market_type  TEXT,            -- The venue's own market category, for example 'moneyline'.
    line         REAL,            -- Spread or total line when the venue gives one.
    rules        TEXT,
    start_time   TEXT,            -- Game start in ISO 8601 UTC, when known.
    close_time   TEXT,            -- When trading stops, or settles if sooner, ISO 8601 UTC.
    fee_info     TEXT,            -- JSON describing the venue's fee model for this contract.
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    PRIMARY KEY (venue, contract_id)
);

CREATE INDEX IF NOT EXISTS idx_contracts_sport ON contracts (sport, venue);

CREATE TABLE IF NOT EXISTS bets (
    venue        TEXT NOT NULL,
    contract_id  TEXT NOT NULL,
    kind         TEXT NOT NULL,   -- 'champion', 'game_winner', 'spread', 'total', and so on.
    season       INTEGER,         -- The year the season ends.
    game_date    TEXT,            -- YYYY-MM-DD in US Eastern time, for game kinds only.
    team_a       TEXT,            -- Away team code for game kinds.
    team_b       TEXT,            -- Home team code for game kinds.
    subject      TEXT,            -- The team the contract is about, when there is one.
    line         REAL,            -- Spread margin, total points, or wins threshold.
    polarity     TEXT NOT NULL,   -- 'yes' or 'no', see models.Bet.
    pair_id      INTEGER,         -- The pair this bet belongs to, set by match.py. Null while only one venue lists the bet.
    PRIMARY KEY (venue, contract_id)
);

-- A pair keeps its id across matches, found by its label, and its row stays once no bet points at it any more,
-- so the opportunities and trades that refer to it always resolve. A pair is current when a bet points at it.
CREATE TABLE IF NOT EXISTS pairs (
    id           INTEGER PRIMARY KEY,
    sport        TEXT NOT NULL DEFAULT 'nfl',   -- Our sport key. Pairs from before a second sport are 'nfl'.
    label        TEXT NOT NULL UNIQUE,   -- The sport and the bet's identity in words, for example 'nfl spread 2026-09-20 CAR@ATL ATL 4.5'.
    kind         TEXT NOT NULL,
    season       INTEGER,
    game_date    TEXT,
    team_a       TEXT,
    team_b       TEXT,
    subject      TEXT,
    line         REAL,
    venues       TEXT NOT NULL,      -- Comma separated venues with a member contract.
    contracts    INTEGER NOT NULL,   -- Member contracts.
    flags        TEXT NOT NULL,      -- JSON list of things to check before trusting the pair.
    matched_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gaps (
    venue        TEXT NOT NULL,
    start_ts     TEXT NOT NULL,   -- When the connection was lost, ISO 8601 UTC.
    end_ts       TEXT,            -- When a new connection was subscribed. Null if the recorder stopped first.
    PRIMARY KEY (venue, start_ts)
);

CREATE TABLE IF NOT EXISTS opportunities (
    id             INTEGER PRIMARY KEY,
    pair_id        INTEGER NOT NULL,   -- The pair, see pairs.
    trade          TEXT NOT NULL,   -- The two legs in words.
    yes_venue      TEXT NOT NULL,   -- Where the yes exposure was cheapest at the peak.
    yes_contract   TEXT NOT NULL,
    no_venue       TEXT NOT NULL,   -- Where the no exposure was cheapest at the peak.
    no_contract    TEXT NOT NULL,
    start_ts       TEXT NOT NULL,   -- When the net edge first went positive.
    end_ts         TEXT NOT NULL,   -- When it went back to zero, or the last quote seen.
    seconds        REAL NOT NULL,
    peak_ts        TEXT NOT NULL,
    peak_edge      REAL NOT NULL,   -- Net dollars per contract at the top of book, at the peak.
    peak_size      REAL NOT NULL,   -- Contracts fillable at a positive net edge, at the peak.
    peak_profit    REAL NOT NULL,   -- Net dollars from filling peak_size, at the peak.
    live           INTEGER NOT NULL,   -- 1 when the game had started, 0 otherwise.
    days_held      REAL,            -- From the peak until the bet pays out, assuming it is held to resolution.
    return_pct     REAL NOT NULL,   -- Net edge over the capital tied up, as a percent.
    annual_pct     REAL,            -- return_pct scaled to a year over days_held, without compounding.
    min_edge_seconds REAL,          -- The longest unbroken stretch of the episode at config.MIN_EDGE or more. Null before 2026-09-30.
    min_edge_size  REAL,            -- Contracts fillable at that edge through all of that stretch.
    min_edge_profit REAL,           -- Net dollars from filling them, at the stretch's thinnest moment.
    take_size      REAL,            -- Contracts one order could have had on the levels at live's least edge or more, at the best moment
                                    -- the scanner weighed, see Scanner.weigh_take(); at the peak alone to 2026-10-06. Null before 2026-10-05.
    take_profit    REAL,            -- Net dollars from filling them.
    pm_changed     INTEGER          -- 1 when that moment came with a change of the Polymarket US leg's book, 0 otherwise. Null before 2026-10-05.
);

-- Reports read the episodes of a recent window, see tools/summary.py, and this spares them reading every one.
CREATE INDEX IF NOT EXISTS idx_opportunities_start ON opportunities (start_ts);

CREATE TABLE IF NOT EXISTS trades (
    id             INTEGER PRIMARY KEY,
    mode           TEXT NOT NULL DEFAULT 'paper',   -- 'paper' or 'live', the executor that made the trade. Rows from before live trading are paper.
    pair_id        INTEGER NOT NULL,   -- The pair, see pairs.
    trade          TEXT NOT NULL,   -- The two legs in words.
    signal_ts      TEXT NOT NULL,
    edge           REAL NOT NULL,   -- Net dollars per contract at the signal.
    quantity       INTEGER NOT NULL,   -- Contracts wanted on each leg: whole until 2026-10-07, to the hundredth live since. SQLite keeps
                                       -- a fraction in an INTEGER column as it is, as orders.quantity always has.
    yes_venue      TEXT NOT NULL,
    yes_contract   TEXT NOT NULL,
    yes_polarity   TEXT NOT NULL,   -- The side the contract pays on, so settlement knows whether the leg won.
    yes_limit      REAL NOT NULL,
    yes_filled     INTEGER NOT NULL,   -- Contracts filled, to the hundredth the venues fill in; SQLite keeps a fraction as a REAL. So are held and matched.
    yes_cost       REAL NOT NULL,   -- Dollars paid including fees.
    yes_latency_ms INTEGER NOT NULL,
    yes_fill_ts    TEXT,
    no_venue       TEXT NOT NULL,
    no_contract    TEXT NOT NULL,
    no_polarity    TEXT NOT NULL,
    no_limit       REAL NOT NULL,
    no_filled      INTEGER NOT NULL,
    no_cost        REAL NOT NULL,
    no_latency_ms  INTEGER NOT NULL,
    no_fill_ts     TEXT,
    yes_held       INTEGER NOT NULL,   -- Contracts still held on the yes leg after any flattening.
    no_held        INTEGER NOT NULL,
    matched        INTEGER NOT NULL,   -- Contracts held on both sides after any flattening.
    profit         REAL NOT NULL,   -- Dollars locked in on the matched contracts, after fees.
    hedge          TEXT NOT NULL,   -- How a mismatch was flattened, in words.
    hedge_pnl      REAL NOT NULL,   -- Dollars gained or lost by flattening, after fees.
    status         TEXT NOT NULL,   -- 'sent' while in flight, then 'filled', 'partial', or 'failed'.
    pays_at        TEXT NOT NULL,   -- When the slower leg pays out.
    in_play        INTEGER          -- 1 for a trade on a game under way at the signal, else 0. Null before 2026-10-05.
);

-- The same for the trades of a recent window.
CREATE INDEX IF NOT EXISTS idx_trades_signal ON trades (signal_ts);

-- How each trade's legs paid out, one row per trade once both of its held legs have resolved, by settle.py.
-- A trade with no row here is still open. A leg that held nothing has no result.
CREATE TABLE IF NOT EXISTS settlements (
    trade_id       INTEGER PRIMARY KEY,   -- The trade, see trades.
    mode           TEXT NOT NULL DEFAULT 'paper',   -- 'paper' or 'live', the trade's mode.
    settled_at     TEXT NOT NULL,         -- When both legs had resolved and the payouts were booked: the later leg's settlement.
    yes_result     TEXT,                  -- How the yes leg's contract resolved, 'yes' or 'no', or the decimal a yes contract paid
                                          -- when it paid part of a dollar, '0.6' for 60 cents, as for a tie or a drawn match.
    yes_payout     REAL,                  -- Dollars received on the yes leg: a contract held pays what a yes contract paid when the
                                          -- leg holds yes and the rest of the dollar when it holds no, so one or nothing as a rule.
    yes_settled_at TEXT,                  -- The venue's settlement time for the yes leg.
    no_result      TEXT,
    no_payout      REAL,
    no_settled_at  TEXT
);

-- Every real order the live executor sent, stored before it is sent and updated with the venue's answer.
CREATE TABLE IF NOT EXISTS orders (
    id             INTEGER PRIMARY KEY,
    trade_id       INTEGER NOT NULL,   -- The live trade the order was sent for, see trades.
    venue          TEXT NOT NULL,
    contract_id    TEXT NOT NULL,
    purpose        TEXT NOT NULL,      -- 'open' for one of the trade's two legs, 'flatten' for an order that evens them.
    action         TEXT NOT NULL,      -- 'buy' or 'sell'.
    outcome        TEXT NOT NULL,      -- 'yes' for the contract itself, 'no' for its other side.
    quantity       INTEGER NOT NULL,   -- Contracts asked for, to the hundredth.
    limit_price    REAL NOT NULL,      -- The worst price per contract accepted for the outcome, before fees.
    client_id      TEXT NOT NULL,      -- Our id for the order, sent with it, so an order whose answer was lost can be found at the venue.
    sent_at        TEXT NOT NULL,
    status         TEXT NOT NULL,      -- 'sent' until the venue answers, then 'filled', 'partial', 'unfilled', 'unfunded', 'closed', 'rejected', or 'error'.
    venue_order_id TEXT,               -- The venue's id for the order, once it answered.
    answered_at    TEXT,
    latency_ms     INTEGER,            -- From sending the order to its answer.
    filled         INTEGER NOT NULL,   -- Contracts bought or sold, to the hundredth.
    dollars        REAL NOT NULL,      -- Paid for a buy or received for a sale, fees included.
    fees           REAL NOT NULL,
    note           TEXT,               -- Why the venue rejected the order, or the error.
    response       TEXT,               -- The venue's answer as JSON, for reconciling.
    book_at        TEXT,               -- The venue's time for the contract's newest book when the order went out, null when it gave
                                       -- none, as for the first book on a connection: when the venue last changed the market.
    book_ts        TEXT                -- When that book reached us, by our clock. sent_at less it is how long the market had sent nothing.
);
-- The brakes and the reports join orders to their trades. Without this SQLite built a throwaway index over every order
-- on each join: on 2026-10-03, with 116,000 orders, the brakes' check took some 150 ms on the instance and held up the
-- orders queued behind it.
CREATE INDEX IF NOT EXISTS idx_orders_trade ON orders (trade_id);

-- The in-play test of 2026-10-04 17:49 to 2026-10-05 01:27 UTC: each of the 200 live trades it took while their games were
-- under way, at no more than 5 contracts, and the paper trade on the same signal, of the same size and limits, it is set
-- against. tools/in_play_test.py reports on it. Nothing writes to it any more.
CREATE TABLE IF NOT EXISTS twins (
    live_trade_id   INTEGER PRIMARY KEY,    -- The live trade, see trades.
    paper_trade_id  INTEGER,                -- Its paper twin, or null when the paper money could not pay for one.
    sequence        TEXT                    -- With a leg on each venue, 'together' when both orders went out at once, or
                                            -- 'polymarket_first' when Kalshi's waited for Polymarket US's answer. Null otherwise.
);

-- The paper balances. Live money is read from the venues and has no ledger.
CREATE TABLE IF NOT EXISTS ledger (
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL,
    venue        TEXT NOT NULL,
    amount       REAL NOT NULL,     -- Dollars in or out of the venue balance, positive when money arrives.
    reason       TEXT NOT NULL,     -- 'buy', 'sell', 'payout', or 'transfer_in', each venue's first entry, of its starting balance.
    trade_id     INTEGER,           -- The trade behind a buy, sell, or payout.
    balance      REAL NOT NULL      -- The venue's balance after this entry, so the newest entry gives the balance.
);

-- Everything the live process told a human, by notify.py, whether or not the email went out.
CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL,
    kind         TEXT NOT NULL,     -- 'halt' when live trading stopped, 'low_cash' when a live venue's cash fell under config.LIVE_LOW_CASH, or 'attestation' when the Kalshi key's location attestation is about to lapse or has.
    subject      TEXT NOT NULL,
    body         TEXT NOT NULL,
    sent_at      TEXT,              -- When the email went out, null until it has.
    error        TEXT               -- Why it could not be sent, for example no email settings in data/.
);
