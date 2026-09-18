"""Report queries over the job data.

clicks — printed pages, the billing figure.
sheets — nofsimplex + ceil(nofduplex / 2). nofduplex counts pages, not sheets.
proof  — heuristic, not reported by the machine. See _RUN_TYPE_SQL.
folds  — folded sheets, not folded products. See FINISHING.
"""

import json
from datetime import datetime

from . import config, jobkey

# clicks = all formats, colour and black/white combined.
CLICKS = (
    "(nofprinteda4bw + nofprinteda4c + nofprinteda3bw + nofprinteda3c"
    " + nofprintedXLbw + nofprintedXLc)"
)

# sheets from pages: integer division floors, (n+1)/2 gives ceil(n/2).
SHEETS = "(nofsimplex + (nofduplex + 1) / 2)"

# The printer does not report proofs: a single-copy run followed later by a
# multi-copy run of the same job is a release proof, anything else a genuine
# single copy.
_RUN_TYPE_SQL = """
    CASE
        WHEN j.noffinishedsets > 1 THEN 'Production'
        WHEN m.last_multi_ts IS NOT NULL AND j.ts < m.last_multi_ts THEN 'Proof'
        ELSE 'Single'
    END
"""


def _(message: str) -> str:
    """No-op marker so Babel finds msgids that are translated at render time.

    The templates call _(run.run_type), which Babel cannot see through; without
    these markers `pybabel update` drops the run types from the catalogue.
    """
    return message


RUN_TYPE_MSGIDS = (_("Production"), _("Proof"), _("Single"))

# Canon's grammar: COPY, IP, AP, SYSTEM, SCAN, SCAN2MBX, MBXCOPY. This press
# writes only the three below; an unknown type appears only under "all" so it
# cannot join the billed IP figures unnoticed.
JOBTYPES = ("IP", "SYSTEM", "AP")

# Canon's grammar: DONE finished, ABRT aborted, STOP halted but still queued.
# The machine writes 'Done' and the TRM makes string constants case-insensitive,
# so every comparison on `result` goes through UPPER(). The raw spelling stays in
# the database — a finalised CSV day is never re-read and would keep the old one.
RESULT_DONE = "DONE"
RESULT_LABELS = {
    "DONE": _("Done"),
    "ABRT": _("Aborted"),
    "STOP": _("Stopped"),
}


def result_code(value: str | None) -> str:
    return (value or "").strip().upper()


def result_label(value: str | None) -> str:
    """Translatable msgid for a result code — the raw value if it is unknown."""
    return RESULT_LABELS.get(result_code(value), (value or "").strip())


def ngettext(singular: str, plural: str) -> tuple[str, str]:
    """No-op plural marker, counterpart to _() above.

    Babel matches on the function name (`ngettext:1,2`), so these pairs land in
    the catalogue as real plural entries. Two single _() markers would not do:
    gettext looks plural forms up by the singular msgid, and a singular-only
    entry has none — German would render "1 Falzungen".
    """
    return singular, plural


# Finishing operations the machine reports, in display order, as
# (report key, column, label pair).
#
# noffolds counts folded SHEETS, not products: one booklet of 7 sheets reports 7.
# nofsinglestaples, nofpunches and nofcreases are 0 in all data so far and are
# carried along so they appear by themselves once those units get used.
FINISHING = (
    ("folds", "noffolds", ngettext("Fold", "Folds")),
    ("booklets", "nofbooklets", ngettext("Booklet", "Booklets")),
    ("stitches", "nofdoublestaples", ngettext("Saddle stitch", "Saddle stitches")),
    ("staples", "nofsinglestaples", ngettext("Staple", "Staples")),
    ("punches", "nofpunches", ngettext("Punch", "Punches")),
    ("creases", "nofcreases", ngettext("Crease", "Creases")),
)

_FINISHING_SUMS = ",\n            ".join(f"SUM({col}) AS {key}" for key, col, _label in FINISHING)


def finishing_items(row: dict) -> list[dict]:
    """Non-zero finishing counts of a row, keyed by the short keys of FINISHING."""
    items = []
    for key, _col, (one, many) in FINISHING:
        count = row.get(key) or 0
        if not count:
            continue
        # The machine counts the saddle stitch and the booklet separately; the
        # data and the API keep both, the display would read as a doubled figure.
        if key == "stitches" and count == (row.get("booklets") or 0):
            continue
        items.append({"key": key, "one": one, "many": many, "count": count})
    return items


# End of a run as a sortable 'YYYY-MM-DD HH:MM:SS' string. readydate is empty on
# rows that finished on their start day. Without readytime the start stamp stands
# in, so MAX() never picks a truncated 'date + space' over a real timestamp.
_END_TS = """CASE WHEN readytime <> ''
                  THEN COALESCE(NULLIF(readydate, ''), startdate) || ' ' || readytime
                  ELSE ts END"""

# Seconds occupied by one machine run; mirrors duration_s(). Incomplete or
# backwards timestamps contribute no time. strftime keeps the subtraction exact
# to the second — julianday arithmetic can be off by one through float error.
_RUNTIME_S = """
    CASE
        WHEN startdate <> '' AND starttime <> '' AND readytime <> ''
         AND (COALESCE(NULLIF(readydate, ''), startdate) || ' ' || readytime)
             >= (startdate || ' ' || starttime)
        THEN CAST(
            strftime('%s', COALESCE(NULLIF(readydate, ''), startdate) || ' ' || readytime)
            - strftime('%s', startdate || ' ' || starttime)
            AS INTEGER
        )
        ELSE NULL
    END
"""

# job_key is derived here and nowhere else; it is not a column of `jobs`. Without
# a pattern it is the row identity, with one whatever the regex captures — a
# grouping key, not necessarily a number. See jobkey.py.
_BASE_CTE_TEMPLATE = f"""
WITH j AS (
    SELECT
        jobs.*,
        {{key}} AS job_key,
        {CLICKS} AS clicks,
        (startdate || ' ' || starttime) AS ts
    FROM jobs
    WHERE 1 = 1
      {{filters}}
),
multi AS (
    SELECT job_key, MAX(ts) AS last_multi_ts
    FROM j
    WHERE noffinishedsets > 1 AND job_key IS NOT NULL
    GROUP BY job_key
),
-- Result of the chronologically last run of a job: a STOP or ABRT in between is
-- a pause or a retry that was printed again afterwards. Ordered like job_detail().
last_run AS (
    SELECT job_key, result AS last_result FROM (
        SELECT job_key, result,
               ROW_NUMBER() OVER (PARTITION BY job_key
                                  ORDER BY startdate DESC, starttime DESC,
                                           line_seq DESC) AS rn
        FROM j
        WHERE job_key IS NOT NULL
    ) WHERE rn = 1
),
sheets_cte AS (
    SELECT machine_id, source_date, jobid, line_seq, SUM({SHEETS}) AS sheets
    FROM job_media
    GROUP BY machine_id, source_date, jobid, line_seq
),
cls AS (
    SELECT
        j.*,
        COALESCE(s.sheets, 0) AS sheets,
        lr.last_result,
        {_RUN_TYPE_SQL} AS run_type
    FROM j
    LEFT JOIN multi m ON m.job_key = j.job_key
    LEFT JOIN last_run lr ON lr.job_key = j.job_key
    -- Row identity, so machine_id belongs in: the same jobid on the same day is
    -- a different run on another press. The joins above are on job_key alone —
    -- an order is one order across machines.
    LEFT JOIN sheets_cte s
           ON s.machine_id = j.machine_id
          AND s.source_date = j.source_date
          AND s.jobid = j.jobid
          AND s.line_seq = j.line_seq
)
"""


def _base_cte(filters: str) -> str:
    """The CTE with the filters and the grouping key filled in.

    A function, not a constant: the key expression comes from the configuration
    and must not be frozen at import time.
    """
    return _BASE_CTE_TEMPLATE.format(key=jobkey.key_sql(), filters=filters)


def _filters(
    date_from: str | None,
    date_to: str | None,
    q: str | None,
    jobtype: str | None,
    result: str | None,
    exclude_jobtype: str | None = None,
    machine: str | None = None,
    alias: str = "",
) -> tuple[str, dict]:
    """Build WHERE fragments. Dates filter on startdate, not source_date —
    otherwise the live ACL day shifts the range boundaries.

    `alias` qualifies the columns; required wherever `jobs` is joined against
    `job_media`, whose machine_id, source_date, jobid and line_seq collide.
    """
    p = f"{alias}." if alias else ""
    sql = ""
    params: dict = {}
    if machine:
        # A row filter, like jobtype: filtered, the job list shows this press's
        # share of a job that ran on two.
        sql += f" AND {p}machine_id = :machine"
        params["machine"] = machine
    if date_from:
        sql += f" AND {p}startdate >= :date_from"
        params["date_from"] = date_from
    if date_to:
        sql += f" AND {p}startdate <= :date_to"
        params["date_to"] = date_to
    if jobtype:
        sql += f" AND {p}jobtype = :jobtype"
        params["jobtype"] = jobtype
    if exclude_jobtype:
        sql += f" AND {p}jobtype <> :exclude_jobtype"
        params["exclude_jobtype"] = exclude_jobtype
    if result:
        # Case-insensitive: the log says 'Done', the grammar says DONE.
        sql += f" AND UPPER({p}result) = :result"
        params["result"] = result_code(result)
    if q:
        sql += f" AND ({p}jobname LIKE :q OR COALESCE({jobkey.key_sql(alias)}, '') LIKE :q)"
        params["q"] = f"%{q}%"
    return sql, params


def job_list(
    conn,
    date_from=None,
    date_to=None,
    q=None,
    jobtype="IP",
    result=None,
    sort="last_day",
    desc=True,
    machine=None,
) -> list[dict]:
    """One record per job key, summed across days and across machines."""
    filters, params = _filters(date_from, date_to, q, jobtype, result, machine=machine)
    sql = (
        _base_cte(filters)
        + f"""
        SELECT
            job_key,
            COUNT(*)                                     AS rows,
            SUM(noffinishedsets)                         AS sets,
            SUM(clicks)                                  AS clicks,
            SUM(nofprinteda4bw)                          AS a4bw,
            SUM(nofprinteda4c)                           AS a4c,
            SUM(nofprinteda3bw)                          AS a3bw,
            SUM(nofprinteda3c)                           AS a3c,
            SUM(nofprintedXLbw)                          AS xlbw,
            SUM(nofprintedXLc)                           AS xlc,
            SUM(sheets)                                  AS sheets,
            SUM({_RUNTIME_S})                             AS runtime_s,
            {_FINISHING_SUMS},
            MIN(startdate)                               AS first_day,
            MAX(startdate)                               AS last_day,
            COUNT(DISTINCT startdate)                    AS days,
            MIN(ts)                                      AS first_ts,
            MAX({_END_TS})                               AS last_ts,
            SUM(run_type = 'Proof')                      AS proofs,
            SUM(CASE WHEN run_type = 'Proof' THEN clicks ELSE 0 END) AS proof_clicks,
            SUM(UPPER(result) <> '{RESULT_DONE}')        AS not_done,
            MAX(last_result)                             AS last_result,
            json_group_array(DISTINCT jobname)           AS jobnames_raw,
            json_group_array(DISTINCT machine_id)        AS machines_raw
        FROM cls
        WHERE job_key IS NOT NULL
        GROUP BY job_key
    """
    )
    rows = [dict(r) for r in conn.execute(sql, params)]

    for r in rows:
        # A JSON array, not GROUP_CONCAT: a job name is a file name and can
        # contain any separator.
        r["jobnames"] = [n for n in json.loads(r.pop("jobnames_raw")) if n]
        r["machines"] = sorted(m for m in json.loads(r.pop("machines_raw")) if m)
        r["jobname"] = r["jobnames"][0] if r["jobnames"] else ""
        # Without a grouping rule job_key is the row identity — a link target,
        # not a readable label, so the filename is shown instead.
        r["job_label"] = r["job_key"] if jobkey.grouping_enabled() else r["jobname"]
        # The *last* run decides: an ABRT or STOP followed by a successful run is
        # a complete job. not_done keeps the raw count for the tooltip and the API.
        r["unfinished"] = result_code(r["last_result"]) != RESULT_DONE
        r["result_label"] = result_label(r["last_result"])
        r["clicks_bw"] = (r["a4bw"] or 0) + (r["a3bw"] or 0) + (r["xlbw"] or 0)
        r["clicks_colour"] = (r["a4c"] or 0) + (r["a3c"] or 0) + (r["xlc"] or 0)
        r["proof_ratio"] = round(100 * r["proof_clicks"] / r["clicks"], 1) if r["clicks"] else 0.0

    allowed = {
        "job_key",
        "clicks",
        "clicks_bw",
        "clicks_colour",
        "sets",
        "sheets",
        "runtime_s",
        "rows",
        "proofs",
        "proof_ratio",
        "first_day",
        "last_day",
    }
    key = sort if sort in allowed else "last_day"
    # Sort the two date keys on the timestamp instead: same order between days,
    # but two jobs of the same day no longer land in arbitrary order.
    key = {"first_day": "first_ts", "last_day": "last_ts"}.get(key, key)
    rows.sort(key=lambda r: (r[key] is None, r[key]), reverse=desc)
    return rows


def job_detail(conn, job_key: str) -> dict | None:
    """All rows of a job chronologically, with media and key figures."""
    filters, params = _filters(None, None, None, None, None)
    params["job_key"] = job_key
    sql = (
        _base_cte(filters)
        + """
        SELECT * FROM cls
        WHERE job_key = :job_key
        ORDER BY startdate, starttime, line_seq
    """
    )
    rows = [dict(r) for r in conn.execute(sql, params)]
    if not rows:
        return None

    media = _media_by_line(conn, job_key)

    prev_end = None
    for run in rows:
        key = (run["machine_id"], run["source_date"], run["jobid"], run["line_seq"])
        run["media"] = media.get(key, [])
        # Short finishing keys, so finishing_items() also works per run.
        run.update({k: run[col] or 0 for k, col, _label in FINISHING})
        run["done"] = result_code(run["result"]) == RESULT_DONE
        run["result_label"] = result_label(run["result"])
        run["runtime_s"] = duration_s(
            run["startdate"], run["starttime"], run["readydate"], run["readytime"]
        )
        start = parse_stamp(run["startdate"], run["starttime"])
        run["pause_s"] = (
            int((start - prev_end).total_seconds())
            if start and prev_end and start >= prev_end
            else None
        )
        end = parse_stamp(run["readydate"], run["readytime"])
        prev_end = end or prev_end
        try:
            run["raw"] = json.loads(run["raw_json"]) if run["raw_json"] else {}
        except (json.JSONDecodeError, TypeError):
            run["raw"] = {}

    def total(field):
        return sum(run[field] or 0 for run in rows)

    _run_type_css = {"Production": "production", "Proof": "proof", "Single": "single"}
    for run in rows:
        run["run_type_css"] = _run_type_css.get(run["run_type"], run["run_type"].lower())

    proofs = [run for run in rows if run["run_type"] == "Proof"]
    production = [run for run in rows if run["run_type"] == "Production"]
    singles = [run for run in rows if run["run_type"] == "Single"]

    clicks = total("clicks")
    return {
        "job_key": job_key,
        "job_label": (job_key if jobkey.grouping_enabled() else rows[0]["jobname"]),
        "jobname": rows[0]["jobname"],
        "rows": rows,
        "clicks": clicks,
        "sets": total("noffinishedsets"),
        "sheets": total("sheets"),
        **{key: total(col) for key, col, _label in FINISHING},
        "a4bw": total("nofprinteda4bw"),
        "a4c": total("nofprinteda4c"),
        "a3bw": total("nofprinteda3bw"),
        "a3c": total("nofprinteda3c"),
        "xlbw": total("nofprintedXLbw"),
        "xlc": total("nofprintedXLc"),
        "first_day": min(run["startdate"] for run in rows),
        "last_day": max(run["startdate"] for run in rows),
        "days": sorted({run["startdate"] for run in rows}),
        "proofs": len(proofs),
        "proof_clicks": sum(run["clicks"] for run in proofs),
        "proof_ratio": (
            round(100 * sum(run["clicks"] for run in proofs) / clicks, 1) if clicks else 0.0
        ),
        "production_runs": len(production),
        "singles": len(singles),
        "runtime_s": sum(run["runtime_s"] or 0 for run in rows),
        "material": _material_summary(conn, job_key),
        "by_machine": _by_machine(rows),
    }


def _by_machine(rows: list[dict]) -> list[dict]:
    """Per-press figures of one job, summed from the rows already loaded."""
    out: dict[str, dict] = {}
    for run in rows:
        entry = out.setdefault(
            run["machine_id"],
            {
                "machine_id": run["machine_id"],
                "runs": 0,
                "clicks": 0,
                "sheets": 0,
                "sets": 0,
                "runtime_s": 0,
                "first_ts": run["ts"],
                "last_day": run["startdate"],
            },
        )
        entry["runs"] += 1
        entry["clicks"] += run["clicks"] or 0
        entry["sheets"] += run["sheets"] or 0
        entry["sets"] += run["noffinishedsets"] or 0
        entry["runtime_s"] += run["runtime_s"] or 0
        entry["first_ts"] = min(entry["first_ts"], run["ts"])
        entry["last_day"] = max(entry["last_day"], run["startdate"])
    return sorted(out.values(), key=lambda e: e["machine_id"])


def _media_by_line(conn, job_key: str) -> dict:
    sql = f"""
        SELECT m.*
        FROM job_media m
        JOIN jobs j ON j.machine_id = m.machine_id
                   AND j.source_date = m.source_date
                   AND j.jobid = m.jobid
                   AND j.line_seq = m.line_seq
        WHERE {jobkey.key_sql("j")} = :job_key
        ORDER BY m.tray
    """
    out: dict = {}
    for r in conn.execute(sql, {"job_key": job_key}):
        d = dict(r)
        d["sheets"] = (d["nofsimplex"] or 0) + ((d["nofduplex"] or 0) + 1) // 2
        out.setdefault((d["machine_id"], d["source_date"], d["jobid"], d["line_seq"]), []).append(d)
    return out


def _material_summary(conn, job_key: str) -> list[dict]:
    """Sheet usage per medium — also captures tray 2+ (e.g. covers)."""
    sql = f"""
        SELECT
            m.medianame, m.mediaweight, m.mediaformat, m.mediatype,
            SUM(m.nofsimplex) AS simplex,
            SUM(m.nofduplex)  AS duplex,
            SUM({SHEETS})     AS sheets
        FROM job_media m
        JOIN jobs j ON j.machine_id = m.machine_id
                   AND j.source_date = m.source_date
                   AND j.jobid = m.jobid
                   AND j.line_seq = m.line_seq
        WHERE {jobkey.key_sql("j")} = :job_key
        GROUP BY m.medianame, m.mediaweight, m.mediaformat, m.mediatype
        ORDER BY sheets DESC
    """
    return [dict(r) for r in conn.execute(sql, {"job_key": job_key})]


PAPER_SORT = frozenset(
    {
        "medianame",
        "mediaformat",
        "mediaweight",
        "mediacolor",
        "sheets",
        "simplex",
        "duplex",
        "job_count",
        "rows",
        "first_day",
        "last_day",
    }
)


def paper_sort_key(sort: str | None) -> str:
    """Valid sort column, or the default."""
    return sort if sort in PAPER_SORT else "sheets"


def paper_usage(
    conn, date_from=None, date_to=None, q=None, jobtype=None, sort="sheets", desc=True, machine=None
) -> list[dict]:
    """Sheet usage per paper grade, summed over the date range.

    Grouped on (medianame, mediaformat, mediaweight, mediacolor): the name is
    free text and empty for trays outside the machine's media catalogue, the
    format alone would merge different grades.

    `q` searches the medium here, not the jobname. All job types count by
    default — calibration and AP prints consume stock too.
    """
    # q is handled separately below. Aliased: this query joins jobs against
    # job_media, where the key columns exist twice.
    filters, params = _filters(date_from, date_to, None, jobtype, None, machine=machine, alias="j")
    if q:
        filters += " AND (m.medianame LIKE :mq OR m.mediaformat LIKE :mq)"
        params["mq"] = f"%{q}%"

    # Date range via jobs.startdate — job_media has no date of its own.
    sql = f"""
        SELECT
            m.medianame, m.mediaformat, m.mediaweight, m.mediacolor,
            SUM(m.nofsimplex)        AS simplex,
            SUM(m.nofduplex)         AS duplex,
            SUM({SHEETS})            AS sheets,
            COUNT(*)                 AS rows,
            -- Aliased, the key columns exist in job_media too. Counts a job on
            -- two presses once.
            COUNT(DISTINCT {jobkey.key_sql("j")}) AS job_count,
            MIN(j.startdate)         AS first_day,
            MAX(j.startdate)         AS last_day
        FROM job_media m
        -- machine_id belongs in every join over the row identity: jobid and
        -- line_seq are machine-local, so without it the sheets fan out over
        -- every press.
        JOIN jobs j ON j.machine_id = m.machine_id
                   AND j.source_date = m.source_date
                   AND j.jobid      = m.jobid
                   AND j.line_seq   = m.line_seq
        WHERE 1 = 1 {filters}
        GROUP BY m.medianame, m.mediaformat, m.mediaweight, m.mediacolor
        HAVING sheets > 0
    """
    rows = [dict(r) for r in conn.execute(sql, params)]

    key = paper_sort_key(sort)
    rows.sort(key=lambda r: (r[key] is None, r[key]), reverse=desc)
    return rows


def unassigned_runs(
    conn, date_from=None, date_to=None, q=None, jobtype=None, exclude_jobtype=None, machine=None
) -> list[dict]:
    """Runs the grouping rule gives no key, grouped by filename.

    SYSTEM runs carry no key either, legitimately — the caller excludes them and
    /own-use reports them instead. Always empty without a configured pattern: the
    key is then the row identity and never NULL.
    """
    filters, params = _filters(
        date_from, date_to, q, jobtype, None, exclude_jobtype, machine=machine
    )
    sql = (
        _base_cte(filters)
        + f"""
        SELECT
            machine_id, jobname, jobtype, username,
            COUNT(*)                      AS rows,
            SUM(noffinishedsets)          AS sets,
            SUM(clicks)                   AS clicks,
            SUM(sheets)                   AS sheets,
            SUM({_RUNTIME_S})             AS runtime_s,
            SUM(run_type = 'Proof')       AS proofs,
            {_FINISHING_SUMS},
            MIN(startdate)                AS first_day,
            MAX(startdate)                AS last_day
        FROM cls
        WHERE job_key IS NULL
        -- Per machine as well: an unnamed file on two presses is two findings.
        GROUP BY machine_id, jobname, jobtype, username
        ORDER BY last_day DESC, clicks DESC
    """
    )
    return [dict(r) for r in conn.execute(sql, params)]


# -- Own use ------------------------------------------------------------------
# What the press prints for itself: calibration, service runs, meter reports.
# Selected by jobtype: a misnamed customer file has no grouping key either, and
# that belongs on /unassigned.

SYSTEM_JOBTYPE = "SYSTEM"


def own_use(conn, date_from=None, date_to=None, q=None, machine=None) -> list[dict]:
    """The machine's own runs, grouped by kind — the file name is the kind.

    The press writes a fixed handful of names ('Service Job', 'Spot Color Patch
    Chart', 'Abrechnungszählerbericht'). Per machine: how often a press
    calibrates is a statement about that press.
    """
    filters, params = _filters(date_from, date_to, q, SYSTEM_JOBTYPE, None, machine=machine)
    sql = (
        _base_cte(filters)
        + """
        SELECT
            machine_id, jobname,
            COUNT(*)                  AS runs,
            SUM(clicks)               AS clicks,
            SUM(sheets)               AS sheets,
            COUNT(DISTINCT startdate) AS days,
            MIN(startdate)            AS first_day,
            MAX(startdate)            AS last_day,
            MAX(ts)                   AS last_ts
        FROM cls
        GROUP BY machine_id, jobname
        ORDER BY runs DESC, clicks DESC
    """
    )
    return [dict(r) for r in conn.execute(sql, params)]


def own_use_months(conn, date_from=None, date_to=None, q=None, machine=None) -> list[dict]:
    """Own use per calendar month, newest first.

    Summed over all presses unless one is filtered.
    """
    filters, params = _filters(date_from, date_to, q, SYSTEM_JOBTYPE, None, machine=machine)
    sql = (
        _base_cte(filters)
        + """
        SELECT
            substr(startdate, 1, 7) AS month,
            COUNT(*)                AS runs,
            SUM(clicks)             AS clicks,
            SUM(sheets)             AS sheets
        FROM cls
        WHERE startdate <> ''
        GROUP BY month
        ORDER BY month DESC
    """
    )
    return [dict(r) for r in conn.execute(sql, params)]


def sync_status(conn, machine_id: str) -> dict:
    """State of ONE press.

    ok=0 means the file listing could not be fetched — most often a press that
    was off, which is not an error state. `error` carries the short reason.
    """
    last = conn.execute(
        "SELECT * FROM sync_run WHERE machine_id = ? ORDER BY id DESC LIMIT 1",
        (machine_id,),
    ).fetchone()
    last_ok = conn.execute(
        "SELECT * FROM sync_run WHERE machine_id = ? AND ok = 1 ORDER BY id DESC LIMIT 1",
        (machine_id,),
    ).fetchone()
    days = conn.execute(
        """SELECT source_date, source_type, filename, last_synced, row_count
           FROM sync_log WHERE machine_id = ? ORDER BY source_date DESC""",
        (machine_id,),
    ).fetchall()
    return {
        "machine_id": machine_id,
        "last_attempt": dict(last) if last else None,
        "last_success": dict(last_ok) if last_ok else None,
        "online": bool(last and last["ok"]),
        "days": [dict(r) for r in days],
    }


def sync_status_all(conn) -> list[dict]:
    """One status per configured machine, in configuration order.

    Configuration order, not what the database holds: a press that has never
    synced must still appear.
    """
    return [sync_status(conn, m.id) for m in config.get().machines]


def sync_history(conn, machine_id: str | None = None, limit: int = 50) -> list[dict]:
    sql = "SELECT * FROM sync_run"
    params: dict = {"limit": limit}
    if machine_id:
        sql += " WHERE machine_id = :machine_id"
        params["machine_id"] = machine_id
    sql += " ORDER BY id DESC LIMIT :limit"
    return [dict(r) for r in conn.execute(sql, params)]


def date_limits(conn) -> tuple[str | None, str | None]:
    row = conn.execute(
        "SELECT MIN(startdate) AS date_from, MAX(startdate) AS date_to"
        " FROM jobs WHERE startdate <> ''"
    ).fetchone()
    return (row["date_from"], row["date_to"]) if row else (None, None)


# -- Time calculations -------------------------------------------------------
# The printer does not populate activetime/idletime (always empty), so run
# and pause durations are derived from startdate/starttime and readydate/readytime.


def parse_stamp(d: str | None, t: str | None) -> datetime | None:
    """A date and clock field of the log as one timestamp, None if unusable."""
    if not d or not t:
        return None
    try:
        return datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def duration_s(startdate, starttime, readydate, readytime) -> int | None:
    """Seconds one machine run occupied, None where the stamps do not allow it.

    Serves /job/<job_key> and /api/v1/runs alike; _RUNTIME_S mirrors it in SQL.
    """
    start = parse_stamp(startdate, starttime)
    end = parse_stamp(readydate or startdate, readytime)
    if not start or not end or end < start:
        return None
    return int((end - start).total_seconds())


def fmt_duration(seconds: int | None) -> str:
    """Seconds as compact duration: 25:39 or 1:05:12, -0:15:00 for a negative.

    The sign is split off before the divmod, which would render -900 as -1:45:00.
    """
    if seconds is None:
        return "—"
    seconds = int(seconds)
    sign = "-" if seconds < 0 else ""
    hours, rest = divmod(abs(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours or sign:
        return f"{sign}{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
