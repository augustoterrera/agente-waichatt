from __future__ import annotations

import logging
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

from .config import settings

logger = logging.getLogger(__name__)

__all__ = ["DBError", "Json", "execute", "execute_returning", "fetch_all", "fetch_one", "fetch_val"]


class DBError(RuntimeError):
    """Error de acceso a Postgres. Transitorio para las tasks de Celery (se reintenta)."""


_pool: ConnectionPool | None = None


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        if not settings.database_url:
            raise DBError("Falta DATABASE_URL: el agente no puede leer ni persistir.")
        # min_size=0: no abre conexiones hasta que hagan falta (la API arranca aunque la DB
        # esté caída un momento; el primer uso real conecta o falla con DBError → retry).
        _pool = ConnectionPool(
            settings.database_url,
            min_size=0,
            max_size=settings.db_pool_max,
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _pool


def _run(sql: str, params: tuple | dict | None, *, fetch: str) -> Any:
    """Una sentencia = una transacción (commit al salir del with). Las operaciones atómicas
    (dedup, locks, claims) son sentencias únicas con ON CONFLICT / WHERE condicional, así que
    no hace falta manejo de transacciones multi-statement."""
    try:
        with _get_pool().connection() as conn:
            cur = conn.execute(sql, params)
            if fetch == "all":
                return cur.fetchall()
            if fetch == "one":
                return cur.fetchone()
            if fetch == "val":
                row = cur.fetchone()
                return next(iter(row.values())) if row else None
            return cur.rowcount
    except psycopg.Error as exc:
        raise DBError(f"Postgres: {exc}") from exc


def fetch_all(sql: str, params: tuple | dict | None = None) -> list[dict[str, Any]]:
    return _run(sql, params, fetch="all")


def fetch_one(sql: str, params: tuple | dict | None = None) -> dict[str, Any] | None:
    return _run(sql, params, fetch="one")


def fetch_val(sql: str, params: tuple | dict | None = None) -> Any:
    return _run(sql, params, fetch="val")


def execute(sql: str, params: tuple | dict | None = None) -> int:
    """Ejecuta y devuelve rowcount (para claims/locks: 0 = otro lo tomó primero)."""
    return _run(sql, params, fetch="rowcount")


def execute_returning(sql: str, params: tuple | dict | None = None) -> list[dict[str, Any]]:
    return _run(sql, params, fetch="all")
