"""
Tests for the email alerts.
"""

import asyncio
import pytest
from common.timeutil import epoch
from db import database
from engine.components.trading import notify


def test_an_alert_without_email_settings_is_still_logged_and_stored(tmp_path, monkeypatch):
    conn = database.connect(tmp_path / "t.sqlite")
    monkeypatch.setattr(notify, "EMAIL_FILE", tmp_path / "missing.json")
    logs = []
    notifier = notify.Notifier(conn, logs.append, sender=lambda *args: pytest.fail("nothing to send with"))

    async def scenario():
        notifier.send("halt", "PredictionArbitrageTrader live trading halted", "why")      # From inside the loop the email goes out in the background.
        await asyncio.gather(*notifier.tasks)
    asyncio.run(scenario())
    (stored,) = [dict(r) for r in conn.execute("SELECT kind, sent_at, error FROM alerts")]
    assert stored == {"kind": "halt", "sent_at": None, "error": "no email settings in missing.json"}
    assert logs == ["alert: PredictionArbitrageTrader live trading halted", "alert 1 was not emailed: no email settings in missing.json"]


def test_an_email_upgrades_to_tls_logs_in_and_sends(monkeypatch):
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None, context=None):
            calls.append(("connect", host, port, "tls" if context else "plain"))

        def starttls(self, context=None):
            calls.append(("starttls",))

        def login(self, user, password):
            calls.append(("login", user))

        def send_message(self, message):
            calls.append(("send", message["To"], message["Subject"], message.get_content().strip()))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            calls.append(("quit",))
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSMTP)
    settings = {"host": "smtp.example.com", "port": 587, "user": "bot", "password": "p", "from": "bot@example.com", "to": ["a@x.com", "b@x.com"]}
    notify.send_email(settings, "subject", "body")
    assert calls == [("connect", "smtp.example.com", 587, "plain"), ("starttls",), ("login", "bot"),
                     ("send", "a@x.com, b@x.com", "subject", "body"), ("quit",)]
    calls.clear()
    notify.send_email(dict(settings, port=465, user=None), "subject", "body")
    assert calls[0] == ("connect", "smtp.example.com", 465, "tls") and ("starttls",) not in calls and calls[1][0] == "send"


def test_the_attestation_watch_emails_before_the_day_it_lapses_and_once_it_has_each_only_once(tmp_path, monkeypatch):
    conn = database.connect(tmp_path / "t.sqlite")
    monkeypatch.setattr(notify, "EMAIL_FILE", tmp_path / "missing.json")
    notifier = notify.Notifier(conn, lambda m: None)
    lapses = {"at": epoch("2026-10-05T14:22:13+00:00")}

    def check(now):
        async def scenario():
            await notify.AttestationWatch(conn, notifier, read=lambda: lapses["at"]).check(now)    # A new one each time, as after a restart.
            await asyncio.gather(*notifier.tasks)
        asyncio.run(scenario())

    for now in ["2026-10-02T12:00:00+00:00",        # 74 hours ahead, not yet.
                "2026-10-03T15:00:00+00:00",        # 47 hours ahead, inside the 48.
                "2026-10-04T09:00:00+00:00",        # Again, but it went out already.
                "2026-10-05T15:00:00+00:00"]:       # Lapsed.
        check(now)
    lapses["at"] = epoch("2026-11-05T14:22:13+00:00")       # Renewed.
    check("2026-11-03T15:00:00+00:00")
    alerts = [dict(r) for r in conn.execute("SELECT ts, kind, subject, body FROM alerts ORDER BY id")]
    assert [(a["ts"], a["kind"], a["subject"]) for a in alerts] == [
        ("2026-10-03T15:00:00+00:00", "attestation", "PredictionArbitrageTrader: renew the Kalshi key's location attestation before 2026-10-05 14:22 UTC"),
        ("2026-10-05T15:00:00+00:00", "attestation", "PredictionArbitrageTrader: the Kalshi key's location attestation lapsed at 2026-10-05 14:22 UTC"),
        ("2026-11-03T15:00:00+00:00", "attestation", "PredictionArbitrageTrader: renew the Kalshi key's location attestation before 2026-11-05 14:22 UTC")]
    assert "in 47 hours. Renew it on Kalshi before then." in alerts[0]["body"]


def test_the_attestation_watch_raises_nothing_when_kalshi_gives_no_date(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    notifier = notify.Notifier(conn, lambda m: None, sender=lambda *args: pytest.fail("nothing to send"))
    asyncio.run(notify.AttestationWatch(conn, notifier, read=lambda: None).check("2026-10-05T15:00:00+00:00"))
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0
