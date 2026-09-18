import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import requests

from . import config
from .db import ensure_schema, get_conn, reset_db
from .parser import RECORD_HEADER, decode_content, extract_media, parse_csv, row_to_db

log = logging.getLogger(__name__)


class InvalidAccountingFile(ValueError):
    """A download that is not a usable Canon accounting snapshot."""


INSERT_JOB_SQL = """
    INSERT INTO jobs (
        machine_id, source_date, jobid, line_seq, jobtype,
        startdate, starttime, readydate, readytime,
        result, username, jobname,
        noffinishedsets, nofprinteda4bw, nofprinteda4c,
        nofprinteda3bw, nofprinteda3c, nofprintedXLbw, nofprintedXLc,
        nofbooklets, nofsinglestaples, nofdoublestaples,
        nofpunches, nofcreases, noffolds, raw_json
    ) VALUES (
        :machine_id, :source_date, :jobid, :line_seq, :jobtype,
        :startdate, :starttime, :readydate, :readytime,
        :result, :username, :jobname,
        :noffinishedsets, :nofprinteda4bw, :nofprinteda4c,
        :nofprinteda3bw, :nofprinteda3c, :nofprintedXLbw, :nofprintedXLc,
        :nofbooklets, :nofsinglestaples, :nofdoublestaples,
        :nofpunches, :nofcreases, :noffolds, :raw_json
    )
"""

INSERT_MEDIA_SQL = """
    INSERT INTO job_media (
        machine_id, source_date, jobid, line_seq, tray,
        mediaformat, mediatype, mediaweight, mediacolor, medianame,
        nofsimplex, nofduplex, isinsert, istab
    ) VALUES (
        :machine_id, :source_date, :jobid, :line_seq, :tray,
        :mediaformat, :mediatype, :mediaweight, :mediacolor, :medianame,
        :nofsimplex, :nofduplex, :isinsert, :istab
    )
"""


@dataclass
class SyncResult:
    """Result of a sync attempt against ONE machine. ok=False means the listing
    could not be fetched, most often because that press was off."""

    machine_id: str = ""
    ok: bool = False
    error: str | None = None
    files_seen: int = 0
    files_imported: int = 0
    rows_imported: int = 0


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _short_error(exc: Exception) -> str:
    """Short message for sync_run and the UI, instead of a traceback."""
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return "connection timeout"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "no connection"
    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout"
    if isinstance(exc, requests.exceptions.HTTPError):
        status = exc.response.status_code if exc.response is not None else "?"
        return f"HTTP {status}"
    first_line = str(exc).strip().splitlines()
    return (first_line[0][:200] if first_line else "") or type(exc).__name__


_SAFE_FILENAME_RE = re.compile(r"^[^/\\:\x00-\x1f]+\.(?:CSV|ACL)$", re.IGNORECASE)


def _is_safe_filename(filename: str) -> bool:
    """No separators, drive letter, control chars, '.'/'..' — only .CSV/.ACL.

    "D:outside...CSV" carries no separator yet still escapes raw_dir: pathlib
    reads it as a Windows drive-relative path.
    """
    if filename in (".", ".."):
        return False
    return bool(_SAFE_FILENAME_RE.fullmatch(filename))


def _fetch_listing(base_url: str) -> list[tuple[str, str]]:
    """Returns list of (download_url, filename); unsafe names filtered out."""
    resp = requests.get(f"{base_url}/accounting/", timeout=config.get().http_timeout)
    resp.raise_for_status()
    matches = re.findall(r'href="(/accounting/([^"/\\]+))"', resp.text, re.IGNORECASE)
    listing = []
    for path, filename in matches:
        if not _is_safe_filename(filename):
            log.warning("Skipping unsafe file name in accounting listing: %r", filename)
            continue
        listing.append((f"{base_url}{path}", filename))
    return listing


# Canon names the file <serial><yyyy><mm><dd> daily, <serial><yyyy>W<ww> or
# <serial><yyyy>M<mm> on weekly and monthly rotation. One file is one calendar
# day here, so the other two are refused by name rather than mis-parsed.
_DAILY_RE = re.compile(r"(\d{4})(\d{2})(\d{2})$")
_ROTATION_RE = re.compile(r"(\d{4})([WM])(\d{2})$")


def _parse_source_date(filename: str) -> str:
    """Extract YYYY-MM-DD from filename like 99900011120260803.CSV."""
    stem = Path(filename).stem
    m = _DAILY_RE.search(stem)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    rotation = _ROTATION_RE.search(stem)
    if rotation:
        interval = "weekly" if rotation.group(2) == "W" else "monthly"
        raise ValueError(
            f"{interval} log rotation — PressLedger needs one file per day; "
            "set the accounting log interval back to daily in the Settings Editor"
        )
    raise ValueError(f"Cannot parse date from filename: {filename}")


def _validate_snapshot(conn, machine_id: str, source_date: str, content_bytes: bytes) -> list[dict]:
    """Parse a snapshot and refuse it if it looks like a partial download.

    An import replaces a whole day (DELETE + reinsert), so an incomplete
    snapshot would delete records the machine has already reported.

    * the 4302 header catches an HTML error page served with HTTP 200;
    * the trailing newline catches a body cut short without a Content-Length,
      which raises nothing in requests;
    * the row count must not fall below what the day already holds — the ACL
      only grows — which also catches a cut on a line boundary.
    """
    content = decode_content(content_bytes)
    header_prefixes = (f"{RECORD_HEADER};", f"{RECORD_HEADER},")
    if not any(
        line.lstrip("\ufeff").lstrip().startswith(header_prefixes) for line in content.splitlines()
    ):
        raise InvalidAccountingFile("missing Canon 4302 header")

    if not content.endswith("\n"):
        raise InvalidAccountingFile("does not end in a newline — truncated?")

    rows = parse_csv(content)
    existing = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE machine_id = ? AND source_date = ?",
        (machine_id, source_date),
    ).fetchone()["n"]
    if len(rows) < existing:
        raise InvalidAccountingFile(
            f"snapshot holds {len(rows)} row(s), the database already has "
            f"{existing} for {source_date}"
        )
    return rows


def _import_date(
    conn, machine_id: str, source_date: str, source_type: str, filename: str, content_bytes: bytes
) -> int:
    rows = _validate_snapshot(conn, machine_id, source_date, content_bytes)

    job_rows = []
    media_rows = []
    for line_seq, d in enumerate(rows):
        job = row_to_db(d, machine_id, source_date, line_seq)
        job_rows.append(job)
        media_rows.extend(extract_media(d, machine_id, source_date, job["jobid"], line_seq))

    with conn:
        # job_media first — otherwise an ACL refresh would leave orphaned media rows.
        # Both DELETEs are scoped to the machine: a day exists once per press.
        conn.execute(
            "DELETE FROM job_media WHERE machine_id = ? AND source_date = ?",
            (machine_id, source_date),
        )
        conn.execute(
            "DELETE FROM jobs WHERE machine_id = ? AND source_date = ?",
            (machine_id, source_date),
        )
        if job_rows:
            conn.executemany(INSERT_JOB_SQL, job_rows)
        if media_rows:
            conn.executemany(INSERT_MEDIA_SQL, media_rows)
        conn.execute(
            """INSERT OR REPLACE INTO sync_log
                   (machine_id, source_date, source_type, filename,
                    last_synced, row_count)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (machine_id, source_date, source_type, filename, _now(), len(job_rows)),
        )
    return len(job_rows)


ARCHIVE_PART_SUFFIX = ".part"


def _archive(raw_dir: Path, filename: str, content: bytes) -> None:
    """Atomically replace a validated raw snapshot.

    Write and rename, so a sync that dies mid-write leaves a .part behind rather
    than half a day; _raw_files() skips those by suffix and warns. The filename
    is checked here as well as in the caller, because this is what writes.
    """
    if not _is_safe_filename(filename):
        raise InvalidAccountingFile(f"Refusing to archive unsafe file name: {filename!r}")

    raw_dir.mkdir(parents=True, exist_ok=True)
    target = raw_dir / filename
    if target.resolve().parent != raw_dir.resolve():
        raise InvalidAccountingFile(f"Archive target escapes {raw_dir}: {filename!r}")

    temporary = target.with_suffix(target.suffix + ARCHIVE_PART_SUFFIX)
    temporary.write_bytes(content)
    temporary.replace(target)


def _record_run(conn, started_at: str, result: SyncResult) -> None:
    with conn:
        conn.execute(
            """INSERT INTO sync_run
                   (machine_id, started_at, finished_at, ok, error,
                    files_seen, files_imported, rows_imported)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                result.machine_id,
                started_at,
                _now(),
                1 if result.ok else 0,
                result.error,
                result.files_seen,
                result.files_imported,
                result.rows_imported,
            ),
        )


def _plan_action(conn, machine_id: str, source_date: str, ext: str) -> str | None:
    """'IMPORT' / 'FINALIZE' / 'UPDATE' — or None if the day should be skipped.

    A day imported from a CSV is final: a later ACL for it is the stale running
    log and would replace the final rows.
    """
    row = conn.execute(
        "SELECT source_type FROM sync_log WHERE machine_id = ? AND source_date = ?",
        (machine_id, source_date),
    ).fetchone()
    if row is None:
        return "IMPORT"
    if row["source_type"] == "CSV":
        return None
    return "FINALIZE" if ext == "CSV" else "UPDATE"


def machine_raw_dir(machine_id: str, raw_dir: Path | None = None) -> Path:
    """Archive directory of one press.

    The directory is what tells a reimport which press a file belongs to — never
    the serial number in the file name, which is the machine's own label.
    """
    return (raw_dir or config.get().raw_dir) / machine_id


def sync(machine: config.Machine, raw_dir: Path | None = None) -> SyncResult:
    """Fetch accounting files from ONE press and import them.

    An unreachable press does not raise: it is the expected state.
    """
    base_url = machine.url.rstrip("/")
    machine_dir = machine_raw_dir(machine.id, raw_dir)

    conn = get_conn()
    try:
        ensure_schema(conn)

        started_at = _now()
        result = SyncResult(machine_id=machine.id)

        try:
            files = _fetch_listing(base_url)
        except Exception as exc:
            result.error = _short_error(exc)
            # info, not warning: a press that is off nights and weekends would
            # flood the journal at anything higher.
            log.info("%s unreachable (%s) — sync skipped", machine.id, result.error)
            _record_run(conn, started_at, result)
            return result

        result.ok = True
        result.files_seen = len(files)
        log.info("%s reachable, %d file(s) in listing", machine.id, len(files))

        download_errors = []

        for url, filename in files:
            ext = Path(filename).suffix.upper()[1:]  # 'CSV' or 'ACL'

            try:
                source_date = _parse_source_date(filename)
            except ValueError as exc:
                log.warning("Skipping %s: %s", filename, exc)
                continue

            action = _plan_action(conn, machine.id, source_date, ext)
            if action is None:
                log.debug("%s already finalized, skipping", filename)
                continue

            try:
                resp = requests.get(url, timeout=config.get().http_timeout)
                resp.raise_for_status()
            except Exception as exc:
                # Can happen when the machine is switched off mid-sync.
                short = _short_error(exc)
                download_errors.append(f"{filename}: {short}")
                log.info("Download of %s failed (%s)", filename, short)
                continue

            # Validate before archiving, so a partial download cannot overwrite
            # the recovery source. _import_date validates again.
            try:
                _validate_snapshot(conn, machine.id, source_date, resp.content)
            except InvalidAccountingFile as exc:
                log.warning("Skipping invalid snapshot %s: %s", filename, exc)
                download_errors.append(f"{filename}: {exc}")
                continue
            except Exception:
                log.exception("Validation of %s failed", filename)
                download_errors.append(f"{filename}: import error")
                continue

            try:
                _archive(machine_dir, filename, resp.content)
            except (OSError, InvalidAccountingFile):
                log.exception("Archiving of %s failed", filename)
                download_errors.append(f"{filename}: archive error")
                continue

            try:
                count = _import_date(conn, machine.id, source_date, ext, filename, resp.content)
            except Exception:
                # The file is archived, so a later reimport can repair this.
                log.exception("Import of %s failed", filename)
                download_errors.append(f"{filename}: import error")
                continue

            result.files_imported += 1
            result.rows_imported += count
            log.info("%s %s — %d rows", action, filename, count)

        if download_errors:
            result.error = "; ".join(download_errors)[:500]

        _record_run(conn, started_at, result)
        log.info(
            "%s complete: %d file(s) imported, %d rows",
            machine.id,
            result.files_imported,
            result.rows_imported,
        )
        return result
    finally:
        conn.close()


def sync_all(
    raw_dir: Path | None = None, machines: tuple[config.Machine, ...] | None = None
) -> list[SyncResult]:
    """Sync every configured press, one after the other.

    Sequential because SQLite allows a single writer; an unreachable press delays
    the next one by up to sync.http_timeout. One result and one sync_run row per
    press. `machines` narrows the run to a subset.
    """
    if machines is None:
        machines = config.get().machines
    return [sync(machine, raw_dir) for machine in machines]


def _raw_files(raw_dir: Path) -> list[Path]:
    """Raw files in import order.

    When both ACL and CSV exist for the same day, the CSV must be imported
    last — it is the final version and must win.
    """
    files = []
    leftovers = []
    for p in raw_dir.iterdir():
        if not p.is_file():
            continue
        if p.suffix == ARCHIVE_PART_SUFFIX:
            leftovers.append(p.name)
        elif p.suffix.upper() in (".CSV", ".ACL"):
            files.append(p)
    if leftovers:
        # A sync that died mid-write: possibly partial, so never imported.
        log.warning("Leftover partial download(s) in %s: %s", raw_dir, ", ".join(sorted(leftovers)))

    def sort_key(p: Path):
        try:
            source_date = _parse_source_date(p.name)
        except ValueError:
            source_date = ""
        return (source_date, 0 if p.suffix.upper() == ".ACL" else 1)

    return sorted(files, key=sort_key)


def _check_flat_archive(raw_dir: Path, missing: list[str]) -> None:
    """Refuse to rebuild while raw files lie directly in the archive root.

    They belong in data/raw/<machine_id>/. Aborting with the `mv` keeps a rebuild
    from dropping the database and refilling it with nothing.
    """
    flat = [
        p.name for p in raw_dir.iterdir() if p.is_file() and p.suffix.upper() in (".CSV", ".ACL")
    ]
    if not flat:
        return
    raise InvalidAccountingFile(
        f"{len(flat)} raw file(s) lie directly in {raw_dir}, and the archive "
        f"directory of {', '.join(missing)} is missing. The archive is one "
        f"directory per machine:\n"
        f"  mkdir -p {raw_dir / missing[0]} && "
        f"mv {raw_dir}/*.CSV {raw_dir}/*.ACL {raw_dir / missing[0]}/"
    )


def reimport_from_raw(rebuild: bool = False, raw_dir: Path | None = None) -> SyncResult:
    """Rebuild the database from the local raw archive, without the printer.

    The machine of a file is the directory it lies in, so every configured press
    is read in turn.
    """
    raw_dir = raw_dir or config.get().raw_dir
    machines = config.get().machines

    result = SyncResult(ok=True)

    if not raw_dir.is_dir():
        result.error = f"Raw data directory not found: {raw_dir}"
        log.warning(result.error)
        return result

    missing = [m.id for m in machines if not machine_raw_dir(m.id, raw_dir).is_dir()]
    if missing:
        _check_flat_archive(raw_dir, missing)

    conn = get_conn()
    try:
        if rebuild:
            reset_db(conn)
            log.info("Schema recreated")
        else:
            ensure_schema(conn)

        import_errors = []

        for machine in machines:
            machine_dir = machine_raw_dir(machine.id, raw_dir)
            if not machine_dir.is_dir():
                # Legitimate for a press that has not synced yet.
                log.info("No archive directory for %s yet — nothing to import", machine.id)
                continue

            files = _raw_files(machine_dir)
            result.files_seen += len(files)

            for path in files:
                try:
                    source_date = _parse_source_date(path.name)
                except ValueError as exc:
                    log.warning("Skipping %s: %s", path.name, exc)
                    continue

                ext = path.suffix.upper()[1:]
                try:
                    count = _import_date(
                        conn, machine.id, source_date, ext, path.name, path.read_bytes()
                    )
                except InvalidAccountingFile as exc:
                    # One unusable file must not abort the whole rebuild. It is
                    # skipped and recorded in result.error, on which the CLI exits 1.
                    log.warning("Skipping %s: %s", path.name, exc)
                    import_errors.append(f"{path.name}: {exc}")
                    continue
                result.files_imported += 1
                result.rows_imported += count
                log.info("IMPORT %s %s — %d rows", machine.id, path.name, count)

        if import_errors:
            result.error = "; ".join(import_errors)[:500]

        log.info(
            "Reimport complete: %d file(s), %d rows",
            result.files_imported,
            result.rows_imported,
        )
        return result
    finally:
        conn.close()
