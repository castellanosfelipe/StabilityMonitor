"""Windows notification adapter: native toasts + optional local .wav sound.

All Windows-only imports are lazy so the module stays importable (and
testable via mocks) on any platform. The tray icon lives in
``tray_windows.py``; this module only emits toasts/sound and forwards the
state to the tray when one is registered.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from app import config
from app.db import Database
from app.settings_store import get_str

if TYPE_CHECKING:  # pragma: no cover
    from app.alerts import Alert

logger = logging.getLogger(__name__)

SOUND_FILE = Path(__file__).resolve().parent.parent.parent / "static" / "sounds" / "alert.wav"


class WindowsNotifier:
    channel = "toast"

    def __init__(self, db: Database, on_state_change: Callable[[str], None] | None = None) -> None:
        self._db = db
        # Callback hacia la bandeja (verde/rojo) — registrado por tray_windows.
        self.on_state_change = on_state_change

    def notify(self, alert: "Alert") -> None:
        if self.on_state_change is not None:
            try:
                self.on_state_change("down" if alert.severity != "recovered" else "up")
            except Exception:
                logger.exception("error actualizando el ícono de bandeja")
        self._toast(alert)
        if alert.severity != "recovered" and get_str(self._db, "alerts.sound_enabled") == "1":
            self._play_sound()

    def _toast(self, alert: "Alert") -> None:
        from winotify import Notification  # type: ignore[import-not-found]

        toast = Notification(
            app_id=config.APP_NAME,
            title=alert.title,
            msg=alert.message,
        )
        toast.show()

    def _play_sound(self) -> None:
        try:
            import winsound  # type: ignore[import-not-found]

            if SOUND_FILE.exists():
                winsound.PlaySound(
                    str(SOUND_FILE), winsound.SND_FILENAME | winsound.SND_ASYNC
                )
        except Exception:
            logger.debug("no se pudo reproducir el sonido de alerta", exc_info=True)
