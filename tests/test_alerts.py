"""Alert orchestration tests: channels, anti-spam, reminders, alerts_log."""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

from app.alerts import Alert, Alerter, humanize_duration, smtp_config
from app.db import Database
from app.incidents import IncidentClosed, IncidentOpened
from app.models import ConnectionConfig, Protocol

T0 = datetime(2026, 2, 1, 8, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


class CaptureNotifier:
    channel = "toast"

    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def notify(self, alert: Alert) -> None:
        self.alerts.append(alert)


@pytest.fixture()
def env(tmp_path):
    db = Database(tmp_path / "alerts.db")
    cfg = ConnectionConfig(
        id=None, name="SFTP Acme", client="ACME", protocol=Protocol.SFTP,
        host="sftp.acme.local", port=22,
    )
    db.create_connection(cfg)
    incident_id = db.open_incident(cfg.id, "2026-02-01T07:55:00.000Z", "tcp_connect", "rechazada")
    return db, cfg, incident_id


def opened_event(cfg, incident_id):
    return IncidentOpened(
        incident_id=incident_id, connection_id=cfg.id,
        started_at=T0 - timedelta(minutes=5), error_type="tcp_connect", message="rechazada",
    )


def closed_event(cfg, incident_id, duration_s=300.0):
    return IncidentClosed(
        incident_id=incident_id, connection_id=cfg.id,
        started_at=T0 - timedelta(minutes=5), ended_at=T0,
        duration_s=duration_s, error_type="tcp_connect",
    )


def make_alerter(db, notifier, clock, smtp=None, webhook=None):
    return Alerter(
        db, notifier, clock=clock,
        smtp_sender=smtp or (lambda cfg, alert: None),
        webhook_sender=webhook or (lambda url, alert: None),
    )


def test_down_and_recovery_alerts_with_log(env):
    db, cfg, incident_id = env
    notifier = CaptureNotifier()
    alerter = make_alerter(db, notifier, FakeClock())

    alerter.handle_events([opened_event(cfg, incident_id)])
    assert len(notifier.alerts) == 1
    down = notifier.alerts[0]
    assert down.severity == "down"
    assert "SFTP Acme" in down.title and "CAÍDA" in down.title
    assert "tcp_connect" in down.message

    alerter.handle_events([closed_event(cfg, incident_id)])
    recovered = notifier.alerts[1]
    assert recovered.severity == "recovered"
    assert "5.0 min" in recovered.message  # duración humanizada

    rows = list(db.execute("SELECT channel, ok FROM alerts_log"))
    assert [(r["channel"], r["ok"]) for r in rows] == [("toast", 1), ("toast", 1)]


def test_notifier_failure_is_logged_not_raised(env):
    db, cfg, incident_id = env

    class BrokenNotifier:
        channel = "toast"

        def notify(self, alert):
            raise RuntimeError("winotify explotó")

    alerter = make_alerter(db, BrokenNotifier(), FakeClock())
    alerter.handle_events([opened_event(cfg, incident_id)])  # no debe lanzar
    row = db.execute("SELECT ok, detail FROM alerts_log").fetchone()
    assert row["ok"] == 0 and "winotify" in row["detail"]


def test_smtp_and_webhook_channels_when_enabled(env):
    db, cfg, incident_id = env
    db.set_setting("alerts.smtp_enabled", "1")
    db.set_setting("smtp.host", "smtp.lan")
    db.set_setting("smtp.to", "ops@lan, jefe@lan")
    db.set_setting("alerts.webhook_enabled", "1")
    db.set_setting("webhook.url", "http://hook.lan/x")

    sent = {"smtp": [], "webhook": []}
    alerter = make_alerter(
        db, CaptureNotifier(), FakeClock(),
        smtp=lambda c, a: sent["smtp"].append((c, a)),
        webhook=lambda url, a: sent["webhook"].append((url, a)),
    )
    alerter.handle_events([opened_event(cfg, incident_id)])

    assert len(sent["smtp"]) == 1
    smtp_cfg, alert = sent["smtp"][0]
    assert smtp_cfg.mail_to == ("ops@lan", "jefe@lan")
    assert alert.payload["event"] == "incident_opened"
    assert len(sent["webhook"]) == 1
    assert sent["webhook"][0][0] == "http://hook.lan/x"

    channels = [r["channel"] for r in db.execute("SELECT channel FROM alerts_log")]
    assert channels == ["toast", "smtp", "webhook"]


def test_channels_disabled_by_default(env):
    db, cfg, incident_id = env
    called = []
    alerter = make_alerter(
        db, CaptureNotifier(), FakeClock(),
        smtp=lambda c, a: called.append("smtp"),
        webhook=lambda u, a: called.append("webhook"),
    )
    alerter.handle_events([opened_event(cfg, incident_id)])
    assert called == []  # apagados por defecto (RF-4)


def test_reminders_only_when_configured_and_spaced(env):
    db, cfg, incident_id = env
    clock = FakeClock()
    notifier = CaptureNotifier()
    alerter = make_alerter(db, notifier, clock)
    alerter.handle_events([opened_event(cfg, incident_id)])
    assert len(notifier.alerts) == 1

    # sin configurar: nunca recuerda
    clock.advance(hours=2)
    alerter.check_reminders()
    assert len(notifier.alerts) == 1

    db.set_setting("alerts.reminder_minutes", "15")
    alerter.check_reminders()  # ya pasaron 2 h desde la alerta → recuerda
    assert len(notifier.alerts) == 2
    assert notifier.alerts[1].severity == "reminder"
    assert "SIGUE CAÍDA" in notifier.alerts[1].title

    alerter.check_reminders()  # inmediato: aún no pasan 15 min → anti-spam
    assert len(notifier.alerts) == 2
    clock.advance(minutes=16)
    alerter.check_reminders()
    assert len(notifier.alerts) == 3

    # al recuperarse, no hay más recordatorios
    alerter.handle_events([closed_event(cfg, incident_id)])
    clock.advance(hours=1)
    alerter.check_reminders()
    assert len(notifier.alerts) == 4  # solo la de recuperación


def test_restart_does_not_realert_but_keeps_reminding(env):
    db, cfg, incident_id = env
    clock = FakeClock()
    notifier = CaptureNotifier()
    db.set_setting("alerts.reminder_minutes", "10")

    # nuevo Alerter con un incidente ya abierto en la BD (reinicio de la app)
    alerter = make_alerter(db, notifier, clock)
    assert notifier.alerts == []  # sin re-alertar al arrancar

    clock.advance(minutes=11)
    alerter.check_reminders()
    assert len(notifier.alerts) == 1
    assert notifier.alerts[0].severity == "reminder"


def test_smtp_config_decrypts_secret_tokens(env, monkeypatch):
    db, _, _ = env
    db.set_setting("alerts.smtp_enabled", "1")
    db.set_setting("smtp.host", "smtp.lan")
    db.set_setting("smtp.to", "ops@lan")
    db.set_setting("smtp.password", "fernet:token-cifrado")

    fake_store = types.SimpleNamespace(decrypt=lambda token: "clave-real")
    monkeypatch.setattr("app.platform.secretstore.get_secret_store", lambda mode=None: fake_store)
    cfg = smtp_config(db)
    assert cfg is not None and cfg.password == "clave-real"


def test_humanize_duration():
    assert humanize_duration(45) == "45 s"
    assert humanize_duration(300) == "5.0 min"
    assert humanize_duration(7200) == "2.0 h"
    assert humanize_duration(3 * 86400) == "3.0 días"


def test_windows_notifier_toast_and_sound_mocked(env, monkeypatch):
    db, cfg, incident_id = env
    shown = []
    played = []

    class FakeToast:
        def __init__(self, app_id, title, msg):
            self.args = (app_id, title, msg)

        def show(self):
            shown.append(self.args)

    fake_winotify = types.SimpleNamespace(Notification=FakeToast)
    fake_winsound = types.SimpleNamespace(
        SND_FILENAME=1, SND_ASYNC=2,
        PlaySound=lambda path, flags: played.append(path),
    )
    monkeypatch.setitem(sys.modules, "winotify", fake_winotify)
    monkeypatch.setitem(sys.modules, "winsound", fake_winsound)

    from app.platform.notify_windows import WindowsNotifier

    states = []
    notifier = WindowsNotifier(db, on_state_change=states.append)
    alerter = make_alerter(db, notifier, FakeClock())
    alerter.handle_events([opened_event(cfg, incident_id)])
    alerter.handle_events([closed_event(cfg, incident_id)])

    assert len(shown) == 2
    assert shown[0][0] == "StabilityMonitor" and "CAÍDA" in shown[0][1]
    assert states == ["down", "up"]
    assert len(played) == 1  # suena solo en caída, no en recuperación
