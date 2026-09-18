"""The grouping rule — which print runs are folded into one job.

Both halves are tested: the Python derivation and the expression that reaches
SQLite. The unconfigured case is the default and must need no second code path.
"""

import sqlite3
import unittest

from pressledger import config, jobkey
from tests.support import configured

ORDER_NO = r"^(\d{6})_"


def _with(pattern):
    """jobkey with `pattern` in the active configuration."""
    return configured(job_key_pattern=pattern)


class PatternTests(unittest.TestCase):
    def test_the_configured_group_is_the_key(self):
        with _with(ORDER_NO):
            self.assertEqual(jobkey.job_key("105774_Customer_Cover.pdf"), "105774")

    def test_a_filename_that_does_not_match_has_no_key(self):
        with _with(ORDER_NO):
            self.assertIsNone(jobkey.job_key("Invoice.pdf"))
            self.assertIsNone(jobkey.job_key("12345_Too_short.pdf"))
            self.assertIsNone(jobkey.job_key(""))
            self.assertIsNone(jobkey.job_key(None))

    def test_without_a_pattern_nothing_has_a_key(self):
        with _with(""):
            self.assertIsNone(jobkey.job_key("105774_Customer_Cover.pdf"))
            self.assertFalse(jobkey.grouping_enabled())

    def test_a_pattern_without_a_group_yields_the_whole_match(self):
        """Parentheses are easy to forget; returning the match beats an
        IndexError on every row."""
        with _with(r"^\d{6}"):
            self.assertEqual(jobkey.job_key("105774_Customer.pdf"), "105774")

    def test_an_invalid_pattern_is_a_configuration_error(self):
        """Set but broken must not degrade to "no grouping", which is a valid
        configuration of its own. The start stops and the message names the key.
        """
        with self.assertRaises(config.ConfigError) as ctx:
            config._job_key_re("^(\\d{6}")
        self.assertIn("jobs.key_pattern", str(ctx.exception))

    def test_no_pattern_is_not_an_error(self):
        """Only a set value is validated — unset is the documented default."""
        self.assertIsNone(config._job_key_re(""))

    def test_the_display_pattern_may_be_as_loose_as_it_likes(self):
        """A captured key need not be numeric — only the regex syntax is checked."""
        with _with(r"^(\w+?)_"):
            self.assertEqual(jobkey.job_key("105774_Customer.pdf"), "105774")
            self.assertEqual(jobkey.job_key("Invoice_May.pdf"), "Invoice")


class KeySqlTests(unittest.TestCase):
    """key_sql() has to be valid SQL in both configurations, and the
    unconfigured one must not need the registered function at all."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE jobs (machine_id TEXT, source_date TEXT, jobid INTEGER,"
            " line_seq INTEGER, jobname TEXT)"
        )
        self.conn.execute(
            "INSERT INTO jobs VALUES"
            " ('v1000-01', '2026-08-14', 1204, 7, '105774_Customer_Cover.pdf')"
        )
        self.addCleanup(self.conn.close)

    def _key(self, alias=""):
        table = f"jobs {alias}" if alias else "jobs"
        return self.conn.execute(f"SELECT {jobkey.key_sql(alias)} FROM {table}").fetchone()[0]

    def test_with_a_pattern_the_registered_function_is_used(self):
        self.conn.create_function("job_key", 1, jobkey.job_key, deterministic=True)
        with _with(ORDER_NO):
            self.assertEqual(self._key(), "105774")
            self.assertEqual(self._key("j"), "105774")

    def test_without_a_pattern_the_row_identity_is_the_key(self):
        """Every run becomes its own group. No function is registered on this
        connection: the fallback has to be plain SQL. machine_id leads the
        identity, because the same jobid recurs on another press.
        """
        with _with(""):
            self.assertEqual(self._key(), "v1000-01.2026-08-14.1204.7")
            self.assertEqual(self._key("j"), "v1000-01.2026-08-14.1204.7")

    def test_the_alias_qualifies_every_column(self):
        """Where jobs is joined against job_media, all four key columns exist in
        both and are ambiguous unqualified."""
        with _with(""):
            sql = jobkey.key_sql("j")
        for column in ("machine_id", "source_date", "jobid", "line_seq"):
            self.assertIn(f"j.{column}", sql)


if __name__ == "__main__":
    unittest.main()
