import time
import unittest
from tests import bootstrap  # noqa: F401

from auto_test.platform.mysql_store import (
    MySQLConnection,
    _ConnectionPool,
    _ensure_column,
    _ensure_index,
    _mysql_sql,
)


def _normalize(sql: str) -> str:
    return " ".join(sql.split())


class FakeCursor:
    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, args=()):
        self.raw.executed.append((sql, args))
        if self.raw.fail_codes:
            code = self.raw.fail_codes.pop(0)
            if code:
                error = RuntimeError("mysql error")
                error.args = (code, "mysql server error")
                raise error

    def fetchall(self):
        return list(self.raw.results.get(_normalize(self.raw.executed[-1][0]), []))

    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None


class FakeRaw:
    def __init__(self):
        self.executed = []
        self.fail_codes = []
        self.results = {}
        self.reconnects = 0
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def ping(self, reconnect=True):
        self.reconnects += 1

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.closed = True


class PoolTests(unittest.TestCase):
    def _factory(self):
        def create():
            self.created.append(MySQLConnection(FakeRaw()))
            return self.created[-1]

        self.created = []
        return create

    def test_released_connection_is_reused(self):
        pool = _ConnectionPool(self._factory(), max_connections=2)
        first = pool.acquire()
        pool.release(first)

        second = pool.acquire()

        self.assertIs(first, second)
        pool.release(second)
        pool.close()
        self.assertTrue(first.raw.closed)

    def test_pool_exhaustion_fails_fast_instead_of_hanging(self):
        pool = _ConnectionPool(self._factory(), max_connections=2, acquire_timeout=0.05)
        pool.acquire()
        pool.acquire()

        start = time.monotonic()
        with self.assertRaises(RuntimeError):
            pool.acquire()
        self.assertLess(time.monotonic() - start, 2.0)
        pool.close()

    def test_broken_connection_is_discarded_and_replaced(self):
        pool = _ConnectionPool(self._factory(), max_connections=2)
        first = pool.acquire()
        first.broken = True
        pool.release(first)

        second = pool.acquire()

        self.assertIsNot(first, second)
        self.assertTrue(first.raw.closed)
        pool.close()

    def test_factory_failure_releases_pool_slot(self):
        calls = {"count": 0}

        def flaky_factory():
            calls["count"] += 1
            if calls["count"] == 1:
                raise ConnectionError("unreachable")
            return MySQLConnection(FakeRaw())

        pool = _ConnectionPool(flaky_factory, max_connections=1)

        with self.assertRaises(ConnectionError):
            pool.acquire()

        connection = pool.acquire()
        self.assertIsNotNone(connection)
        pool.close()


class MySQLConnectionRetryTests(unittest.TestCase):
    def test_lost_connection_is_reconnected_and_retried_once(self):
        raw = FakeRaw()
        raw.fail_codes = [2006]
        connection = MySQLConnection(raw)

        cursor = connection.execute("SELECT 1", ())

        self.assertEqual(raw.reconnects, 1)
        self.assertEqual(len(raw.executed), 2)
        self.assertEqual(cursor.raw, raw)

    def test_non_connection_errors_are_not_retried(self):
        raw = FakeRaw()
        raw.fail_codes = [1064]
        connection = MySQLConnection(raw)

        with self.assertRaises(RuntimeError):
            connection.execute("SELECT *", ())

        self.assertEqual(raw.reconnects, 0)
        self.assertEqual(len(raw.executed), 1)


class SchemaMigrationHelperTests(unittest.TestCase):
    def test_ensure_column_adds_only_when_missing(self):
        raw = FakeRaw()
        raw.results["SHOW COLUMNS FROM runs"] = [{"Field": "id"}]
        connection = MySQLConnection(raw)

        _ensure_column(connection, "runs", "stale_marked_at", "stale_marked_at DOUBLE NULL")

        statements = [sql for sql, _args in raw.executed]
        self.assertIn("ALTER TABLE runs ADD COLUMN stale_marked_at DOUBLE NULL", statements)

        raw.executed.clear()
        raw.results["SHOW COLUMNS FROM runs"] = [{"Field": "id"}, {"Field": "stale_marked_at"}]
        _ensure_column(connection, "runs", "stale_marked_at", "stale_marked_at DOUBLE NULL")
        statements = [sql for sql, _args in raw.executed]
        self.assertFalse(any(sql.startswith("ALTER TABLE") for sql in statements))

    def test_ensure_index_creates_only_when_missing(self):
        raw = FakeRaw()
        raw.results = {}
        connection = MySQLConnection(raw)

        _ensure_index(connection, "run_events", "idx_events_run_type_id", "(run_id, event_type, id)")

        statements = [sql for sql, _args in raw.executed]
        self.assertTrue(
            any(
                sql.startswith("CREATE INDEX idx_events_run_type_id")
                for sql in statements
            )
        )

        raw.executed.clear()
        raw.results = {
            _normalize(
                _mysql_sql(
                    """
                    SELECT 1 FROM information_schema.statistics
                    WHERE table_schema = DATABASE() AND table_name = ? AND index_name = ?
                    """
                )
            ): [{"1": 1}]
        }
        _ensure_index(connection, "run_events", "idx_events_run_type_id", "(run_id, event_type, id)")
        statements = [sql for sql, _args in raw.executed]
        self.assertFalse(any(sql.startswith("CREATE INDEX") for sql in statements))


if __name__ == "__main__":
    unittest.main()
