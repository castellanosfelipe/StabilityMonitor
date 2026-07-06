"""System tray icon for Modo A (pystray + Pillow, lazy imports).

Menu: Abrir dashboard / Pausar todo / Reanudar / Salir. The icon turns red
while any incident is open and green when everything recovered.
"""
from __future__ import annotations

import logging
import threading
import webbrowser
from typing import TYPE_CHECKING, Callable

from app import config

if TYPE_CHECKING:  # pragma: no cover
    from app.scheduler import MonitorEngine

logger = logging.getLogger(__name__)

_COLORS = {"up": (34, 163, 74), "down": (220, 38, 38), "paused": (148, 163, 184)}


def _make_image(color: tuple[int, int, int]):
    from PIL import Image, ImageDraw  # type: ignore[import-not-found]

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, 56, 56), fill=color + (255,))
    return image


class TrayApp:
    def __init__(self, engine: "MonitorEngine", port: int, on_quit: Callable[[], None]) -> None:
        self._engine = engine
        self._port = port
        self._on_quit = on_quit
        self._icon = None
        self._state = "up"

    # Called by WindowsNotifier when incidents open/close.
    def set_state(self, state: str) -> None:
        self._state = state
        if self._icon is not None:
            try:
                self._icon.icon = _make_image(_COLORS.get(state, _COLORS["up"]))
            except Exception:
                logger.debug("no se pudo actualizar el ícono", exc_info=True)

    def _open_dashboard(self) -> None:
        webbrowser.open(f"http://127.0.0.1:{self._port}/")

    def _pause(self) -> None:
        self._engine.pause_all()
        self.set_state("paused")

    def _resume(self) -> None:
        self._engine.resume_all()
        self.set_state("up")

    def _quit(self) -> None:
        if self._icon is not None:
            self._icon.stop()
        self._on_quit()

    def start(self) -> None:
        """Run the tray icon in a daemon thread; never blocks the server."""
        try:
            import pystray  # type: ignore[import-not-found]
        except ImportError:
            logger.warning("pystray no disponible: sin ícono de bandeja")
            return

        menu = pystray.Menu(
            pystray.MenuItem("Abrir dashboard", lambda: self._open_dashboard(), default=True),
            pystray.MenuItem("Pausar todo", lambda: self._pause()),
            pystray.MenuItem("Reanudar", lambda: self._resume()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Salir", lambda: self._quit()),
        )
        self._icon = pystray.Icon(
            config.APP_NAME, _make_image(_COLORS["up"]), config.APP_NAME, menu
        )
        threading.Thread(target=self._icon.run, name="tray", daemon=True).start()
