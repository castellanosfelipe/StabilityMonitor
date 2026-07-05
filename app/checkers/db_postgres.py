"""PostgreSQL checker (pg8000, pure Python).

Identifiable traffic: ``application_name`` is set to ``StabilityMonitor/x.y.z``
so administrators can spot and filter the monitor in ``pg_stat_activity``.
"""
from __future__ import annotations

import ssl
from contextlib import contextmanager
from typing import Any, Iterator

import pg8000.dbapi
import pg8000.exceptions

from app import config
from app.checkers.db_base import DbChecker
from app.errors import ErrorType
from app.models import ConnectionConfig

# SQLSTATE → cause. https://www.postgresql.org/docs/current/errcodes-appendix.html
_SQLSTATE_MAP: dict[str, tuple[ErrorType, str]] = {
    "28P01": (ErrorType.AUTH, "autenticación rechazada (contraseña inválida)"),
    "28000": (ErrorType.AUTH, "autenticación rechazada"),
    "3D000": (ErrorType.DB_MISSING, "la base de datos no existe"),
    "42501": (ErrorType.PERMISSION, "permiso denegado"),
    "42P01": (ErrorType.TARGET_MISSING, "la tabla no existe"),
    "3F000": (ErrorType.TARGET_MISSING, "el esquema no existe"),
    "57014": (ErrorType.QUERY_TIMEOUT, "la consulta fue cancelada por tiempo"),
    "53300": (ErrorType.PROTOCOL, "demasiadas conexiones en el servidor"),
}


def _unverified_tls_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class PostgresChecker(DbChecker):
    validation_query = "SELECT 1"

    def _connect(self, cfg: ConnectionConfig, secret: str | None) -> Any:
        ssl_context = _unverified_tls_context() if cfg.ssl_mode == "required" else None
        conn = pg8000.dbapi.connect(
            user=cfg.username,
            password=secret,
            host=cfg.host,
            port=cfg.port,
            database=cfg.db_name,
            timeout=cfg.timeout_s,
            ssl_context=ssl_context,
            application_name=config.USER_AGENT,
        )
        # Never sit "idle in transaction" on the monitored server (RF-2).
        conn.autocommit = True
        return conn

    def _classify(self, exc: Exception) -> tuple[ErrorType, str] | None:
        if isinstance(exc, pg8000.exceptions.DatabaseError) and exc.args:
            detail = exc.args[0]
            if isinstance(detail, dict):
                sqlstate = detail.get("C", "")
                hit = _SQLSTATE_MAP.get(sqlstate)
                if hit is not None:
                    message = detail.get("M", "")
                    return hit[0], f"{hit[1]}: {message}" if message else hit[1]
                if sqlstate.startswith("08"):  # connection_exception family
                    return ErrorType.TCP_CONNECT, detail.get("M", "error de conexión")
                if sqlstate.startswith("28"):
                    return ErrorType.AUTH, detail.get("M", "autenticación rechazada")
        return None

    def _schema_exists(self, conn: Any, schema: str) -> bool:
        return self._exists(
            conn,
            "SELECT 1 FROM information_schema.schemata WHERE lower(schema_name) = lower(%s)",
            (schema,),
        )

    def _table_exists(self, conn: Any, schema: str, table: str) -> bool:
        return self._exists(
            conn,
            "SELECT 1 FROM information_schema.tables "
            "WHERE lower(table_schema) = lower(%s) AND lower(table_name) = lower(%s)",
            (schema, table),
        )

    @contextmanager
    def _query_timeout(self, conn: Any, timeout_s: float) -> Iterator[None]:
        # pg8000 has no per-query timeout; temporarily tighten the socket timeout.
        sock = getattr(conn, "_usock", None)
        if sock is None:
            yield
            return
        previous = sock.gettimeout()
        sock.settimeout(timeout_s)
        try:
            yield
        finally:
            try:
                sock.settimeout(previous)
            except Exception:
                pass
