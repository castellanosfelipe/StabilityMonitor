"""MySQL / MariaDB checker (PyMySQL, pure Python).

Identifiable traffic: ``program_name`` connection attribute is set to
``StabilityMonitor/x.y.z`` (visible in ``performance_schema.session_connect_attrs``).
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import pymysql

from app import config
from app.checkers.db_base import DbChecker
from app.errors import ErrorType
from app.models import ConnectionConfig

# MySQL/MariaDB error codes → cause.
_CODE_MAP: dict[int, tuple[ErrorType, str]] = {
    1045: (ErrorType.AUTH, "autenticación rechazada (usuario o contraseña)"),
    1698: (ErrorType.AUTH, "autenticación rechazada (plugin de autenticación)"),
    1049: (ErrorType.DB_MISSING, "la base de datos no existe"),
    1044: (ErrorType.PERMISSION, "acceso denegado a la base de datos"),
    1142: (ErrorType.PERMISSION, "permiso denegado sobre la tabla"),
    1143: (ErrorType.PERMISSION, "permiso denegado sobre la columna"),
    1146: (ErrorType.TARGET_MISSING, "la tabla no existe"),
    2003: (ErrorType.TCP_CONNECT, "no se pudo conectar al servidor"),
    2006: (ErrorType.TCP_CONNECT, "el servidor cerró la conexión"),
    2013: (ErrorType.TCP_CONNECT, "se perdió la conexión durante la consulta"),
    3024: (ErrorType.QUERY_TIMEOUT, "la consulta excedió el tiempo máximo"),
    1969: (ErrorType.QUERY_TIMEOUT, "la consulta excedió max_statement_time"),  # MariaDB
}


class MySqlChecker(DbChecker):
    validation_query = "SELECT 1"

    def _connect(self, cfg: ConnectionConfig, secret: str | None) -> Any:
        return pymysql.connect(
            host=cfg.host,
            port=cfg.port,
            user=cfg.username,
            password=secret or "",
            database=cfg.db_name or None,
            connect_timeout=int(max(1, cfg.timeout_s)),
            read_timeout=cfg.timeout_s,
            write_timeout=cfg.timeout_s,
            program_name=config.USER_AGENT,
            charset="utf8mb4",
            autocommit=True,
            # TLS without chain verification under "required" (LAN self-signed);
            # PyMySQL does not negotiate TLS unless asked, so "preferred" is plain.
            ssl={} if cfg.ssl_mode == "required" else None,
            ssl_disabled=True if cfg.ssl_mode == "disabled" else None,
        )

    def _classify(self, exc: Exception) -> tuple[ErrorType, str] | None:
        if isinstance(exc, pymysql.err.MySQLError) and exc.args:
            code = exc.args[0]
            if isinstance(code, int):
                hit = _CODE_MAP.get(code)
                if hit is not None:
                    detail = exc.args[1] if len(exc.args) > 1 else ""
                    return hit[0], f"{hit[1]}: {detail}" if detail else hit[1]
        return None

    def _schema_exists(self, conn: Any, schema: str) -> bool:
        return self._exists(
            conn,
            "SELECT 1 FROM information_schema.SCHEMATA WHERE lower(SCHEMA_NAME) = lower(%s)",
            (schema,),
        )

    def _table_exists(self, conn: Any, schema: str, table: str) -> bool:
        return self._exists(
            conn,
            "SELECT 1 FROM information_schema.TABLES "
            "WHERE lower(TABLE_SCHEMA) = lower(%s) AND lower(TABLE_NAME) = lower(%s)",
            (schema, table),
        )

    @contextmanager
    def _query_timeout(self, conn: Any, timeout_s: float) -> Iterator[None]:
        # PyMySQL consults _read_timeout on each query; tighten it temporarily.
        previous = getattr(conn, "_read_timeout", None)
        try:
            conn._read_timeout = timeout_s
        except Exception:
            yield
            return
        try:
            yield
        finally:
            try:
                conn._read_timeout = previous
            except Exception:
                pass
