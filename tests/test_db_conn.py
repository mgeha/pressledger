"""The connection get_conn() hands out.

FastAPI may run a synchronous dependency and its route in different threadpool
workers, which only shows under concurrent requests.
"""

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from pressledger.db import ensure_schema, get_conn
from tests.support import configured


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db_path = Path(directory.name) / "pressledger.sqlite"
        self.enterContext(configured(db_path=self.db_path))

    def _in_another_thread(self, call):
        out = []
        thread = threading.Thread(target=lambda: out.append(call()))
        thread.start()
        thread.join()
        return out[0]

    def test_a_connection_is_usable_from_another_thread(self):
        conn = get_conn()
        self.addCleanup(conn.close)
        ensure_schema(conn)

        result = self._in_another_thread(
            lambda: conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        )

        self.assertEqual(result, 0)

    def test_closing_from_another_thread_is_allowed(self):
        """The teardown half of the same dependency, which may run elsewhere again."""
        conn = get_conn()
        ensure_schema(conn)

        self.assertIsNone(self._in_another_thread(conn.close))

    def test_the_registered_function_and_the_row_factory_survive(self):
        conn = get_conn()
        self.addCleanup(conn.close)

        row = self._in_another_thread(
            lambda: conn.execute("SELECT job_key('105774_Customer.pdf') AS key").fetchone()
        )

        self.assertIsInstance(row, sqlite3.Row)
        # No pattern configured, so the key is None — it resolves, which is the test.
        self.assertIsNone(row["key"])


if __name__ == "__main__":
    unittest.main()
