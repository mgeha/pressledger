"""Flat rows for the export API.

One object per print run, identified by `(machine_id, source_date, jobid,
line_seq)`, carrying the machine's own field names, counts, run time and raw
fields. Grouping, order numbers and pricing are the caller's.

Own queries, not reports._base_cte(), which derives the display conventions of
this installation. An unknown machine id or an invalid date is a 400, never an
empty page; `jobtype` and `result` are unrestricted and echoed back instead.
"""

import json
from datetime import date

from . import config, reports

# Spelled out rather than `jobs.*` so a new column does not enter the interface
# by itself.
RUN_COLUMNS = (
    "machine_id",
    "source_date",
    "jobid",
    "line_seq",
    "jobtype",
    "startdate",
    "starttime",
    "readydate",
    "readytime",
    "result",
    "username",
    "jobname",
    "noffinishedsets",
    "nofprinteda4bw",
    "nofprinteda4c",
    "nofprinteda3bw",
    "nofprinteda3c",
    "nofprintedXLbw",
    "nofprintedXLc",
    "nofbooklets",
    "nofsinglestaples",
    "nofdoublestaples",
    "nofpunches",
    "nofcreases",
    "noffolds",
)

MEDIA_COLUMNS = (
    "tray",
    "mediaformat",
    "mediatype",
    "mediaweight",
    "mediacolor",
    "medianame",
    "nofsimplex",
    "nofduplex",
    "isinsert",
    "istab",
)

DEFAULT_LIMIT = 500
MAX_LIMIT = 5000


def completed_date_sql(alias: str = "j") -> str:
    """When a run finished, as a date: readydate, or startdate where the press
    left it empty — which it does on every run that finished on its start day.
    Same fallback as reports._END_TS.
    """
    p = f"{alias}." if alias else ""
    return f"COALESCE(NULLIF({p}readydate, ''), {p}startdate)"


# What the range filter can apply to: the start of the run, the log day it was
# read from, or the completion day.
DATE_FIELDS = ("startdate", "source_date", "completed_date")

CURSOR_SEP = "|"


def _date_sql(date_field: str) -> str:
    """What the range filter compares against. Qualified with the `j` alias —
    machine_id and source_date exist in sync_log too."""
    if date_field == "completed_date":
        return completed_date_sql("j")
    return f"j.{date_field}"


class BadRequest(ValueError):
    """A parameter the caller has to fix — the web layer turns this into a 400."""


def known_machines(conn) -> list[dict]:
    """Every press this database can answer for: configured, or with data.

    The union of `pressledger.toml` and the machine ids in `sync_log`, so a
    decommissioned press stays reachable. What /api/v1/machines publishes and
    what the machine filter validates against.
    """
    configured = config.get().machines
    entries = [{"id": m.id, "name": m.name, "configured": True} for m in configured]
    known = {m.id for m in configured}
    historic = conn.execute(
        "SELECT DISTINCT machine_id FROM sync_log ORDER BY machine_id"
    ).fetchall()
    entries += [
        # A press has a name only in the configuration, and this one is not in it.
        {"id": row["machine_id"], "name": None, "configured": False}
        for row in historic
        if row["machine_id"] not in known
    ]
    return entries


def _validate_machine(conn, machine: str | None) -> str | None:
    """Reject unknown machine IDs instead of returning an empty result."""
    if not machine:
        return None
    known = [m["id"] for m in known_machines(conn)]
    if machine not in known:
        raise BadRequest(
            f"Unknown machine {machine!r}. Known: {', '.join(known) or '(none)'} "
            "— see /api/v1/machines"
        )
    return machine


def _validate_date(value: str | None, label: str) -> str | None:
    """Validate and normalise a date before SQLite compares it as a string.

    fromisoformat() also accepts 20260801 and ISO week dates, which would be
    compared against YYYY-MM-DD rows and match nothing.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise BadRequest(f"{label} must be a date as YYYY-MM-DD, got {value!r}") from exc


def clamp_limit(value) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise BadRequest(f"limit must be an integer, got {value!r}") from exc
    if limit < 1:
        raise BadRequest("limit must be at least 1")
    return min(limit, MAX_LIMIT)


def encode_cursor(row: dict) -> str:
    """The row identity as one string.

    The machine id cannot contain the separator (config.MACHINE_ID_RE) and the
    other three parts are a date and two integers, so the split is unambiguous.
    """
    return CURSOR_SEP.join(str(row[c]) for c in RUN_COLUMNS[:4])


def decode_cursor(value: str) -> tuple[str, str, int, int]:
    parts = (value or "").split(CURSOR_SEP)
    if len(parts) != 4:
        raise BadRequest(
            f"Invalid cursor {value!r}: expected "
            f"machine_id{CURSOR_SEP}source_date{CURSOR_SEP}jobid{CURSOR_SEP}line_seq"
        )
    machine_id, source_date, jobid, line_seq = parts
    try:
        jobid, line_seq = int(jobid), int(line_seq)
    except ValueError as exc:
        raise BadRequest(f"Invalid cursor {value!r}: jobid and line_seq must be integers") from exc
    try:
        # Normalised like the range filter: the row-value comparison below sorts
        # a non-canonical date to the wrong place.
        source_date = date.fromisoformat(source_date).isoformat()
    except ValueError as exc:
        raise BadRequest(f"Invalid cursor {value!r}: source_date must be YYYY-MM-DD") from exc
    return machine_id, source_date, jobid, line_seq


def _filters(
    conn, machine, date_from, date_to, date_field, jobtype, result, final_only, after
) -> tuple[str, dict, dict]:
    """The WHERE fragment, its parameters, and the filter echo.

    `jobtype` and `result` are Canon's vocabulary and depend on the press model,
    so they are not validated; the echo is what makes a typo visible.
    """
    if date_field not in DATE_FIELDS:
        raise BadRequest(f"date_field must be one of {', '.join(DATE_FIELDS)}, got {date_field!r}")
    date_column = _date_sql(date_field)
    sql = ""
    params: dict = {}
    applied = {
        "machine": _validate_machine(conn, machine),
        "date_from": _validate_date(date_from, "date_from"),
        "date_to": _validate_date(date_to, "date_to"),
        "date_field": date_field,
        "jobtype": jobtype.strip().upper() if jobtype else None,
        "result": reports.result_code(result) or None,
        "final_only": bool(final_only),
    }
    if applied["machine"]:
        sql += " AND j.machine_id = :machine"
        params["machine"] = applied["machine"]
    if applied["date_from"]:
        sql += f" AND {date_column} >= :date_from"
        params["date_from"] = applied["date_from"]
    if applied["date_to"]:
        sql += f" AND {date_column} <= :date_to"
        params["date_to"] = applied["date_to"]
    if applied["jobtype"]:
        # Case-insensitive, as the TRM specifies for string constants.
        sql += " AND UPPER(j.jobtype) = :jobtype"
        params["jobtype"] = applied["jobtype"]
    if applied["result"]:
        # The log says 'Done', the grammar says DONE.
        sql += " AND UPPER(j.result) = :result"
        params["result"] = applied["result"]
    if final_only:
        # The running ACL day is re-read from scratch on every sync, so its rows
        # still change and vanish again.
        sql += " AND sl.source_type = 'CSV'"
    if after:
        # Row-value comparison, matching the ORDER BY below. Pagination, not a
        # watermark: a day synced late lands behind a cursor already handed out.
        sql += (
            " AND (j.machine_id, j.source_date, j.jobid, j.line_seq)"
            " > (:after_machine, :after_date, :after_jobid, :after_seq)"
        )
        (
            params["after_machine"],
            params["after_date"],
            params["after_jobid"],
            params["after_seq"],
        ) = decode_cursor(after)
    return sql, params, applied


def run_page(
    conn,
    machine=None,
    date_from=None,
    date_to=None,
    date_field="startdate",
    jobtype=None,
    result=None,
    final_only=True,
    media=True,
    raw=False,
    limit=DEFAULT_LIMIT,
    after=None,
) -> dict:
    """One page of print runs, ordered by the row identity.

    Returns the rows plus the cursor for the next page, or None on the last one.
    """
    limit = clamp_limit(limit)
    filters, params, applied = _filters(
        conn, machine, date_from, date_to, date_field, jobtype, result, final_only, after
    )
    columns = ",\n            ".join(f"j.{c}" for c in RUN_COLUMNS)
    params["limit"] = limit + 1  # one row of look-ahead: is there a next page?
    sql = f"""
        SELECT
            {columns},
            -- Unqualified: the click columns exist in `jobs` only.
            {reports.CLICKS} AS clicks,
            (SELECT COALESCE(SUM({reports.SHEETS}), 0) FROM job_media m
              WHERE m.machine_id = j.machine_id
                AND m.source_date = j.source_date
                AND m.jobid = j.jobid
                AND m.line_seq = j.line_seq) AS sheets,
            {completed_date_sql("j")} AS completed_date,
            sl.source_type AS source_type,
            sl.filename    AS source_file,
            j.raw_json     AS raw_json
        FROM jobs j
        -- machine_id belongs in this join: every press has its own log day, with
        -- its own finality.
        LEFT JOIN sync_log sl
               ON sl.machine_id = j.machine_id
              AND sl.source_date = j.source_date
        WHERE 1 = 1
          {filters}
        ORDER BY j.machine_id, j.source_date, j.jobid, j.line_seq
        LIMIT :limit
    """
    rows = [dict(r) for r in conn.execute(sql, params)]
    more = len(rows) > limit
    rows = rows[:limit]

    for row in rows:
        raw_json = row.pop("raw_json")
        # 'CSV' is the finished file, 'ACL' the live one.
        row["final"] = row["source_type"] == "CSV"
        row["runtime_s"] = reports.duration_s(
            row["startdate"], row["starttime"], row["readydate"], row["readytime"]
        )
        # Null when the completion time is unusable; completed_date still carries
        # the day.
        row["completed_at"] = (
            f"{row['completed_date']} {row['readytime']}"
            if reports.parse_stamp(row["completed_date"], row["readytime"])
            else None
        )
        if raw:
            row["raw"] = _raw(raw_json)
    if media:
        _attach_media(conn, rows)

    return {
        "count": len(rows),
        "limit": limit,
        "next": encode_cursor(rows[-1]) if more and rows else None,
        "filter": {**applied, "media": bool(media), "raw": bool(raw)},
        "runs": rows,
    }


def _raw(raw_json: str | None) -> dict:
    if not raw_json:
        return {}
    try:
        return json.loads(raw_json)
    except ValueError:
        # A row whose backup is unreadable still has its columns.
        return {}


def _attach_media(conn, rows: list[dict]) -> None:
    """The tray rows for one page, in one query.

    Row-value IN over the four-part identity, which leads the job_media primary
    key — an index lookup per row, and it cannot mix presses.
    """
    for row in rows:
        row["media"] = []
    if not rows:
        return
    keys = [(r["machine_id"], r["source_date"], r["jobid"], r["line_seq"]) for r in rows]
    placeholders = ", ".join(["(?, ?, ?, ?)"] * len(keys))
    columns = ", ".join(MEDIA_COLUMNS)
    sql = f"""
        SELECT machine_id, source_date, jobid, line_seq, {columns},
               {reports.SHEETS} AS sheets
        FROM job_media
        WHERE (machine_id, source_date, jobid, line_seq) IN (VALUES {placeholders})
        ORDER BY machine_id, source_date, jobid, line_seq, tray
    """
    by_key: dict[tuple, list[dict]] = {}
    for r in conn.execute(sql, [v for key in keys for v in key]):
        entry = dict(r)
        key = (
            entry.pop("machine_id"),
            entry.pop("source_date"),
            entry.pop("jobid"),
            entry.pop("line_seq"),
        )
        by_key.setdefault(key, []).append(entry)
    for row, key in zip(rows, keys, strict=True):
        row["media"] = by_key.get(key, [])


def day_page(conn, machine=None, date_from=None, date_to=None) -> dict:
    """What has been imported, per machine and log day.

    Tells "that press printed nothing" from "that day was never imported", which
    an empty run list cannot. `final` false means the day is still the live ACL.
    """
    applied = {
        "machine": _validate_machine(conn, machine),
        "date_from": _validate_date(date_from, "date_from"),
        "date_to": _validate_date(date_to, "date_to"),
    }
    sql = """
        SELECT machine_id, source_date, source_type, filename, last_synced, row_count
        FROM sync_log
        WHERE 1 = 1
    """
    params: dict = {}
    if applied["machine"]:
        sql += " AND machine_id = :machine"
        params["machine"] = applied["machine"]
    if applied["date_from"]:
        sql += " AND source_date >= :date_from"
        params["date_from"] = applied["date_from"]
    if applied["date_to"]:
        sql += " AND source_date <= :date_to"
        params["date_to"] = applied["date_to"]
    sql += " ORDER BY machine_id, source_date"
    rows = [{**dict(r), "final": r["source_type"] == "CSV"} for r in conn.execute(sql, params)]
    return {"count": len(rows), "filter": applied, "days": rows}
