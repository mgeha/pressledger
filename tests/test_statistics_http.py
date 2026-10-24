"""/statistics over HTTP: the month row and its links."""

import sqlite3
import unittest

from fastapi.testclient import TestClient

from pressledger.db import SCHEMA_SQL, register_functions
from pressledger.reports import job_list, keyless_clicks
from pressledger.web.app import _month_range, _statistics_ctx, app
from pressledger.web.deps import db
from tests.support import MACHINE, configured

JOB_SQL = """
    INSERT INTO jobs (
        machine_id, source_date, jobid, line_seq, jobtype, startdate, starttime,
        readydate, readytime, result, jobname, noffinishedsets,
        nofprinteda4bw, nofprinteda4c, nofprinteda3bw, nofprinteda3c,
        nofprintedXLbw, nofprintedXLc
    ) VALUES (?, ?, ?, 0, ?, ?, '09:00:00', '', '09:30:00', 'Done', ?, 1, ?, ?, 0, 0, 0, 0)
"""


class StatisticsHttpTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(configured(job_key_pattern=r"^(\d{6})_"))
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)
        register_functions(self.conn)
        self.addCleanup(self.conn.close)
        rows = [
            ("2026-09-03", 1, "IP", "105774_Customer.pdf", 10, 5),
            ("2026-09-04", 2, "IP", "105774_Customer.pdf", 4, 0),
            ("2026-09-04", 3, "IP", "Flyer ohne Nummer.pdf", 7, 1),
            ("2026-09-05", 4, "SYSTEM", "Service Job", 2, 0),
            # Same job, next month: a second row of its own.
            ("2026-10-01", 5, "IP", "105774_Customer.pdf", 100, 0),
        ]
        for day, jobid, jobtype, name, bw, colour in rows:
            self.conn.execute(JOB_SQL, (MACHINE.id, day, jobid, jobtype, day, name, bw, colour))
        self.conn.commit()

        app.dependency_overrides[db] = lambda: self.conn
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app)

    def test_own_use_is_not_counted_as_unassigned(self):
        [_october, september] = _statistics_ctx(self.conn, "", "")["months"]
        self.assertEqual(
            (september["clicks"], september["unassigned_clicks"], september["own_clicks"]),
            (29, 8, 2),
        )
        self.assertEqual((september["jobs"], september["runs"]), (1, 4))

    def test_the_job_list_of_a_month_accounts_for_every_statistics_click(self):
        [_october, september] = _statistics_ctx(self.conn, "", "")["months"]
        filtered = {"date_from": "2026-09-01", "date_to": "2026-09-30"}
        listed = sum(j["clicks"] for j in job_list(self.conn, jobtype=None, **filtered))
        keyless = keyless_clicks(self.conn, jobtype=None, **filtered)
        self.assertEqual((listed, keyless["unassigned"], keyless["own_use"]), (19, 8, 2))
        self.assertEqual(listed + keyless["unassigned"] + keyless["own_use"], september["clicks"])

        page = self.client.get("/jobs?date_from=2026-09-01&date_to=2026-09-30&jobtype=").text
        self.assertIn("29 clicks.", " ".join(page.split()))

    def test_a_job_type_filter_leaves_own_use_out_of_the_note(self):
        keyless = keyless_clicks(
            self.conn, date_from="2026-09-01", date_to="2026-09-30", jobtype="IP"
        )
        self.assertEqual((keyless["unassigned"], keyless["own_use"]), (8, 0))

    def test_the_table_partial_brings_the_note_for_the_new_filter(self):
        part = self.client.get("/jobs/table?date_from=2026-09-04&date_to=2026-09-05&jobtype=").text
        self.assertIn('id="jobs-figures" hx-swap-oob="true"', part)
        self.assertIn("together 14 clicks.", " ".join(part.split()))

    def test_the_note_links_keep_search_and_job_type(self):
        page = self.client.get("/jobs?q=Flyer&jobtype=IP").text
        self.assertIn('href="/unassigned?q=Flyer&amp;filter=IP"', page)
        self.assertNotIn("hx-swap-oob", page)

    def test_the_page_links_each_month_to_its_job_list(self):
        page = self.client.get("/statistics").text
        self.assertIn('href="/jobs?date_from=2026-09-01&amp;date_to=2026-09-30&amp;jobtype="', page)

    def test_a_month_link_keeps_inside_the_page_filter(self):
        self.assertEqual(_month_range("2026-02", "", ""), ("2026-02-01", "2026-02-28"))
        self.assertEqual(
            _month_range("2026-09", "2026-09-10", "2026-12-31"), ("2026-09-10", "2026-09-30")
        )


if __name__ == "__main__":
    unittest.main()
