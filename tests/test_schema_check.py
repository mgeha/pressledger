"""The schema compatibility check that runs before the database is used."""

import sqlite3
import unittest

from pressledger.db import (
    SCHEMA_VERSION,
    SchemaMismatch,
    ensure_schema,
    init_db,
)


class SchemaCheckTests(unittest.TestCase):
    def _connection(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn

    def test_an_empty_database_is_initialized_as_version_one(self):
        conn = self._connection()

        ensure_schema(conn)

        version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
        self.assertEqual(int(version), SCHEMA_VERSION)
        self.assertEqual(SCHEMA_VERSION, 1)

    def test_an_incompatible_schema_version_is_refused(self):
        conn = self._connection()
        init_db(conn)
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION + 1),),
        )

        with self.assertRaisesRegex(SchemaMismatch, "reimport --rebuild"):
            ensure_schema(conn)


if __name__ == "__main__":
    unittest.main()
