"""Checker registry: maps each protocol to its checker implementation."""
from __future__ import annotations

from app.checkers.base import BaseChecker
from app.checkers.db_mysql import MySqlChecker
from app.checkers.db_oracle import OracleChecker
from app.checkers.db_postgres import PostgresChecker
from app.checkers.db_sqlserver import SqlServerChecker
from app.checkers.ftp import FtpChecker
from app.checkers.sftp import SftpChecker
from app.checkers.webdav import WebDavChecker
from app.models import Protocol

_REGISTRY: dict[Protocol, type[BaseChecker]] = {
    Protocol.FTP: FtpChecker,
    Protocol.FTPS: FtpChecker,
    Protocol.SFTP: SftpChecker,
    Protocol.WEBDAV: WebDavChecker,
    Protocol.WEBDAVS: WebDavChecker,
    Protocol.POSTGRES: PostgresChecker,
    Protocol.MYSQL: MySqlChecker,
    Protocol.MARIADB: MySqlChecker,
    Protocol.SQLSERVER: SqlServerChecker,
    Protocol.ORACLE: OracleChecker,
}


def get_checker(protocol: Protocol) -> BaseChecker:
    """Return a fresh checker instance (checkers hold no shared mutable state)."""
    return _REGISTRY[protocol]()
