"""Integration tests against real database containers (Fase 2).

Skipped unless ``MONITOR_IT=1``. Expected containers (see docs/DECISIONS.md):

    docker run -d --name it-pg      -e POSTGRES_USER=monitor -e POSTGRES_PASSWORD=s3cret \
        -e POSTGRES_DB=ventas -p 55432:5432 postgres:16-alpine
    docker run -d --name it-mysql   -e MYSQL_ROOT_PASSWORD=s3cret -e MYSQL_DATABASE=ventas \
        -e MYSQL_USER=monitor -e MYSQL_PASSWORD=s3cret -p 53306:3306 mysql:8.4
    docker run -d --name it-mariadb -e MARIADB_ROOT_PASSWORD=s3cret -e MARIADB_DATABASE=ventas \
        -e MARIADB_USER=monitor -e MARIADB_PASSWORD=s3cret -p 53307:3306 mariadb:11
    # opcionales (MONITOR_IT_MSSQL=1 / MONITOR_IT_ORACLE=1):
    docker run -d --name it-mssql --platform linux/amd64 -e ACCEPT_EULA=Y \
        -e MSSQL_SA_PASSWORD='S3cret!Passw0rd' -p 51433:1433 mcr.microsoft.com/mssql/server:2022-latest
    docker run -d --name it-oracle -e ORACLE_PASSWORD=s3cret -e APP_USER=monitor \
        -e APP_USER_PASSWORD=s3cret -p 51521:1521 gvenzl/oracle-free:23-slim-faststart

Each engine gets a ``pedidos`` table created by the fixture.
"""
from __future__ import annotations

import os

import pytest

from app.checkers import get_checker
from app.errors import ErrorType
from app.models import ConnectionConfig, Protocol, Status

pytestmark = pytest.mark.skipif(
    os.environ.get("MONITOR_IT") != "1",
    reason="integración: requiere contenedores locales (MONITOR_IT=1)",
)

HOST = os.environ.get("MONITOR_IT_HOST", "127.0.0.1")
SECRET = "s3cret"


def cfg_for(protocol: Protocol, default_port: int, **overrides) -> ConnectionConfig:
    base = dict(
        id=None, name=f"it-{protocol.value.lower()}", client="IT",
        protocol=protocol, host=HOST, port=default_port, username="monitor",
        db_name="ventas", timeout_s=8.0,
    )
    base.update(overrides)
    return ConnectionConfig(**base)


def run(cfg: ConnectionConfig, secret: str = SECRET):
    return get_checker(cfg.protocol).check(cfg, secret)


class EngineContract:
    """Same behavioral contract for every engine (RF-2 acceptance criteria)."""

    protocol: Protocol
    port: int
    schema = "ventas"  # schema/database holding the `pedidos` table
    health_query = "SELECT 1"
    secret = SECRET

    def cfg(self, **overrides) -> ConnectionConfig:
        return cfg_for(self.protocol, self.port, **overrides)

    def test_up_with_latency(self):
        result = run(self.cfg(), self.secret)
        assert result.status is Status.UP, result
        assert result.latency_ms is not None and result.latency_ms > 0

    def test_bad_password_is_auth(self):
        result = run(self.cfg(), secret="incorrecta")
        assert result.status is Status.DOWN
        assert result.error_type is ErrorType.AUTH, result

    # A limited monitoring user may get "permission denied" instead of "does not
    # exist" (MySQL 1044 vs 1049): both are clearly distinct from "server down".
    missing_db_causes = (ErrorType.DB_MISSING,)

    def test_missing_database_is_db_missing(self):
        result = run(self.cfg(db_name="no_existe_xyz"), self.secret)
        assert result.status is Status.DOWN
        assert result.error_type in self.missing_db_causes, result

    def test_existing_targets_are_up(self):
        result = run(self.cfg(targets=[self.schema, f"{self.schema}.pedidos"]), self.secret)
        assert result.status is Status.UP, result

    def test_missing_table_is_target_missing_not_down(self):
        result = run(self.cfg(targets=[f"{self.schema}.tabla_fantasma"]), self.secret)
        assert result.status is Status.DEGRADED, result
        assert result.error_type is ErrorType.TARGET_MISSING

    def test_health_query_runs(self):
        result = run(self.cfg(health_query=self.health_query), self.secret)
        assert result.status is Status.UP, result

    def test_closed_port_is_tcp_connect(self):
        result = run(self.cfg(port=59999, timeout_s=3.0), self.secret)
        assert result.status is Status.DOWN
        assert result.error_type in (ErrorType.TCP_CONNECT, ErrorType.TCP_TIMEOUT), result


class TestPostgres(EngineContract):
    protocol = Protocol.POSTGRES
    port = int(os.environ.get("MONITOR_IT_PG_PORT", "55432"))
    schema = "public"


class TestMySql(EngineContract):
    protocol = Protocol.MYSQL
    port = int(os.environ.get("MONITOR_IT_MYSQL_PORT", "53306"))
    missing_db_causes = (ErrorType.DB_MISSING, ErrorType.PERMISSION)


class TestMariaDb(EngineContract):
    protocol = Protocol.MARIADB
    port = int(os.environ.get("MONITOR_IT_MARIADB_PORT", "53307"))
    missing_db_causes = (ErrorType.DB_MISSING, ErrorType.PERMISSION)


@pytest.mark.skipif(
    os.environ.get("MONITOR_IT_MSSQL") != "1",
    reason="SQL Server bajo emulación amd64; activar con MONITOR_IT_MSSQL=1",
)
class TestSqlServer(EngineContract):
    protocol = Protocol.SQLSERVER
    port = int(os.environ.get("MONITOR_IT_MSSQL_PORT", "51433"))
    schema = "dbo"
    secret = os.environ.get("MONITOR_IT_MSSQL_PASS", "S3cret!Passw0rd")

    def cfg(self, **overrides) -> ConnectionConfig:
        base = dict(username="sa", db_name="ventas")
        base.update(overrides)
        return cfg_for(self.protocol, self.port, **base)


@pytest.mark.skipif(
    os.environ.get("MONITOR_IT_ORACLE") != "1",
    reason="Oracle Free tarda en arrancar; activar con MONITOR_IT_ORACLE=1",
)
class TestOracle(EngineContract):
    protocol = Protocol.ORACLE
    port = int(os.environ.get("MONITOR_IT_ORACLE_PORT", "51521"))
    schema = "monitor"
    health_query = "SELECT 1 FROM DUAL"

    def cfg(self, **overrides) -> ConnectionConfig:
        base = dict(db_name="FREEPDB1")
        base.update(overrides)
        return cfg_for(self.protocol, self.port, **base)

    def test_missing_database_is_db_missing(self):
        result = run(self.cfg(db_name="NO_EXISTE"))
        assert result.status is Status.DOWN
        assert result.error_type is ErrorType.DB_MISSING, result
