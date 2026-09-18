"""Reports across two machines.

Every join over the row identity (machine_id, source_date, jobid, line_seq) must
carry machine_id: jobid and line_seq are machine-local, so leaving it out fans
the sheets out over every press, by a clean multiple that still looks plausible.

The assertions therefore compare against SUM() straight off the tables, not
against another query written the same way.
"""

import sqlite3
import unittest

from pressledger.db import SCHEMA_SQL, register_functions
from pressledger.reports import job_detail, job_list, paper_usage
from tests.support import MACHINE, SECOND_MACHINE, configured

# All six click columns are filled: one NULL makes the whole sum NULL.
JOB_SQL = """
    INSERT INTO jobs (
        machine_id, source_date, jobid, line_seq, jobtype, startdate, starttime,
        readydate, readytime, result, jobname, noffinishedsets,
        nofprinteda4bw, nofprinteda4c, nofprinteda3bw, nofprinteda3c,
        nofprintedXLbw, nofprintedXLc
    ) VALUES (?, '2026-08-11', ?, 0, 'IP', '2026-08-11', '09:00:00',
              '', '09:10:00', 'Done', ?, 1, ?, 0, 0, 0, 0, 0)
"""

MEDIA_SQL = """
    INSERT INTO job_media (
        machine_id, source_date, jobid, line_seq, tray,
        mediaformat, mediatype, mediaweight, mediacolor, medianame,
        nofsimplex, nofduplex, isinsert, istab
    ) VALUES (?, '2026-08-11', ?, 0, 1,
              'SRA3', 'Plain', 100, 'White', 'Test paper', ?, 0, '', '')
"""


class TwoMachineReportTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(configured(job_key_pattern=r"^(\d{6})_"))
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)
        register_functions(self.conn)
        self.addCleanup(self.conn.close)

        # The same job on both presses, with the SAME jobid and line_seq — the
        # normal case, since both counters are machine-local.
        for machine in (MACHINE.id, SECOND_MACHINE.id):
            self.conn.execute(JOB_SQL, (machine, 1, "105774_Customer.pdf", 10))
            self.conn.execute(MEDIA_SQL, (machine, 1, 10))
        # A second job, on one press only, so a filter has something to remove.
        self.conn.execute(JOB_SQL, (SECOND_MACHINE.id, 2, "105999_Other.pdf", 5))
        self.conn.execute(MEDIA_SQL, (SECOND_MACHINE.id, 2, 5))
        self.conn.commit()

    def _truth(self, sql: str) -> int:
        return self.conn.execute(sql).fetchone()[0]

    def test_paper_sheets_match_the_table(self):
        grades = paper_usage(self.conn)
        self.assertEqual(
            sum(g["sheets"] for g in grades),
            self._truth("SELECT SUM(nofsimplex + (nofduplex+1)/2) FROM job_media"),
        )
        self.assertEqual(
            sum(g["rows"] for g in grades), self._truth("SELECT COUNT(*) FROM job_media")
        )

    def test_paper_counts_a_job_on_two_presses_once(self):
        """Count a shared job key once across machines, as in /jobs."""
        grades = paper_usage(self.conn)
        self.assertEqual(sum(g["job_count"] for g in grades), 2)

    def test_the_paper_machine_filter_removes_only_that_press(self):
        grades = paper_usage(self.conn, machine=MACHINE.id)
        self.assertEqual(
            sum(g["sheets"] for g in grades),
            self._truth(
                "SELECT SUM(nofsimplex + (nofduplex+1)/2)"
                " FROM job_media WHERE machine_id = 'v1000-01'"
            ),
        )

    def test_job_sheets_match_the_table(self):
        """sheets_cte is the second join over the row identity."""
        jobs = job_list(self.conn)
        self.assertEqual(
            sum(j["sheets"] for j in jobs),
            self._truth("SELECT SUM(nofsimplex + (nofduplex+1)/2) FROM job_media"),
        )
        self.assertEqual(
            sum(j["clicks"] for j in jobs),
            self._truth(
                "SELECT SUM(nofprinteda4bw + nofprinteda4c"
                " + nofprinteda3bw + nofprinteda3c"
                " + nofprintedXLbw + nofprintedXLc) FROM jobs"
            ),
        )

    def test_job_runtime_sums_every_machine_run(self):
        jobs = {j["job_key"]: j for j in job_list(self.conn)}
        self.assertEqual(jobs["105774"]["runtime_s"], 20 * 60)
        self.assertEqual(jobs["105999"]["runtime_s"], 10 * 60)

        filtered = job_list(self.conn, machine=MACHINE.id)
        self.assertEqual(sum(j["runtime_s"] for j in filtered), 10 * 60)

    def test_a_job_on_two_presses_is_one_row_naming_both(self):
        jobs = {j["job_key"]: j for j in job_list(self.conn)}
        self.assertEqual(sorted(jobs), ["105774", "105999"])
        self.assertEqual(jobs["105774"]["machines"], [MACHINE.id, SECOND_MACHINE.id])
        self.assertEqual(jobs["105774"]["rows"], 2)
        self.assertEqual(jobs["105999"]["machines"], [SECOND_MACHINE.id])

    def test_the_detail_page_breaks_it_down_per_press(self):
        job = job_detail(self.conn, "105774")
        self.assertEqual(
            [(e["machine_id"], e["runs"], e["clicks"], e["sheets"]) for e in job["by_machine"]],
            [(MACHINE.id, 1, 10, 10), (SECOND_MACHINE.id, 1, 10, 10)],
        )
        # Media is looked up per row, so the four-part key has to match too.
        self.assertEqual([len(r["media"]) for r in job["rows"]], [1, 1])
        self.assertEqual(sum(m["sheets"] for r in job["rows"] for m in r["media"]), 20)

    def test_the_material_summary_does_not_fan_out(self):
        job = job_detail(self.conn, "105774")
        self.assertEqual(sum(m["sheets"] for m in job["material"]), 20)


if __name__ == "__main__":
    unittest.main()
