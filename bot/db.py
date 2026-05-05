import os
from contextlib import contextmanager
from typing import Generator

import psycopg
from psycopg_pool import ConnectionPool
try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependency is declared in requirements.txt
    def load_dotenv(*args: object, **kwargs: object) -> bool:
        return False

_pool: ConnectionPool | None = None


def _database_url() -> str | None:
    load_dotenv()
    return os.environ.get("DATABASE_URL")


def init_pool(min_size: int = 1, max_size: int = 5, timeout: float = 10.0) -> None:
    global _pool
    if _pool is not None:
        return  # idempotent — already initialized
    url = _database_url()
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    _pool = ConnectionPool(
        url,
        min_size=min_size,
        max_size=max_size,
        open=True,
        kwargs={"connect_timeout": int(timeout)},
    )


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def get_db() -> Generator[psycopg.Connection, None, None]:
    if _pool is None:
        raise RuntimeError("DB pool not initialized — call init_pool() first")
    with _pool.connection() as conn:
        yield conn


def check_db_available() -> tuple[bool, str | None]:
    try:
        url = _database_url()
        if not url:
            return False, "DATABASE_URL not set"
        with psycopg.connect(url, connect_timeout=5) as conn:
            conn.execute("SELECT 1")
        return True, None
    except Exception as exc:
        return False, str(exc)
