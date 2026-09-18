"""The export API: /api/v1/*, token-protected, read only.

Three routes: the presses, the imported days, the print runs. Flat rows — one
object per row of the machine's log, with its own field names.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import export
from .deps import db, require_token

# On the router, so a route added here cannot forget the token.
router = APIRouter(prefix="/api/v1", tags=["export"], dependencies=[Depends(require_token)])


@router.get("/machines")
def machines(conn=Depends(db)):
    """Every press this database can answer for. `id` is what every row carries.

    Presses that are no longer configured but still have data are listed with
    `configured: false` and `name` null. The `machine` filter accepts exactly
    this set.
    """
    rows = export.known_machines(conn)
    return {"count": len(rows), "machines": rows}


@router.get("/days")
def days(conn=Depends(db), machine: str = "", date_from: str = "", date_to: str = ""):
    """Imported log days per press.

    An empty `/runs` answer means either "nothing printed" or "nothing imported
    yet", and only this list tells the two apart. `final` false is the running
    day: it is re-read from scratch on every sync, so its rows still change.
    """
    try:
        return export.day_page(
            conn, machine=machine or None, date_from=date_from or None, date_to=date_to or None
        )
    except export.BadRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/runs")
def runs(
    conn=Depends(db),
    machine: str = "",
    date_from: str = "",
    date_to: str = "",
    date_field: str = "startdate",
    jobtype: str = "",
    result: str = "",
    final_only: bool = True,
    media: bool = True,
    raw: bool = False,
    limit: int = Query(default=export.DEFAULT_LIMIT, ge=1),
    after: str = "",
):
    """Print runs, one object per row of the machine's log.

    Ordered by `(machine_id, source_date, jobid, line_seq)`; `next` is the
    cursor for the following page, or null on the last one. `filter` echoes what
    was applied: an unknown machine or an unparseable date is a 400, a `jobtype`
    this press never writes is not.
    """
    try:
        return export.run_page(
            conn,
            machine=machine or None,
            date_from=date_from or None,
            date_to=date_to or None,
            date_field=date_field,
            jobtype=jobtype or None,
            result=result or None,
            final_only=final_only,
            media=media,
            raw=raw,
            limit=limit,
            after=after or None,
        )
    except export.BadRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
