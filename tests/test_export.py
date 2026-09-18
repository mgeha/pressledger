"""The export API's query layer and its guard.

Three failure modes are pinned here, all of them silent:

  * a join over the row identity without machine_id, which fans the sheets out
    over every press (test_reports_machines.py covers the same class);
  * a page cursor that skips or repeats a row;
  * the guard letting a request through when no token is configured.

test_export_http.py drives the routes themselves.
"""

import sqlite3
import unittest

from fastapi import HTTPException

from pressledger import export
from pressledger.db import SCHEMA_SQL, register_functions
from pressledger.web.deps import authorize, bearer_token
from tests.support import MACHINE, SECOND_MACHINE, configured

JOB_SQL = """
    INSERT INTO jobs (
        machine_id, source_date, jobid, line_seq, jobtype, startdate, starttime,
        readydate, readytime, result, username, jobname, noffinishedsets,
        nofprinteda4bw, nofprinteda4c, nofprinteda3bw, nofprinteda3c,
        nofprintedXLbw, nofprintedXLc,
        nofbooklets, nofsinglestaples, nofdoublestaples, nofpunches,
        nofcreases, noffolds, raw_json
    ) VALUES (?, ?, ?, 0, 'IP', ?, '09:00:00', '', '09:10:00', 'Done',
              'operator', ?, 1, ?, 0, 0, 0, 0, 0,
              0, 0, 0, 0, 0, 0, '{"jobid": "1", "nofbinds": "3"}')
"""

MEDIA_SQL = """
    INSERT INTO job_media (
        machine_id, source_date, jobid, line_seq, tray,
        mediaformat, mediatype, mediaweight, mediacolor, medianame,
        nofsimplex, nofduplex, isinsert, istab
    ) VALUES (?, ?, ?, 0, 1, 'SRA3', 'Plain', 100, 'White', 'Test paper',
              ?, 0, '', '')
"""

LOG_SQL = """
    INSERT INTO sync_log (machine_id, source_date, source_type, filename,
                          last_synced, row_count)
    VALUES (?, ?, ?, ?, '2026-08-14T06:00:00Z', 1)
"""


class ExportTestCase(unittest.TestCase):
    def setUp(self):
        self.enterContext(configured(machines=(MACHINE, SECOND_MACHINE)))
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)
        register_functions(self.conn)
        self.addCleanup(self.conn.close)

        # A finalised day, the same jobid and line_seq on both presses — the
        # normal case, since both counters are machine-local.
        for machine in (MACHINE.id, SECOND_MACHINE.id):
            self.conn.execute(
                JOB_SQL, (machine, "2026-08-11", 1, "2026-08-11", "105774_Customer.pdf", 10)
            )
            self.conn.execute(MEDIA_SQL, (machine, "2026-08-11", 1, 10))
            self.conn.execute(LOG_SQL, (machine, "2026-08-11", "CSV", f"{machine}_2026-08-11.csv"))
        # A second run on the first press, same day.
        self.conn.execute(
            JOB_SQL, (MACHINE.id, "2026-08-11", 2, "2026-08-11", "105999_Other.pdf", 5)
        )
        self.conn.execute(MEDIA_SQL, (MACHINE.id, "2026-08-11", 2, 5))
        # The running day: ACL, not final.
        self.conn.execute(
            JOB_SQL, (MACHINE.id, "2026-08-12", 7, "2026-08-12", "106001_Today.pdf", 3)
        )
        self.conn.execute(LOG_SQL, (MACHINE.id, "2026-08-12", "ACL", f"{MACHINE.id}_today.acl"))
        self.conn.commit()


class RunPageTests(ExportTestCase):
    def test_one_row_per_print_run_and_no_grouping_key(self):
        page = export.run_page(self.conn, final_only=False)
        self.assertEqual(page["count"], 4)
        self.assertNotIn("job_key", page["runs"][0])
        # The four-part identity is complete on every row.
        for run in page["runs"]:
            for column in export.RUN_COLUMNS[:4]:
                self.assertIsNotNone(run[column])

    def test_the_row_carries_exactly_the_contract(self):
        """Spelled out, so a new field is a decision rather than a side effect
        of a schema change."""
        run = export.run_page(self.conn, final_only=False)["runs"][0]
        derived = {
            "source_file",
            "source_type",
            "final",
            "completed_date",
            "completed_at",
            "clicks",
            "sheets",
            "runtime_s",
            "media",
        }
        self.assertEqual(set(run), set(export.RUN_COLUMNS) | derived)

    def test_sheets_do_not_fan_out_over_the_presses(self):
        """The media subquery is the join over the row identity."""
        page = export.run_page(self.conn, final_only=False)
        truth = self.conn.execute(
            "SELECT SUM(nofsimplex + (nofduplex + 1) / 2) FROM job_media"
        ).fetchone()[0]
        self.assertEqual(sum(r["sheets"] for r in page["runs"]), truth)
        # And per row: 10 sheets on each press, not 20 on both.
        by_key = {(r["machine_id"], r["jobid"]): r for r in page["runs"]}
        self.assertEqual(by_key[(MACHINE.id, 1)]["sheets"], 10)
        self.assertEqual(by_key[(SECOND_MACHINE.id, 1)]["sheets"], 10)

    def test_media_rows_land_on_the_right_press(self):
        page = export.run_page(self.conn, final_only=False)
        for run in page["runs"]:
            with self.subTest(run=(run["machine_id"], run["jobid"])):
                # jobid 7 is the ACL row, which has no media at all.
                expected = 0 if run["jobid"] == 7 else 1
                self.assertEqual(len(run["media"]), expected)
        self.assertEqual(sum(m["sheets"] for r in page["runs"] for m in r["media"]), 25)

    def test_clicks_and_runtime_are_delivered(self):
        run = export.run_page(
            self.conn,
            machine=MACHINE.id,
            final_only=False,
            date_from="2026-08-11",
            date_to="2026-08-11",
        )["runs"][0]
        self.assertEqual(run["clicks"], 10)
        # 09:00:00 → 09:10:00, and readydate is empty on a same-day row.
        self.assertEqual(run["runtime_s"], 600)

    def test_final_only_hides_the_running_day(self):
        final = export.run_page(self.conn)
        self.assertEqual(final["count"], 3)
        self.assertTrue(all(r["final"] for r in final["runs"]))
        self.assertNotIn("2026-08-12", [r["source_date"] for r in final["runs"]])

        everything = export.run_page(self.conn, final_only=False)
        acl = [r for r in everything["runs"] if r["source_date"] == "2026-08-12"]
        self.assertEqual([r["source_type"] for r in acl], ["ACL"])
        self.assertEqual([r["final"] for r in acl], [False])

    def test_the_machine_filter_returns_that_press_only(self):
        page = export.run_page(self.conn, machine=SECOND_MACHINE.id, final_only=False)
        self.assertEqual({r["machine_id"] for r in page["runs"]}, {SECOND_MACHINE.id})

    def test_raw_is_opt_in_and_carries_the_uncolumned_fields(self):
        without = export.run_page(self.conn, final_only=False)["runs"][0]
        self.assertNotIn("raw", without)
        with_raw = export.run_page(self.conn, final_only=False, raw=True)["runs"][0]
        # nofbinds has no column, so raw is the only way to reach it.
        self.assertEqual(with_raw["raw"]["nofbinds"], "3")

    def test_media_can_be_switched_off(self):
        """Omitted, not empty: an empty list would read as "used no paper"."""
        page = export.run_page(self.conn, final_only=False, media=False)
        self.assertTrue(all("media" not in r for r in page["runs"]))


class PaginationTests(ExportTestCase):
    def _walk(self, **kwargs) -> list[tuple]:
        """Every row a client paging through would see, in order."""
        seen: list[tuple] = []
        cursor = None
        for _ in range(20):  # a bound, so a cursor that never advances fails
            page = export.run_page(self.conn, after=cursor, **kwargs)
            seen += [
                (r["machine_id"], r["source_date"], r["jobid"], r["line_seq"]) for r in page["runs"]
            ]
            cursor = page["next"]
            if cursor is None:
                return seen
        raise AssertionError("the cursor did not terminate")

    def test_paging_yields_every_row_exactly_once(self):
        full = self._walk(final_only=False, limit=100)
        for limit in (1, 2, 3, 4):
            with self.subTest(limit=limit):
                self.assertEqual(self._walk(final_only=False, limit=limit), full)

    def test_the_last_page_has_no_cursor(self):
        page = export.run_page(self.conn, final_only=False, limit=4)
        self.assertEqual(page["count"], 4)
        self.assertIsNone(page["next"])

    def test_the_order_is_the_row_identity(self):
        rows = self._walk(final_only=False, limit=100)
        self.assertEqual(rows, sorted(rows))

    def test_the_cursor_survives_a_key_the_separator_could_break(self):
        page = export.run_page(self.conn, final_only=False, limit=1)
        self.assertEqual(export.decode_cursor(page["next"]), (MACHINE.id, "2026-08-11", 1, 0))

    def test_a_broken_cursor_is_the_client_s_error(self):
        for value in ("nonsense", "a|b|c", "m|2026-08-11|x|0", "m|d|1|2|3", "m|d|1|2"):
            with self.subTest(cursor=value), self.assertRaises(export.BadRequest):
                export.run_page(self.conn, after=value)

    def test_a_cursor_date_is_normalised_like_the_range_filter(self):
        """A hand-built cursor in the basic form must not shift the position."""

        def page(cursor):
            return export.run_page(self.conn, final_only=False, after=cursor)

        self.assertEqual(
            page(f"{MACHINE.id}|20260811|1|0")["count"],
            page(f"{MACHINE.id}|2026-08-11|1|0")["count"],
        )

    def test_limit_is_clamped_not_refused(self):
        self.assertEqual(export.clamp_limit(export.MAX_LIMIT + 1000), export.MAX_LIMIT)
        with self.assertRaises(export.BadRequest):
            export.clamp_limit(0)

    def test_an_unknown_date_field_is_refused(self):
        """Refused, not ignored — the other date would shift the range by a day."""
        with self.assertRaises(export.BadRequest):
            export.run_page(self.conn, date_field="readydate")

    def test_the_two_date_fields_filter_differently(self):
        """A run started on the 11th and logged on the 12th."""
        self.conn.execute(
            JOB_SQL, (MACHINE.id, "2026-08-12", 8, "2026-08-11", "105774_Nightrun.pdf", 4)
        )
        self.conn.commit()
        by_start = export.run_page(
            self.conn, final_only=False, date_field="startdate", date_from="2026-08-12"
        )
        by_source = export.run_page(
            self.conn, final_only=False, date_field="source_date", date_from="2026-08-12"
        )
        self.assertNotIn(8, [r["jobid"] for r in by_start["runs"]])
        self.assertIn(8, [r["jobid"] for r in by_source["runs"]])


class DayListTests(ExportTestCase):
    def test_the_day_list_names_the_running_day(self):
        page = export.day_page(self.conn)
        self.assertEqual(
            [(d["machine_id"], d["source_date"], d["final"]) for d in page["days"]],
            [
                (MACHINE.id, "2026-08-11", True),
                (MACHINE.id, "2026-08-12", False),
                (SECOND_MACHINE.id, "2026-08-11", True),
            ],
        )

    def test_the_day_list_can_be_narrowed_to_one_press(self):
        page = export.day_page(self.conn, machine=SECOND_MACHINE.id)
        self.assertEqual([d["source_date"] for d in page["days"]], ["2026-08-11"])

    def test_the_day_list_validates_like_the_run_list(self):
        """Same guard as the run list."""
        with self.assertRaises(export.BadRequest):
            export.day_page(self.conn, machine="v1000-10")
        with self.assertRaises(export.BadRequest):
            export.day_page(self.conn, date_from="2026-13-45")

    def test_the_day_list_echoes_its_filter(self):
        page = export.day_page(self.conn, machine=MACHINE.id, date_from="2026-08-11")
        self.assertEqual(
            page["filter"], {"machine": MACHINE.id, "date_from": "2026-08-11", "date_to": None}
        )


class ValidationTests(ExportTestCase):
    """The silent-empty class of bug: a wrong parameter must fail, not return
    zero rows."""

    def test_an_unknown_machine_is_refused(self):
        with self.assertRaises(export.BadRequest) as caught:
            export.run_page(self.conn, machine="v1000-10")
        # The message names the known ids.
        self.assertIn(MACHINE.id, str(caught.exception))

    def test_a_decommissioned_machine_stays_exportable(self):
        """The validation set is the configuration union sync_log."""
        with configured(machines=(MACHINE,)):  # SECOND_MACHINE removed
            self.assertEqual(
                [m["id"] for m in export.known_machines(self.conn)], [MACHINE.id, SECOND_MACHINE.id]
            )
            page = export.run_page(self.conn, machine=SECOND_MACHINE.id, final_only=False)
            self.assertEqual({r["machine_id"] for r in page["runs"]}, {SECOND_MACHINE.id})

    def test_a_decommissioned_machine_has_no_name(self):
        with configured(machines=(MACHINE,)):
            entries = {m["id"]: m for m in export.known_machines(self.conn)}
        self.assertEqual(
            entries[MACHINE.id], {"id": MACHINE.id, "name": MACHINE.name, "configured": True}
        )
        self.assertEqual(
            entries[SECOND_MACHINE.id], {"id": SECOND_MACHINE.id, "name": None, "configured": False}
        )

    def test_a_configured_press_without_data_is_still_listed(self):
        """It has no sync_log row yet."""
        self.assertIn(SECOND_MACHINE.id, [m["id"] for m in export.known_machines(self.conn)])

    def test_the_published_set_and_the_accepted_set_are_the_same(self):
        for entry in export.known_machines(self.conn):
            with self.subTest(machine=entry["id"]):
                export.run_page(self.conn, machine=entry["id"])  # must not raise

    def test_an_unparseable_date_is_refused(self):
        for value in ("2026-13-45", "yesterday", "11.08.2026", "2026-08"):
            with self.subTest(date=value), self.assertRaises(export.BadRequest):
                export.run_page(self.conn, date_from=value)
        with self.assertRaises(export.BadRequest):
            export.run_page(self.conn, date_to="nonsense")

    def test_a_non_canonical_date_is_normalised_before_it_reaches_sql(self):
        """20260801 and week dates parse, but compare wrong as strings."""
        for value, canonical in (("20260811", "2026-08-11"), ("2026-W33-2", "2026-08-11")):
            with self.subTest(date=value):
                page = export.run_page(self.conn, final_only=False, date_from=value)
                self.assertEqual(page["filter"]["date_from"], canonical)
                self.assertEqual(
                    page["count"],
                    export.run_page(self.conn, final_only=False, date_from=canonical)["count"],
                )

    def test_an_empty_range_is_not_an_error(self):
        """A reversed date range returns no rows and echoes the applied filter."""
        page = export.run_page(self.conn, date_from="2026-09-01", date_to="2026-08-01")
        self.assertEqual(page["count"], 0)
        self.assertEqual(page["filter"]["date_from"], "2026-09-01")

    def test_canon_vocabulary_is_matched_leniently_never_restricted(self):
        """jobtype and result depend on the press model, so there is no list to
        check against; a lowercase spelling must still match."""
        lower = export.run_page(self.conn, jobtype="ip", result="done", final_only=False)
        upper = export.run_page(self.conn, jobtype="IP", result="DONE", final_only=False)
        self.assertEqual(lower["count"], upper["count"])
        self.assertEqual(lower["count"], 4)
        # A type this press never writes is a legal filter; the echo carries it.
        other = export.run_page(self.conn, jobtype="SCAN2MBX", final_only=False)
        self.assertEqual(other["count"], 0)
        self.assertEqual(other["filter"]["jobtype"], "SCAN2MBX")

    def test_the_filter_echo_reports_what_was_applied(self):
        page = export.run_page(
            self.conn,
            machine=MACHINE.id,
            date_from="2026-08-11",
            jobtype="ip",
            result="done",
            media=False,
            raw=True,
        )
        self.assertEqual(
            page["filter"],
            {
                "machine": MACHINE.id,
                "date_from": "2026-08-11",
                "date_to": None,
                "date_field": "startdate",
                "jobtype": "IP",
                "result": "DONE",
                "final_only": True,
                "media": False,
                "raw": True,
            },
        )


class CompletedDateTests(ExportTestCase):
    """Filtering and reporting by completion, without the caller having to
    rebuild Canon's empty-readydate quirk."""

    def setUp(self):
        super().setUp()
        # A run over midnight: started on the 11th, finished on the 12th.
        self.conn.execute(
            JOB_SQL, (MACHINE.id, "2026-08-11", 9, "2026-08-11", "105774_Nightshift.pdf", 7)
        )
        self.conn.execute(
            "UPDATE jobs SET starttime = '23:50:00', readydate = '2026-08-12',"
            " readytime = '00:20:00' WHERE jobid = 9"
        )
        self.conn.commit()

    def _run(self, jobid: int) -> dict:
        page = export.run_page(self.conn, final_only=False)
        return next(r for r in page["runs"] if r["jobid"] == jobid)

    def test_an_empty_readydate_falls_back_to_the_start_day(self):
        """Most runs finish on their start day, where the press leaves readydate
        empty; a bare column filter would drop them all."""
        run = self._run(1)
        self.assertEqual(run["readydate"], "")
        self.assertEqual(run["completed_date"], "2026-08-11")

    def test_a_run_over_midnight_completes_on_the_next_day(self):
        run = self._run(9)
        self.assertEqual(run["completed_date"], "2026-08-12")
        self.assertEqual(run["completed_at"], "2026-08-12 00:20:00")

    def test_the_raw_fields_stay_untouched(self):
        """The four raw date and time fields are part of the interface."""
        run = self._run(9)
        self.assertEqual(
            (run["startdate"], run["starttime"], run["readydate"], run["readytime"]),
            ("2026-08-11", "23:50:00", "2026-08-12", "00:20:00"),
        )

    def test_completed_at_is_null_without_a_usable_clock_time(self):
        self.conn.execute("UPDATE jobs SET readytime = '' WHERE jobid = 9")
        self.conn.commit()
        run = self._run(9)
        # The day is known, the moment is not — null rather than midnight.
        self.assertEqual(run["completed_date"], "2026-08-12")
        self.assertIsNone(run["completed_at"])

    def test_filtering_by_completion_differs_from_filtering_by_start(self):
        by_completion = export.run_page(
            self.conn, final_only=False, date_field="completed_date", date_from="2026-08-12"
        )
        by_start = export.run_page(
            self.conn, final_only=False, date_field="startdate", date_from="2026-08-12"
        )
        self.assertIn(9, [r["jobid"] for r in by_completion["runs"]])
        self.assertNotIn(9, [r["jobid"] for r in by_start["runs"]])

    def test_a_month_boundary_holds_from_both_sides(self):
        """The night run belongs to the day it ended, and to exactly one of the
        two ranges."""
        august = export.run_page(
            self.conn,
            final_only=False,
            date_field="completed_date",
            date_from="2026-08-01",
            date_to="2026-08-11",
        )
        september = export.run_page(
            self.conn,
            final_only=False,
            date_field="completed_date",
            date_from="2026-08-12",
            date_to="2026-08-31",
        )
        self.assertNotIn(9, [r["jobid"] for r in august["runs"]])
        self.assertIn(9, [r["jobid"] for r in september["runs"]])


class GuardTests(unittest.TestCase):
    def test_no_token_configured_means_no_interface(self):
        """503, never an open route."""
        with self.assertRaises(HTTPException) as caught:
            authorize("Bearer whatever", "")
        self.assertEqual(caught.exception.status_code, 503)

    def test_a_matching_token_passes(self):
        self.assertIsNone(authorize("Bearer s3cret", "s3cret"))

    def test_everything_else_is_a_401(self):
        for header in (
            None,
            "",
            "Bearer",
            "Bearer ",
            "Bearer wrong",
            "s3cret",
            "Basic s3cret",
            "bearer wrong",
        ):
            with self.subTest(header=header), self.assertRaises(HTTPException) as caught:
                authorize(header, "s3cret")
            self.assertEqual(caught.exception.status_code, 401)
            self.assertEqual(caught.exception.headers["WWW-Authenticate"], "Bearer")

    def test_the_scheme_is_case_insensitive_but_the_token_is_not(self):
        # RFC 7235: the scheme is case-insensitive. The credential is not.
        self.assertIsNone(authorize("bearer s3cret", "s3cret"))
        self.assertEqual(bearer_token("BEARER s3cret"), "s3cret")
        self.assertEqual(bearer_token("Bearer  s3cret "), "s3cret")
        self.assertEqual(bearer_token("Token s3cret"), "")


if __name__ == "__main__":
    unittest.main()
