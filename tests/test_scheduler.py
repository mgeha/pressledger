"""The scheduler's one job: never two syncs at once.

One job and one lock for all machines, because SQLite allows a single writer.
"""

import threading
import unittest
from unittest.mock import patch

from pressledger.scheduler import JOB_ID, run_sync_once, start_scheduler
from pressledger.sync import SyncResult
from tests.support import MACHINE, SECOND_MACHINE, configured


class RunSyncOnceTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(configured())

    def test_skips_parallel_sync(self):
        entered = threading.Event()
        release = threading.Event()
        expected = SyncResult(machine_id=MACHINE.id, ok=True)

        def blocking_sync(_machine, _raw_dir=None):
            entered.set()
            self.assertTrue(release.wait(timeout=2))
            return expected

        first_result = []
        with patch("pressledger.sync.sync", side_effect=blocking_sync) as sync_mock:
            first = threading.Thread(target=lambda: first_result.append(run_sync_once()))
            first.start()
            self.assertTrue(entered.wait(timeout=2))

            parallel_result = run_sync_once()
            release.set()
            first.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertIsNone(parallel_result)
        self.assertEqual(first_result, [[expected]])
        sync_mock.assert_called_once_with(MACHINE, None)

    def test_releases_lock_after_failure(self):
        outcomes = [RuntimeError("boom"), SyncResult(machine_id=MACHINE.id, ok=True)]
        with patch("pressledger.sync.sync", side_effect=outcomes):
            with self.assertLogs("pressledger.scheduler", level="ERROR"):
                self.assertIsNone(run_sync_once())
            self.assertTrue(run_sync_once()[0].ok)

    def test_every_machine_is_synced_in_turn(self):
        """Sequentially, so the single SQLite writer is never contended."""
        calls = []
        with (
            configured(machines=(MACHINE, SECOND_MACHINE)),
            patch(
                "pressledger.sync.sync",
                side_effect=lambda m, _raw=None: (
                    calls.append(m.id) or SyncResult(machine_id=m.id, ok=True)
                ),
            ),
        ):
            results = run_sync_once()

        self.assertEqual(calls, [MACHINE.id, SECOND_MACHINE.id])
        self.assertEqual([r.machine_id for r in results], [MACHINE.id, SECOND_MACHINE.id])

    def test_one_machine_can_be_synced_on_its_own(self):
        """The CLI's --machine and the per-press button on /status."""
        with (
            configured(machines=(MACHINE, SECOND_MACHINE)),
            patch(
                "pressledger.sync.sync",
                side_effect=lambda m, _raw=None: SyncResult(machine_id=m.id, ok=True),
            ),
        ):
            results = run_sync_once(SECOND_MACHINE.id)

        self.assertEqual([r.machine_id for r in results], [SECOND_MACHINE.id])

    def test_an_unknown_machine_id_syncs_nothing(self):
        with (
            configured(),
            patch("pressledger.sync.sync") as sync_mock,
            self.assertLogs("pressledger.scheduler", level="WARNING"),
        ):
            self.assertIsNone(run_sync_once("does-not-exist"))
        sync_mock.assert_not_called()


class SchedulerJobTests(unittest.IsolatedAsyncioTestCase):
    """AsyncIOScheduler needs a running loop, so the test is async."""

    async def test_two_machines_still_mean_one_job(self):
        """A job per press would run them concurrently — see the module docstring."""
        with configured(machines=(MACHINE, SECOND_MACHINE)):
            scheduler = start_scheduler()
            try:
                jobs = scheduler.get_jobs()
            finally:
                scheduler.shutdown(wait=False)

        self.assertEqual([j.id for j in jobs], [JOB_ID])


if __name__ == "__main__":
    unittest.main()
