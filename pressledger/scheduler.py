"""Periodic data fetch inside the running web service.

A sync attempt without a connection is expected: logged, data unchanged, and it
must not stop the scheduler.
"""

import asyncio
import logging
import threading
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from . import config
from .sync import SyncResult, sync_all

log = logging.getLogger(__name__)

JOB_ID = "sync"

# One lock for all machines, shared by scheduled and manual syncs: a sync is
# downloads, archive writes and several transactions that must not interleave.
_sync_lock = threading.Lock()

# Fetch once shortly after start, but only after the service has come up.
INITIAL_DELAY_S = 5


def run_sync_once(machine_id: str | None = None) -> list[SyncResult] | None:
    """Blocking sync, every configured press in turn without an id.

    Catches everything: sync() handles the offline case, and a program error
    beyond that must still not halt the scheduler.
    """
    if not _sync_lock.acquire(blocking=False):
        log.info("Sync skipped — another sync is already running")
        return None

    try:
        try:
            if machine_id is None:
                return sync_all()
            machine = config.get().machine(machine_id)
            if machine is None:
                log.warning("Unknown machine id %r — sync skipped", machine_id)
                return None
            return sync_all(machines=(machine,))
        except Exception:
            log.exception("Sync failed unexpectedly")
            return None
    finally:
        _sync_lock.release()


async def _tick() -> None:
    loop = asyncio.get_running_loop()
    # requests is blocking; running it in an executor keeps the event loop free.
    await loop.run_in_executor(None, run_sync_once)


def start_scheduler() -> AsyncIOScheduler:
    """One job for all machines, which then syncs them one after the other.

    Not one job per machine: they would collide on the single SQLite writer.
    """
    settings = config.get()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _tick,
        trigger=IntervalTrigger(minutes=settings.sync_interval_min),
        id=JOB_ID,
        name="Fetch accounting data",
        # No second run while one is still in progress.
        max_instances=1,
        # After a restart or standby, do not catch up every missed run.
        coalesce=True,
        misfire_grace_time=300,
        next_run_time=datetime.now() + timedelta(seconds=INITIAL_DELAY_S),
    )
    scheduler.start()
    log.info(
        "Scheduler active: every %d minutes against %s",
        settings.sync_interval_min,
        ", ".join(f"{m.id} ({m.url})" for m in settings.machines),
    )
    return scheduler


def stop_scheduler(scheduler: AsyncIOScheduler) -> None:
    scheduler.shutdown(wait=False)
    log.info("Scheduler stopped")
