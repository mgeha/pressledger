import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pressledger.db import SCHEMA_SQL, get_conn
from pressledger.sync import (
    InvalidAccountingFile,
    _archive,
    _fetch_listing,
    _import_date,
    _is_safe_filename,
    _plan_action,
    machine_raw_dir,
    reimport_from_raw,
    sync,
    sync_all,
)
from tests.support import MACHINE, SECOND_MACHINE, configured

HEADER = "4302;jobid;jobname\n"
ROW = "4303;1;123456_job\n"


class ImportValidationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)

    def tearDown(self):
        self.conn.close()

    def _import(self, content: str, machine_id: str = MACHINE.id) -> int:
        return _import_date(
            self.conn,
            machine_id,
            "2026-08-11",
            "ACL",
            "machine20260811.ACL",
            content.encode(),
        )

    def _rows(self, machine_id: str = MACHINE.id) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE machine_id = ? AND source_date = '2026-08-11'",
            (machine_id,),
        ).fetchone()["n"]

    def test_empty_snapshot_is_allowed_for_an_empty_day(self):
        # A day without jobs is served as a header-only file — documented as
        # normal, so this must stay importable.
        self.assertEqual(self._import(HEADER), 0)

    def test_empty_snapshot_does_not_replace_existing_rows(self):
        self._import(HEADER + ROW)

        with self.assertRaisesRegex(InvalidAccountingFile, "0 row.*already has 1"):
            self._import(HEADER)

        self.assertEqual(self._rows(), 1)

    def test_shorter_snapshot_does_not_replace_existing_rows(self):
        # A valid header and some, but not all, records. The ACL only grows.
        self._import(HEADER + ROW + "4303;2;123457_job\n" + "4303;3;123458_job\n")

        with self.assertRaisesRegex(InvalidAccountingFile, "1 row.*already has 3"):
            self._import(HEADER + ROW)

        self.assertEqual(self._rows(), 3)

    def test_equal_row_count_is_accepted(self):
        # Finalising a day whose ACL was already complete — not a shrink.
        self._import(HEADER + ROW)
        count = _import_date(
            self.conn,
            MACHINE.id,
            "2026-08-11",
            "CSV",
            "machine20260811.CSV",
            (HEADER + ROW).encode(),
        )
        self.assertEqual(count, 1)
        self.assertEqual(self._rows(), 1)

    def test_growing_snapshot_is_accepted(self):
        self._import(HEADER + ROW)
        self.assertEqual(self._import(HEADER + ROW + "4303;2;123457_job\n"), 2)

    def test_content_without_a_canon_header_is_rejected(self):
        with self.assertRaisesRegex(InvalidAccountingFile, "4302 header"):
            self._import("<html>oops</html>\n")

    def test_snapshot_cut_off_mid_record_is_rejected(self):
        # No trailing newline: a body cut short *without a Content-Length*
        # raises nothing in requests, so this is the only signal left.
        with self.assertRaisesRegex(InvalidAccountingFile, "truncated"):
            self._import(HEADER + ROW + "4303;2;1234")

        self.assertEqual(self._rows(), 0)

    def test_archive_replacement_leaves_no_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            raw_dir = Path(directory)
            target = raw_dir / "machine20260811.ACL"
            target.write_bytes(b"old")

            _archive(raw_dir, target.name, b"new")

            self.assertEqual(target.read_bytes(), b"new")
            self.assertFalse(target.with_suffix(".ACL.part").exists())


class PlanActionTests(unittest.TestCase):
    """Which files of the listing are imported, and which are already settled."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)

    def tearDown(self):
        self.conn.close()

    def _plan(self, ext: str) -> str | None:
        return _plan_action(self.conn, MACHINE.id, "2026-08-11", ext)

    def _source_type(self) -> str:
        return self.conn.execute("SELECT source_type FROM sync_log").fetchone()["source_type"]

    def test_an_unknown_day_is_imported(self):
        self.assertEqual(self._plan("ACL"), "IMPORT")

    def test_the_running_day_is_refreshed_and_then_finalised(self):
        _import_date(self.conn, MACHINE.id, "2026-08-11", "ACL", "m20260811.ACL", HEADER.encode())
        self.assertEqual(self._plan("ACL"), "UPDATE")
        self.assertEqual(self._plan("CSV"), "FINALIZE")

    def test_a_finalised_day_is_never_reopened_by_its_own_acl(self):
        """The press may still offer the ACL of a day it has already rotated.

        Importing it would replace the final rows and set source_type back to
        ACL, so the day would read as not final again.
        """
        _import_date(
            self.conn, MACHINE.id, "2026-08-11", "CSV", "m20260811.CSV", (HEADER + ROW).encode()
        )
        self.assertIsNone(self._plan("CSV"))
        self.assertIsNone(self._plan("ACL"))
        self.assertEqual(self._source_type(), "CSV")


class ArchiveFilenameSafetyTests(unittest.TestCase):
    """ "D:outside...CSV" has no '/' or '\\' yet escapes raw_dir on Windows."""

    UNSAFE_NAMES = (
        "",
        ".",
        "..",
        "../escape20260916.CSV",
        "..\\escape20260916.CSV",
        "D:outside20260916.CSV",
        "C:\\Windows\\evil20260916.CSV",
        "/etc/passwd20260916.CSV",
        "\\\\server\\share\\evil20260916.CSV",
        "machine20260916.CSV:stream",
        "no-extension20260916",
        "machine20260916.CSV\x00.ACL",
        "99900011120260916.CSV\n",
    )

    def test_unsafe_names_are_rejected(self):
        for name in self.UNSAFE_NAMES:
            with self.subTest(name=name):
                self.assertFalse(_is_safe_filename(name))

    def test_ordinary_canon_names_are_accepted(self):
        for name in ("99900011120260916.CSV", "99900011120260916.acl"):
            with self.subTest(name=name):
                self.assertTrue(_is_safe_filename(name))

    def test_archive_refuses_unsafe_names_and_writes_nothing(self):
        for name in self.UNSAFE_NAMES:
            if name in ("", ".", ".."):
                continue  # not a path _archive would even be called with
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                raw_dir = Path(directory)
                with self.assertRaisesRegex(InvalidAccountingFile, "unsafe"):
                    _archive(raw_dir, name, b"data")
                self.assertEqual(list(raw_dir.iterdir()), [])

    def test_listing_drops_unsafe_hrefs(self):
        html = (
            '<a href="/accounting/99900011120260916.CSV">a</a>'
            '<a href="/accounting/D:outside20260916.CSV">b</a>'
            '<a href="/accounting/..\\escape20260916.CSV">c</a>'
        )
        fake_resp = mock.Mock(text=html)
        fake_resp.raise_for_status = mock.Mock()
        with configured(), mock.patch("pressledger.sync.requests.get", return_value=fake_resp):
            listing = _fetch_listing("http://printer")

        self.assertEqual([name for _, name in listing], ["99900011120260916.CSV"])


class TwoMachineTests(unittest.TestCase):
    """jobid is machine-local: the same day and the same jobid on two presses is
    the normal case, not a conflict."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)
        self.addCleanup(self.conn.close)

    def _import(self, machine_id: str, content: str) -> int:
        return _import_date(
            self.conn,
            machine_id,
            "2026-08-11",
            "ACL",
            "x20260811.ACL",
            content.encode(),
        )

    def test_same_day_and_jobid_on_two_machines_coexist(self):
        self._import(MACHINE.id, HEADER + ROW)
        self._import(SECOND_MACHINE.id, HEADER + ROW)

        rows = self.conn.execute(
            "SELECT machine_id, jobid FROM jobs ORDER BY machine_id"
        ).fetchall()
        self.assertEqual(
            [(r["machine_id"], r["jobid"]) for r in rows], [(MACHINE.id, 1), (SECOND_MACHINE.id, 1)]
        )

    def test_a_refresh_of_one_machine_leaves_the_other_alone(self):
        """The ACL day is re-read from scratch — DELETE + reinsert. Scoped to the
        machine, or one press would wipe the other's day."""
        self._import(MACHINE.id, HEADER + ROW + "4303;2;123457_job\n")
        self._import(SECOND_MACHINE.id, HEADER + ROW)

        # Same content again for machine one: its rows are replaced.
        self._import(MACHINE.id, HEADER + ROW + "4303;2;123457_job\n")

        counts = dict(
            self.conn.execute(
                "SELECT machine_id, COUNT(*) FROM jobs GROUP BY machine_id"
            ).fetchall()
        )
        self.assertEqual(counts, {MACHINE.id: 2, SECOND_MACHINE.id: 1})

    def test_sync_log_holds_one_row_per_machine_and_day(self):
        self._import(MACHINE.id, HEADER + ROW)
        self._import(SECOND_MACHINE.id, HEADER + ROW)

        days = self.conn.execute(
            "SELECT machine_id, source_date FROM sync_log ORDER BY machine_id"
        ).fetchall()
        self.assertEqual(
            [tuple(r) for r in days],
            [(MACHINE.id, "2026-08-11"), (SECOND_MACHINE.id, "2026-08-11")],
        )


class FakeResponse:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        pass


class SyncArchiveTests(unittest.TestCase):
    """A partial download changes neither the archive nor the database."""

    def test_partial_download_leaves_archive_and_database_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            raw_dir = Path(directory)
            good = HEADER + ROW + "4303;2;123457_job\n"
            machine_dir = machine_raw_dir(MACHINE.id, raw_dir)
            machine_dir.mkdir(parents=True)
            archived = machine_dir / "99900011120260810.CSV"
            archived.write_bytes(good.encode())

            with configured(db_path=raw_dir / "scratch.sqlite", raw_dir=raw_dir):
                reimport_from_raw(rebuild=True, raw_dir=raw_dir)

                with (
                    mock.patch(
                        "pressledger.sync._fetch_listing",
                        return_value=[("http://printer/x", archived.name)],
                    ),
                    mock.patch(
                        "pressledger.sync.requests.get",
                        return_value=FakeResponse((HEADER + ROW).encode()),
                    ),
                ):
                    # A finalised CSV is normally skipped, so make the day look
                    # like the running ACL to force the refresh path.
                    conn = get_conn()
                    conn.execute(
                        "UPDATE sync_log SET source_type = 'ACL' WHERE source_date = '2026-08-10'"
                    )
                    conn.commit()
                    conn.close()

                    result = sync(MACHINE, raw_dir=raw_dir)

                conn = get_conn()
                rows = conn.execute(
                    "SELECT COUNT(*) AS n FROM jobs WHERE source_date = '2026-08-10'"
                ).fetchone()["n"]
                conn.close()

            self.assertEqual(result.files_imported, 0)
            self.assertIn("already has 2", result.error)
            self.assertEqual(archived.read_bytes(), good.encode())
            self.assertEqual(rows, 2)

    def test_sync_all_records_one_run_per_machine(self):
        """An unreachable press must not keep the others from being attempted."""
        with tempfile.TemporaryDirectory() as directory:
            raw_dir = Path(directory)
            with configured(
                db_path=raw_dir / "scratch.sqlite",
                raw_dir=raw_dir,
                machines=(MACHINE, SECOND_MACHINE),
            ):
                with mock.patch(
                    "pressledger.sync._fetch_listing", side_effect=OSError("no connection")
                ):
                    results = sync_all(raw_dir=raw_dir)

                conn = get_conn()
                runs = conn.execute(
                    "SELECT machine_id, ok FROM sync_run ORDER BY machine_id"
                ).fetchall()
                conn.close()

            self.assertEqual([r.machine_id for r in results], [MACHINE.id, SECOND_MACHINE.id])
            self.assertEqual(
                [(r["machine_id"], r["ok"]) for r in runs],
                [(MACHINE.id, 0), (SECOND_MACHINE.id, 0)],
            )


class ReimportTests(unittest.TestCase):
    """An unusable archived file must not abort a rebuild part way through."""

    def test_invalid_file_is_skipped_and_the_rest_imported(self):
        with tempfile.TemporaryDirectory() as directory:
            raw_dir = Path(directory)
            machine_dir = machine_raw_dir(MACHINE.id, raw_dir)
            machine_dir.mkdir(parents=True)
            (machine_dir / "99900011120260810.CSV").write_bytes((HEADER + ROW).encode())
            (machine_dir / "99900011120260811.CSV").write_bytes(
                (HEADER + ROW + "4303;2;1234").encode()  # truncated
            )
            (machine_dir / "99900011120260812.CSV.part").write_bytes(b"leftover")

            with configured(db_path=raw_dir / "scratch.sqlite", raw_dir=raw_dir):
                result = reimport_from_raw(rebuild=True, raw_dir=raw_dir)

            # The .part is not counted as a file to import.
            self.assertEqual(result.files_seen, 2)
            self.assertEqual(result.files_imported, 1)
            self.assertEqual(result.rows_imported, 1)
            self.assertIn("truncated", result.error)

    def test_files_lying_in_the_archive_root_abort_the_rebuild(self):
        """Importing nothing would leave an empty database and exit 0, so the
        missing move is named instead."""
        with tempfile.TemporaryDirectory() as directory:
            raw_dir = Path(directory)
            (raw_dir / "99900011120260810.CSV").write_bytes((HEADER + ROW).encode())

            with (
                configured(db_path=raw_dir / "scratch.sqlite", raw_dir=raw_dir),
                self.assertRaisesRegex(InvalidAccountingFile, "mv "),
            ):
                reimport_from_raw(rebuild=True, raw_dir=raw_dir)

    def test_a_machine_without_an_archive_directory_is_not_an_error(self):
        """A press that has not synced yet has nothing to import."""
        with tempfile.TemporaryDirectory() as directory:
            raw_dir = Path(directory)
            machine_dir = machine_raw_dir(MACHINE.id, raw_dir)
            machine_dir.mkdir(parents=True)
            (machine_dir / "99900011120260810.CSV").write_bytes((HEADER + ROW).encode())

            with configured(
                db_path=raw_dir / "scratch.sqlite",
                raw_dir=raw_dir,
                machines=(MACHINE, SECOND_MACHINE),
            ):
                result = reimport_from_raw(rebuild=True, raw_dir=raw_dir)

            self.assertTrue(result.ok)
            self.assertEqual(result.files_imported, 1)


if __name__ == "__main__":
    unittest.main()
