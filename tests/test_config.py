"""Reading pressledger.toml.

Two things are tested: that a mistake in the file stops the start instead of
being ignored, and that a second load() reaches every reader — which is what
routing them all through config.get() is for.
"""

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from pressledger import config, db, jobkey

MINIMAL = """
[[machine]]
id  = "v1000-01"
url = "http://printer.test"
"""

FULL = """
[server]
host = "127.0.0.1"
port = 8123

[paths]
db  = "scratch.sqlite"
raw = "archive"

[sync]
interval_min = 5
http_timeout = 3

[ui]
site_name  = "Example GmbH"
custom_css = "brand.css"
lang       = "de"

[jobs]
key_pattern = '^(\\d{6})_'

[[machine]]
id   = "v1000-01"
name = "imagePRESS V1000"
url  = "http://printer.test/"

[[machine]]
id   = "v1000-02"
url  = "http://printer2.test"
"""


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        # resolve(): load() stores the resolved path, and on macOS /var is a
        # symlink to /private/var — the paths would differ only by that.
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.addCleanup(config.reset)

    def write(self, text: str, name: str = "pressledger.toml") -> Path:
        path = self.dir / name
        path.write_text(textwrap.dedent(text))
        return path

    def load(self, text: str) -> config.Settings:
        return config.load(self.write(text))


class ReadingTests(ConfigTestCase):
    def test_defaults_apply_to_everything_but_the_machine(self):
        settings = self.load(MINIMAL)

        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(settings.port, 8000)
        self.assertEqual(settings.sync_interval_min, 30)
        self.assertEqual(settings.http_timeout, 10)
        self.assertEqual(settings.site_name, "")
        self.assertIsNone(settings.custom_css)
        self.assertEqual(settings.default_lang, "en")
        self.assertIsNone(settings.job_key_re)
        # No name given: the id stands in, so nothing renders as empty.
        self.assertEqual(settings.machines[0].name, "v1000-01")

    def test_every_value_arrives(self):
        settings = self.load(FULL)

        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(settings.port, 8123)
        self.assertEqual(settings.sync_interval_min, 5)
        self.assertEqual(settings.http_timeout, 3)
        self.assertEqual(settings.site_name, "Example GmbH")
        self.assertEqual(settings.default_lang, "de")
        self.assertEqual([m.id for m in settings.machines], ["v1000-01", "v1000-02"])
        self.assertTrue(settings.multi_machine)

    def test_a_relative_path_is_relative_to_the_file(self):
        """So a service started elsewhere does not write its database into the
        working directory."""
        settings = self.load(FULL)

        self.assertEqual(settings.db_path, self.dir / "scratch.sqlite")
        self.assertEqual(settings.raw_dir, self.dir / "archive")
        self.assertEqual(settings.custom_css, self.dir / "brand.css")

    def test_a_trailing_slash_on_the_url_is_dropped(self):
        """Every request appends /accounting/ — two slashes would be a 404."""
        self.assertEqual(self.load(FULL).machines[0].url, "http://printer.test")

    def test_the_key_pattern_is_compiled_while_reading(self):
        """So a broken rule fails at load time, not on the first query."""
        settings = self.load(FULL)
        self.assertIsNotNone(settings.job_key_re)
        self.assertEqual(settings.job_key_re.search("105774_Customer.pdf").group(1), "105774")


class RefusalTests(ConfigTestCase):
    """Reject missing files, invalid syntax and unsupported configuration values."""

    def test_a_missing_file_names_the_example(self):
        with self.assertRaisesRegex(config.ConfigError, "pressledger.toml.example"):
            config.load(self.dir / "absent.toml")

    def test_broken_toml_is_reported_as_such(self):
        with self.assertRaisesRegex(config.ConfigError, "not valid TOML"):
            self.load("[[machine]\nid = 'x'\n")

    def test_no_machine_at_all(self):
        with self.assertRaisesRegex(config.ConfigError, "No machine configured"):
            self.load("[server]\nport = 8000\n")

    def test_an_unknown_section(self):
        with self.assertRaisesRegex(config.ConfigError, "Unknown section"):
            self.load(MINIMAL + "\n[printer]\nurl = 'http://x'\n")

    def test_an_unknown_key_in_a_known_section(self):
        """A misspelled key must name itself, not leave the default in place."""
        with self.assertRaisesRegex(config.ConfigError, "interval_minutes"):
            self.load(MINIMAL + "\n[sync]\ninterval_minutes = 5\n")

    def test_an_unknown_key_in_a_machine(self):
        with self.assertRaisesRegex(config.ConfigError, "adress"):
            self.load("[[machine]]\nid='a'\nurl='http://x'\nadress='y'\n")

    def test_a_wrong_type(self):
        with self.assertRaisesRegex(config.ConfigError, "server.port must be int"):
            self.load(MINIMAL + "\n[server]\nport = '8000'\n")

    def test_a_boolean_where_a_number_belongs(self):
        """bool is a subclass of int, so this needs its own guard."""
        with self.assertRaisesRegex(config.ConfigError, "must be int"):
            self.load(MINIMAL + "\n[sync]\ninterval_min = true\n")

    def test_a_duplicate_machine_id(self):
        with self.assertRaisesRegex(config.ConfigError, "Duplicate machine id"):
            self.load("""
                [[machine]]
                id  = "v1000-01"
                url = "http://a.test"

                [[machine]]
                id  = "v1000-01"
                url = "http://b.test"
                """)

    def test_an_id_that_is_not_safe_as_a_directory_name(self):
        """The id becomes a directory under data/raw/ and part of the primary key.

        No dot, which rules out "." and ".."; no colon, which separates a drive
        or a stream on Windows.
        """
        for bad in ("v1000 01", ".", "..", "../etc", "press.2", "c:v1000", "press/2", ""):
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                self.load(f'[[machine]]\nid = "{bad}"\nurl = "http://a.test"\n')

    def test_the_ids_that_are_allowed(self):
        for good in ("v1000-01", "press2", "IP_V1000", "a"):
            with self.subTest(good=good):
                settings = config.load(
                    self.write(f'[[machine]]\nid = "{good}"\nurl = "http://a.test"\n')
                )
                self.assertEqual(settings.machines[0].id, good)

    def test_a_machine_without_a_url(self):
        with self.assertRaisesRegex(config.ConfigError, "has no url"):
            self.load('[[machine]]\nid = "v1000-01"\n')

    def test_an_unsupported_language(self):
        with self.assertRaisesRegex(config.ConfigError, "ui.lang"):
            self.load(MINIMAL + "\n[ui]\nlang = 'fr'\n")

    def test_an_invalid_key_pattern(self):
        """No grouping is a legitimate configuration, so a broken pattern must
        not degrade into it."""
        with self.assertRaisesRegex(config.ConfigError, "jobs.key_pattern"):
            self.load(MINIMAL + "\n[jobs]\nkey_pattern = '^(\\\\d{6}'\n")

    def test_a_non_ascii_token(self):
        """The ellipsis placeholder from the documentation, copied unchanged."""
        with self.assertRaisesRegex(config.ConfigError, "api.token"):
            self.load(MINIMAL + '\n[api]\ntoken = "…"\n')


class NoStaleValuesTests(ConfigTestCase):
    """A second load() has to reach every reader, so nothing may bind a value at
    import time."""

    def test_loading_a_second_configuration_replaces_the_first(self):
        first = self.load(FULL)
        db.get_conn().close()
        # Check that get_conn() created the database at the configured path.
        self.assertTrue(first.db_path.exists())
        self.assertTrue(jobkey.grouping_enabled())
        self.assertEqual(jobkey.job_key("105774_Customer.pdf"), "105774")

        second = config.load(self.write(MINIMAL, "other.toml"))

        self.assertNotEqual(second.db_path, first.db_path)
        db.get_conn().close()
        self.assertTrue(second.db_path.exists())
        self.assertEqual(config.get().site_name, "")
        # The grouping rule too — it reaches the report SQL through key_sql().
        self.assertFalse(jobkey.grouping_enabled())
        self.assertIsNone(jobkey.job_key("105774_Customer.pdf"))
        self.assertIn("machine_id", jobkey.key_sql())

    def test_a_child_process_reads_the_configuration_it_is_given(self):
        """uvicorn's reload child imports the app itself, so cli passes the
        resolved path in the environment."""
        path = self.write(FULL)
        result = subprocess.run(
            [sys.executable, "-c", "from pressledger import config; print(config.get().db_path)"],
            capture_output=True,
            text=True,
            check=True,
            env={
                "PATH": "/usr/bin:/bin",
                config.CONFIG_ENV_VAR: str(path),
                "PYTHONPATH": str(Path(__file__).parent.parent),
            },
        )
        self.assertEqual(result.stdout.strip(), str(self.dir / "scratch.sqlite"))


if __name__ == "__main__":
    unittest.main()
