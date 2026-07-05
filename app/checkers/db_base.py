"""Common flow for all database checkers (RF-2).

The check is always: TCP connect → authenticate → validation query (one row)
→ optionally verify each target (schema or table) via the engine's catalog,
one parameterized single-row query each → optionally run the restricted
health query → clean close.

Courtesy guarantees enforced here:
- Every query reads at most one row (``fetchone``); never full result sets.
- The health query is re-validated immediately before execution (defense in
  depth: whatever is stored in the DB is *also* checked at runtime) and runs
  under a timeout capped at ``HEALTH_QUERY_TIMEOUT_S`` where the driver allows
  it.
- A failed health query or missing target degrades the connection, it does not
  mark it DOWN — the server answered; it is the *content* that is wrong.
"""
from __future__ import annotations

from abc import abstractmethod
from contextlib import contextmanager
from typing import Any, ClassVar, Iterator

from app.checkers.base import BaseChecker
from app.errors import CheckError, ErrorType, classify_exception
from app.models import (
    HEALTH_QUERY_TIMEOUT_S,
    ConnectionConfig,
    TargetResult,
    validate_health_query,
)

HEALTH_LABEL = "(query de salud)"


class DbChecker(BaseChecker):
    validation_query: ClassVar[str] = "SELECT 1"

    def _execute(self, cfg: ConnectionConfig, secret: str | None) -> list[TargetResult]:
        try:
            conn = self._connect(cfg, secret)
        except Exception as exc:
            self._reraise_classified(exc)
            raise  # unreachable; keeps type-checkers happy
        try:
            self._query_one(conn, self.validation_query, ())
            results = [self._check_target(conn, target) for target in cfg.targets]
            if cfg.health_query:
                results.append(self._run_health_query(conn, cfg))
            return results
        except Exception as exc:
            self._reraise_classified(exc)
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _reraise_classified(self, exc: Exception) -> None:
        """Re-raise with driver-specific classification applied when known."""
        if isinstance(exc, CheckError):
            raise exc
        hit = self._classify(exc)
        if hit is not None:
            raise CheckError(hit[0], hit[1]) from exc
        raise exc

    # --- targets ----------------------------------------------------------------

    def _check_target(self, conn: Any, target: str) -> TargetResult:
        parts = target.split(".", 1)
        try:
            if len(parts) == 1:
                exists = self._schema_exists(conn, parts[0])
                missing_msg = "el esquema no existe"
            else:
                exists = self._table_exists(conn, parts[0], parts[1])
                missing_msg = "la tabla no existe"
        except Exception as exc:
            error_type, message = self._classify(exc) or classify_exception(exc)
            return TargetResult(target=target, ok=False, error_type=error_type, message=message)
        if not exists:
            return TargetResult(
                target=target, ok=False, error_type=ErrorType.TARGET_MISSING, message=missing_msg
            )
        return TargetResult(target=target, ok=True)

    # --- health query --------------------------------------------------------------

    def _run_health_query(self, conn: Any, cfg: ConnectionConfig) -> TargetResult:
        query = cfg.health_query or ""
        rejection = validate_health_query(query)
        if rejection is not None:
            return TargetResult(
                target=HEALTH_LABEL,
                ok=False,
                error_type=ErrorType.PROTOCOL,
                message=f"query de salud rechazada: {rejection}",
            )
        timeout_s = min(float(HEALTH_QUERY_TIMEOUT_S), cfg.timeout_s)
        try:
            with self._query_timeout(conn, timeout_s):
                self._query_one(conn, query.rstrip().rstrip(";"), ())
        except Exception as exc:
            error_type, message = self._classify(exc) or classify_exception(exc)
            if error_type is ErrorType.TCP_TIMEOUT:
                error_type = ErrorType.QUERY_TIMEOUT
                message = f"la query de salud excedió el tiempo límite ({timeout_s:.0f} s)"
            return TargetResult(
                target=HEALTH_LABEL, ok=False, error_type=error_type, message=message
            )
        return TargetResult(target=HEALTH_LABEL, ok=True)

    # --- helpers ---------------------------------------------------------------------

    def _query_one(self, conn: Any, sql: str, params: tuple[Any, ...]) -> Any:
        """Run one query and read at most one row (courtesy: never full scans)."""
        cursor = conn.cursor()
        try:
            if params:
                cursor.execute(sql, params)
            else:
                cursor.execute(sql)
            return cursor.fetchone()
        finally:
            try:
                cursor.close()
            except Exception:
                pass

    def _exists(self, conn: Any, sql: str, params: tuple[Any, ...]) -> bool:
        return self._query_one(conn, sql, params) is not None

    @contextmanager
    def _query_timeout(self, conn: Any, timeout_s: float) -> Iterator[None]:
        """Best-effort per-query timeout; overridden where the driver allows it.

        The connection-level socket timeout (``cfg.timeout_s``) always applies
        as the outer bound.
        """
        yield

    # --- driver-specific ----------------------------------------------------------------

    @abstractmethod
    def _connect(self, cfg: ConnectionConfig, secret: str | None) -> Any:
        """Open a connection with strict timeouts and identifiable app name."""

    @abstractmethod
    def _classify(self, exc: Exception) -> tuple[ErrorType, str] | None:
        """Driver-specific error mapping; ``None`` falls back to the generic one."""

    @abstractmethod
    def _schema_exists(self, conn: Any, schema: str) -> bool: ...

    @abstractmethod
    def _table_exists(self, conn: Any, schema: str, table: str) -> bool: ...
