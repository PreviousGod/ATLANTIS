from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager


class ConnectionPool:
    """Thread-aware SQLite connection pool.

    Contract:
    - ``get_connection()`` returns the same connection to the same thread (via
      thread-local storage) until the thread either calls
      ``release_connection()`` or the pool is torn down.
    - ``release_connection(conn)`` returns the connection to the shared idle
      pool for reuse by another thread **and** clears the thread-local slot
      so the next ``get_connection()`` call on the same thread gets a fresh
      (or a different pooled) connection.
    - ``close_all()`` closes both idle-pooled connections and every connection
      currently checked out, so gateway shutdown does not leak open SQLite
      handles.
    """

    def __init__(self, db_path: str, max_connections: int = 10):
        self.db_path = db_path
        self.max_connections = max_connections
        self._local = threading.local()
        self._pool: list[sqlite3.Connection] = []
        # Tracks every connection currently outside the idle pool so
        # close_all can force-close them at shutdown.
        self._active: set[sqlite3.Connection] = set()
        self._lock = threading.Lock()
        self._closed = False

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def get_connection(self) -> sqlite3.Connection:
        """Return a connection for the current thread (thread-local)."""
        existing = getattr(self._local, 'conn', None)
        if existing is not None:
            return existing

        with self._lock:
            if self._closed:
                raise RuntimeError("ConnectionPool is closed")
            if self._pool:
                conn = self._pool.pop()
            else:
                conn = self._new_connection()
            self._active.add(conn)

        self._local.conn = conn
        return conn

    def release_connection(self, conn: sqlite3.Connection) -> None:
        """Return ``conn`` to the idle pool and clear the thread-local slot.

        Safe to call from any thread; if ``conn`` was this thread's cached
        connection, the cache is cleared so subsequent ``get_connection()``
        calls allocate a fresh one instead of reusing the (now pooled) handle.
        """
        if conn is None:
            return
        # Clear thread-local if it matches the released connection so this
        # thread doesn't keep receiving a now-pooled handle.
        if getattr(self._local, 'conn', None) is conn:
            self._local.conn = None

        with self._lock:
            self._active.discard(conn)
            if self._closed:
                try:
                    conn.close()
                except Exception:
                    pass
                return
            if len(self._pool) < self.max_connections:
                self._pool.append(conn)
            else:
                try:
                    conn.close()
                except Exception:
                    pass

    @contextmanager
    def connection(self):
        """Context manager — yields a connection and releases it on exit."""
        conn = self.get_connection()
        try:
            yield conn
        finally:
            self.release_connection(conn)

    def close_all(self) -> None:
        """Close every known connection (pooled + checked out).

        Intended to be called once at provider/gateway teardown. After this,
        ``get_connection()`` raises ``RuntimeError``.
        """
        with self._lock:
            self._closed = True
            for conn in self._pool:
                try:
                    conn.close()
                except Exception:
                    pass
            self._pool.clear()
            for conn in list(self._active):
                try:
                    conn.close()
                except Exception:
                    pass
            self._active.clear()
        # Also clear any thread-local cache on the calling thread so a
        # subsequent get_connection on the same thread does not hand back
        # the now-closed handle.
        if hasattr(self._local, 'conn'):
            self._local.conn = None


__all__ = ["ConnectionPool"]
