"""PostgreSQL connection helpers for the shared account registry."""
from __future__ import annotations

import os
import threading
from typing import Any

_POOLS: dict[str, Any] = {}
_POOLS_LOCK = threading.Lock()


def database_url() -> str | None:
    """Read the managed PostgreSQL URL from environment or Streamlit Secrets."""
    value = os.getenv("DATABASE_URL", "").strip()
    if value:
        return value
    try:
        import streamlit as st
        value = str(st.secrets.get("DATABASE_URL", "")).strip()
        return value or None
    except Exception:
        return None


def get_postgres_pool(dsn: str):
    """Return a bounded psycopg pool shared by authentication requests."""
    with _POOLS_LOCK:
        pool = _POOLS.get(dsn)
        if pool is None:
            try:
                from psycopg.rows import dict_row
                from psycopg_pool import ConnectionPool
            except ImportError as exc:
                raise RuntimeError("PostgreSQL authentication requires psycopg[binary,pool].") from exc
            pool = ConnectionPool(
                conninfo=dsn,
                min_size=1,
                max_size=5,
                kwargs={"row_factory": dict_row, "connect_timeout": 15},
                open=True,
            )
            _POOLS[dsn] = pool
        return pool
