"""
When live trading halts.

Real money calls for brakes, sized for a test with about 100 dollars on
each venue. Each rule reads the orders and trades tables, so a restart does
not reset what it has seen, and each starts over after a halt, so what led
to one does not halt live trading again once a human has cleared it.

A halt stops new trades until a human has checked the venues, removed
HALT_FILE, and restarted the process, and what is held is still settled.
Whether flattening goes on depends on the rule. The rules on orders say
the orders themselves are failing, so they stop every order, flattening
included, and do so even after the rule on losses has halted. After a halt
for a loss, flattening goes on, so what is exposed can still be closed. A
restart while HALT_FILE is there keeps the halt as it was,
flattening or not, since exposed trades are taken back at every start.

Live trading halts, with every order stopped, when:

- config.LIVE_UNKNOWN_LIMIT of the last config.LIVE_ORDER_WINDOW orders had
  an unknown outcome: no answer, a venue failing on its side, or an answer
  that cannot be read. That is three in a row, or a steady error rate. One
  such order only sets its own trade aside, see live.py, and the halt names
  every one in the window, since none of them is emailed on its own.
- One venue refused its last config.LIVE_REJECT_LIMIT orders. A refusal is
  the venue answering that it will not take an order, so nothing traded: an
  error status in the 400s, such as not authorized, not enough money on
  Kalshi, a bad price, or too many requests, or Polymarket US rejecting the
  order. An order it took that found nothing at its price is unfilled, not
  refused, and so is one that Polymarket US turned away for having no
  liquidity or for being slow. One it turned away for lack of cash is
  unfunded, not refused either: the venue works, and the low cash email
  tells a human, see live.py. Nor is one it turned away because the market
  was not trading, paused, closed, or decided, or the exchange had
  stopped, which is closed: the venue works, and the market is left alone
  until it trades again, see live.py.

Live trading halts, with flattening going on, when the live trades
decided in the last config.LIVE_RESULT_HOURS lost more than
config.LIVE_MAX_LOSS_SHARE of the live money, net.

A trade is decided once its result no longer depends on the game: when
both legs hold the same number of contracts, which pay a dollar each
whichever way the game goes, or once it settles. Its result is what it is
paid, plus what it sold, less what it bought, fees included, from the
orders table. A trade with an order of unknown outcome is left out, since
its records may be wrong.

The live money a loss is measured against is the cash on both venues
plus what open live trades hold, at what it cost, less a dollar for each
pair the venues have already paid back. Both venues net a contract's yes
and no into one position and pay a dollar a pair at once, so when a
later trade holds the other outcome of a contract an earlier open one
holds, on the same venue, the smaller of the two is back in cash though
both trades still count what it cost until they settle, see
database.netted(). A dollar a pair, not what the pairs cost, keeps the
live money what it would be had the venues not netted, so what a trade
locked in counts in its result alone.
"""

from common.paths import DATA_DIR, NAME
from common.timeutil import now_iso, shift
from db import database
from engine.helper import config

HALT_FILE = DATA_DIR / "live_halt.txt"      # Why live trading halted, kept until a human removes it.
STOPPED = "every order stopped"             # How a line of HALT_FILE says flattening stopped too.


class Brakes:
    """
    Decides when live trading halts, from the orders and trades stored.
    notifier is the Notifier from notify.py that tells a human when live trading halts.
    """

    def __init__(self, conn, cash, log=print, notifier=None, clock=now_iso):
        self.conn = conn
        self.cash = cash
        self.log = log
        self.notifier = notifier
        self.clock = clock
        self.halted = None                                      # Why live trading halted, once it has. No new trades go out.
        self.stopped = None                                     # Why every live order stopped, flattening included, once one has.
        self.since = database.last_alert_ts(conn, "halt")       # When live trading last halted. The rules count only what came after.
        if HALT_FILE.exists():
            lines = HALT_FILE.read_text().strip().splitlines()
            self.halted = f"halted before this start, remove {HALT_FILE} to resume: {'; '.join(lines)}"
            self.stopped = self.halted if any(f" UTC {STOPPED}: " in line for line in lines) else None
            self.log(f"live trading {self.halted}")

    # HALTING

    def halt(self, reason, flatten=True):
        """
        Stop new trades, and when flatten is False every order, flattening
        included. Add the reason to HALT_FILE, log it, and tell a human. Once
        halted, only the first reason that stops flattening still counts.
        """
        if self.stopped or (self.halted and flatten):
            return
        now = self.clock()
        title = "live flattening stopped" if self.halted else "live trading halted"
        self.halted = self.halted or reason
        self.stopped = None if flatten else reason
        self.since = now
        HALT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with HALT_FILE.open("a") as f:
            f.write(f"{now[:19]} UTC {STOPPED if self.stopped else 'new trades halted'}: {reason}\n")
        self.log(f"{title}: {reason}")
        if self.notifier:
            going_on = "No more orders are sent, flattening included." if self.stopped else "Exposed trades are still flattened."
            self.notifier.send("halt", f"{NAME} {title}",
                               f"At {now[:19]} UTC: {reason}\n\nLive trading opens no new trades. {going_on} "
                               f"What is held is still settled. Balances: {self.cash.summary()}.\n\n"
                               f"Once the venues are checked, remove {HALT_FILE} and restart the process to resume.", now)

    # ORDERS

    def watch(self, order):
        """
        After each answered order, halt on too many unknown outcomes, or on a venue refusing too many orders in a row.
        """
        if order.status == "error":
            recent = database.recent_orders(self.conn, config.LIVE_ORDER_WINDOW, since=self.since)
            unknown = sorted(order_id for order_id, status in recent if status == "error")
            if len(unknown) >= config.LIVE_UNKNOWN_LIMIT:
                self.halt(f"{len(unknown)} of the last {len(recent)} orders had an unknown outcome, orders "
                          f"{', '.join(map(str, unknown))}, the last on {order.venue}: {order.note}", flatten=False)
        elif order.status == "rejected":
            recent = database.recent_orders(self.conn, config.LIVE_REJECT_LIMIT, venue=order.venue, since=self.since)
            if len(recent) >= config.LIVE_REJECT_LIMIT and all(status == "rejected" for _, status in recent):
                self.halt(f"{order.venue} refused {len(recent)} orders in a row, the last with: {order.note}", flatten=False)

    # MONEY

    def capital(self):
        """
        The live money in all: the cash on both venues, what is held back for orders in flight included, and what open live
        trades hold, at cost, less a dollar for each pair the venues have already paid back by netting, which the cash
        holds already, see database.netted().
        """
        held = sum(database.load_held(self.conn, self.cash.mode).values())
        paid = sum(database.load_netted(self.conn, self.cash.mode).values())
        return self.cash.total() + held - paid

    # RESULTS

    def results(self, since):
        """
        The results in dollars of the live trades decided at or after since.
        """
        out = []
        for t in database.load_trade_cash(self.conn, self.cash.mode, since):
            if t["unknown"]:
                continue            # Its records may be wrong until a human has looked.
            if t["settled_at"]:
                result, decided_at = t["payouts"] + t["sold"] - t["bought"], t["settled_at"]
            elif t["yes_held"] == t["no_held"]:
                result, decided_at = t["yes_held"] + t["sold"] - t["bought"], t["last_answer"]
            else:
                continue            # Still exposed, so its result waits on the game.
            if decided_at >= since:
                out.append(result)
        return out

    def check_results(self):
        """
        After a trade may have been decided, halt on too big a loss over the window.
        """
        if self.halted:
            return
        since = max(shift(self.clock(), hours=-config.LIVE_RESULT_HOURS), self.since or "")
        net = sum(self.results(since))
        before = self.capital() - net           # The live money before these results.
        if net < 0 and before > 0 and -net / before > config.LIVE_MAX_LOSS_SHARE:
            self.halt(f"the live trades decided in the last {config.LIVE_RESULT_HOURS} hours lost {-net:,.2f}$, "
                      f"{-net / before:.0%} of the live money, over the {config.LIVE_MAX_LOSS_SHARE:.0%} limit")
