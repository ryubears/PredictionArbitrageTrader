"""
Tell a human by email.

Live trading asks for a person in three cases: the cash on a live venue,
or on one of Kalshi's exchange shards, has run low and money should be
added or moved by hand, live trading has halted, and the Kalshi key's
location attestation is about to lapse, which AttestationWatch checks.
Every alert is logged and stored in the alerts table as it is raised, and
emailed in a background thread, so the loop never waits on the mail
server. Whether the email went out, or why not, is written back to
the alert.

The mail server and addresses are in data/email.json, gitignored like the
venue keys and read at each alert, so they can be added without a restart:

    {"host": "smtp.gmail.com", "port": 587, "user": "me@gmail.com",
     "password": "an app password", "from": "me@gmail.com", "to": ["me@gmail.com"]}

Port 465 connects over TLS from the start, any other port upgrades with
STARTTLS. Without the file the alert is still logged and stored.
"""

import asyncio
import json
import smtplib
import ssl
from email.message import EmailMessage
from api import kalshi
from common.log import on_failure
from common.paths import DATA_DIR, NAME
from common.periodic import Periodic
from common.timeutil import epoch, now_iso, utc_minute
from db import database
from db.models import Alert
from engine.helper import config

EMAIL_FILE = DATA_DIR / "email.json"


def send_email(settings, subject, body):
    """
    Send one plain text email through the SMTP server the settings name.
    """
    message = EmailMessage()
    message["Subject"], message["From"], message["To"] = subject, settings["from"], ", ".join(settings["to"])
    message.set_content(body)
    port = int(settings.get("port", 587))
    context = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(settings["host"], port, timeout=30, context=context)
    else:
        server = smtplib.SMTP(settings["host"], port, timeout=30)
        server.starttls(context=context)
    with server:
        if settings.get("user"):
            server.login(settings["user"], settings["password"])
        server.send_message(message)


class Notifier:
    """
    Stores, logs, and emails alerts. sender is the function that sends one email, given the settings, subject, and body.
    """

    def __init__(self, conn, log=print, sender=send_email):
        self.conn = conn
        self.log = log
        self.sender = sender
        self.tasks = set()          # Emails being sent.

    def deliver(self, subject, body):
        """
        Email one alert. Returns None when it went out, or why it did not.
        """
        try:
            settings = json.loads(EMAIL_FILE.read_text())
        except FileNotFoundError:
            return f"no email settings in {EMAIL_FILE.name}"
        except (OSError, ValueError) as e:
            return f"email settings in {EMAIL_FILE.name} could not be read ({e!r})"
        try:
            self.sender(settings, subject, body)
        except Exception as e:
            return repr(e)
        return None

    def finish(self, alert, error):
        """
        Write back whether the alert's email went out, and log when it did not.
        """
        alert.sent_at, alert.error = (None, error) if error else (now_iso(), None)
        database.update_alert(self.conn, alert)
        if error:
            self.log(f"alert {alert.id} was not emailed: {error}")

    def send(self, kind, subject, body, now=None):
        """
        Raise an alert at now, the current time unless given. Inside the
        running loop the email goes out in a background thread, outside it at once.
        """
        alert = Alert(ts=now or now_iso(), kind=kind, subject=subject, body=body)
        database.insert_alert(self.conn, alert)
        self.log(f"alert: {subject}")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self.finish(alert, self.deliver(subject, body))
            return alert

        def sent(task):
            if not task.cancelled() and task.exception() is None:
                self.finish(alert, task.result())

        task = asyncio.create_task(asyncio.to_thread(self.deliver, subject, body))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(sent)
        task.add_done_callback(on_failure(self.log, "alert email"))
        return alert


class AttestationWatch:
    """
    Emails a human through the notifier when the Kalshi key's location
    attestation lapses within config.KEY_WARN_HOURS, and again once it has
    lapsed. Past it, Kalshi refuses the key for sports markets until the
    attestation is renewed on Kalshi, which only a person can do. It is read
    every config.KEY_CHECK_HOURS, whether or not live trading is on, since
    the feed uses the same key. Each email goes out once for its date, even
    across restarts, since the alerts table remembers it. read returns the
    time it lapses, in seconds since 1970, or None.
    """

    def __init__(self, conn, notifier, log=print, read=None):
        self.conn = conn
        self.notifier = notifier
        self.read = read or kalshi.attestation_lapses
        self.readings = Periodic(lambda: config.KEY_CHECK_HOURS * 3600, log, "kalshi key attestation reading")

    def alert(self, lapses, now):
        """
        The alert for an attestation that lapses at lapses as of now, as (subject, body), or None when it is not due yet.
        """
        when = utc_minute(lapses)
        left = lapses - epoch(now)
        if left <= 0:
            return (f"{NAME}: the Kalshi key's location attestation lapsed at {when}",
                    f"Kalshi's location attestation for the account's API keys lapsed at {when}. Until it is renewed on "
                    f"Kalshi, Kalshi refuses the key for sports markets, so live orders there fail.")
        if left <= config.KEY_WARN_HOURS * 3600:
            return (f"{NAME}: renew the Kalshi key's location attestation before {when}",
                    f"Kalshi's location attestation for the account's API keys lapses at {when}, in {left / 3600:.0f} hours. "
                    f"Renew it on Kalshi before then. Past it, Kalshi refuses the key for sports markets, so live orders there fail.")
        return None

    async def check(self, now):
        """
        Read when the attestation lapses and raise the alert that is due, unless it has been raised before.
        """
        lapses = await asyncio.to_thread(self.read)
        due = self.alert(lapses, now) if lapses else None
        if due and not database.alert_raised(self.conn, "attestation", due[0]):
            self.notifier.send("attestation", *due, now)

    def tick(self, now, clock):
        """
        Once a second from the session, with the wall clock in seconds. Starts a reading when one is due and none is running.
        """
        self.readings.tick(clock, lambda: self.check(now))
