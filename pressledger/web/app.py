import logging
import math
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from babel.support import NullTranslations, Translations
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import config, jobkey, reports
from ..db import ensure_schema, get_conn
from ..scheduler import run_sync_once, start_scheduler, stop_scheduler
from .api import router as api_router
from .deps import db

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
LOCALES_DIR = HERE.parent / "locales"
BAND_MAX_PX = 56

_current_lang: ContextVar[str] = ContextVar("lang", default="en")


def _load_translations() -> dict[str, NullTranslations]:
    out: dict[str, NullTranslations] = {}
    for lang in config.SUPPORTED_LANGS:
        try:
            out[lang] = Translations.load(str(LOCALES_DIR), [lang])
        except Exception:
            out[lang] = NullTranslations()
    return out


_translations: dict[str, NullTranslations] = {}


def _gettext(s: str) -> str:
    t = _translations.get(_current_lang.get())
    return t.gettext(s) if t else s


def _ngettext(singular: str, plural: str, n: int) -> str:
    t = _translations.get(_current_lang.get())
    return t.ngettext(singular, plural, n) if t else (singular if n == 1 else plural)


def _detect_lang(request: Request) -> str:
    lang = request.cookies.get("lang", "")
    if lang in config.SUPPORTED_LANGS:
        return lang
    for part in request.headers.get("Accept-Language", "").split(","):
        code = part.strip().split(";")[0].split("-")[0].lower()
        if code in config.SUPPORTED_LANGS:
            return code
    return config.get().default_lang


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _translations
    _translations = _load_translations()
    conn = get_conn()
    try:
        ensure_schema(conn)
    finally:
        conn.close()
    scheduler = start_scheduler()
    try:
        yield
    finally:
        stop_scheduler(scheduler)


app = FastAPI(title="PressLedger", lifespan=lifespan)
# /api/v1 exports ungrouped runs; /api serves the UI reports.
app.include_router(api_router)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.add_extension("jinja2.ext.i18n")
templates.env.install_gettext_callables(_gettext, _ngettext, newstyle=True)


def _number(value) -> str:
    if value is None:
        return "—"
    lang = _current_lang.get()
    if isinstance(value, float):
        if lang == "de":
            return f"{value:,.1f}".replace(",", "\u2009").replace(".", ",").replace("\u2009", ".")
        return f"{value:,.1f}"
    if lang == "de":
        return f"{int(value):,}".replace(",", ".")
    return f"{int(value):,}"


def _fmt_date(iso: str | None) -> str:
    from datetime import date as _date

    if not iso:
        return "—"
    try:
        d = _date.fromisoformat(iso)
    except ValueError:
        return iso
    if _current_lang.get() == "de":
        return d.strftime("%d.%m.%Y")
    # Not "%-d": that flag is glibc/macOS-only and raises on Windows.
    return f"{d:%b} {d.day}, {d:%Y}"


def _fmt_timestamp(iso: str | None) -> str:
    from datetime import datetime

    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    dt = dt.astimezone()
    if _current_lang.get() == "de":
        return dt.strftime("%d.%m. %H:%M")
    return f"{dt:%b} {dt.day}, {dt:%H:%M}"


def _fmt_clock(stamp: str | None) -> str:
    """'YYYY-MM-DD HH:MM:SS' (or a bare 'HH:MM:SS') → 'HH:MM'.

    Machine local time as the printer wrote it, so no timezone conversion —
    unlike _fmt_timestamp, which formats our own UTC sync stamps.
    """
    if not stamp:
        return "—"
    clock = stamp.split(" ")[-1]
    return clock[:5] if len(clock) >= 5 else stamp


def _path_segment(value) -> str:
    """Encode a grouping key as one URL path segment.

    safe="" also encodes slashes, which ASGI decodes again before routing — so
    the route needs a :path converter.
    """
    return quote(str(value or ""), safe="")


templates.env.filters["number"] = _number
templates.env.filters["path_segment"] = _path_segment
templates.env.filters["duration"] = reports.fmt_duration
templates.env.filters["date"] = _fmt_date
templates.env.filters["timestamp"] = _fmt_timestamp
templates.env.filters["clock"] = _fmt_clock
# Returns msgids; the template translates them via _(item.label).
templates.env.filters["finishing"] = reports.finishing_items


@app.middleware("http")
async def locale_middleware(request: Request, call_next):
    lang = _detect_lang(request)
    token = _current_lang.set(lang)
    request.state.lang = lang
    try:
        response = await call_next(request)
    finally:
        _current_lang.reset(token)
    return response


def _safe_redirect(target: str, fallback: str) -> str:
    """Same-origin paths only.

    A leading '//' is protocol-relative and would leave the origin, so
    startswith('/') alone is not enough.
    """
    return target if target.startswith("/") and not target.startswith("//") else fallback


def _back_to_jobs(request: Request) -> str:
    """The job list the visitor came from, with its filters, as a path.

    The referer is an absolute URL, so the host is compared as well; anything
    else falls back to the unfiltered list.
    """
    parts = urlsplit(request.headers.get("referer") or "")
    if parts.netloc != request.url.netloc or not parts.path.startswith("/jobs"):
        return "/jobs"
    return f"{parts.path}?{parts.query}" if parts.query else parts.path


def base_ctx(request: Request, conn, nav: str, machine: str = "") -> dict:
    settings = config.get()
    return {
        "request": request,
        "nav": nav,
        "lang": request.state.lang,
        "machines": settings.machines,
        "multi_machine": settings.multi_machine,
        "machine": machine,
        "status": reports.sync_status_all(conn),
        "custom_css": bool(settings.custom_css),
        "site_name": settings.site_name,
        # Without a grouping rule /unassigned is always empty, so it is hidden.
        "grouping": jobkey.grouping_enabled(),
    }


def _bar_scale(max_clicks: int):
    """Width of the size bar in px."""

    def width(clicks: int) -> int:
        if not max_clicks or not clicks:
            return 2
        return max(2, round(62 * clicks / max_clicks))

    return width


def _band_heights(rows: list[dict]) -> None:
    """Height of timeline markers. Square-root scale, so a large run does not
    make a small one invisible."""
    high = max((row["clicks"] or 0) for row in rows) or 1
    for row in rows:
        share = math.sqrt((row["clicks"] or 0) / high)
        row["band_h"] = max(6, round(BAND_MAX_PX * share))


def _sort_url_factory(base: str, params: dict, sort: str, desc: bool):
    def sort_url(col: str) -> str:
        q = {k: v for k, v in params.items() if v not in (None, "")}
        q["sort"] = col
        q["desc"] = "0" if (col == sort and desc) else "1"
        return f"{base}?{urlencode(q)}"

    return sort_url


def _machine_param(value: str) -> str:
    """Machine filter out of the query string; an unconfigured id is dropped."""
    return value if value and config.get().machine(value) else ""


def _list_ctx(
    request: Request, conn, q, date_from, date_to, jobtype, sort, desc, machine=""
) -> dict:
    jobs = reports.job_list(
        conn,
        date_from=date_from or None,
        date_to=date_to or None,
        q=q or None,
        jobtype=jobtype or None,
        sort=sort,
        desc=desc,
        machine=machine or None,
    )
    params = {
        "q": q,
        "date_from": date_from,
        "date_to": date_to,
        "jobtype": jobtype,
        "machine": machine,
    }
    max_clicks = max((j["clicks"] or 0) for j in jobs) if jobs else 0
    return {
        "jobs": jobs,
        "totals": {
            "clicks": sum(j["clicks"] or 0 for j in jobs),
            "sets": sum(j["sets"] or 0 for j in jobs),
            "sheets": sum(j["sheets"] or 0 for j in jobs),
            "runtime_s": sum(j["runtime_s"] or 0 for j in jobs),
            "proofs": sum(j["proofs"] or 0 for j in jobs),
            # Over the listed jobs, so these follow the filter.
            "folds": sum(j["folds"] or 0 for j in jobs),
            "booklets": sum(j["booklets"] or 0 for j in jobs),
        },
        "q": q,
        "date_from": date_from,
        "date_to": date_to,
        "jobtype": jobtype,
        "machine": machine,
        "sort": sort,
        "desc": desc,
        "limits": reports.date_limits(conn),
        "bar": _bar_scale(max_clicks),
        "sort_url": _sort_url_factory("/jobs/table", params, sort, desc),
    }


def _paper_ctx(
    request: Request, conn, q, date_from, date_to, jobtype, sort, desc, machine=""
) -> dict:
    sort = reports.paper_sort_key(sort)
    grades = reports.paper_usage(
        conn,
        date_from=date_from or None,
        date_to=date_to or None,
        q=q or None,
        jobtype=jobtype or None,
        sort=sort,
        desc=desc,
        machine=machine or None,
    )
    params = {
        "q": q,
        "date_from": date_from,
        "date_to": date_to,
        "jobtype": jobtype,
        "machine": machine,
    }
    max_sheets = max((s["sheets"] or 0) for s in grades) if grades else 0
    return {
        "grades": grades,
        "totals": {
            "sheets": sum(s["sheets"] or 0 for s in grades),
            "simplex": sum(s["simplex"] or 0 for s in grades),
            "duplex": sum(s["duplex"] or 0 for s in grades),
            "rows": sum(s["rows"] or 0 for s in grades),
        },
        "q": q,
        "date_from": date_from,
        "date_to": date_to,
        "jobtype": jobtype,
        "machine": machine,
        "sort": sort,
        "desc": desc,
        "limits": reports.date_limits(conn),
        "bar": _bar_scale(max_sheets),
        "sort_url": _sort_url_factory("/paper/table", params, sort, desc),
    }


@app.get("/custom.css", include_in_schema=False)
def custom_css():
    # Already absolute — config._resolve() did that at load time.
    path = config.get().custom_css
    if path is None or not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="text/css")


@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse("/jobs", status_code=307)


@app.post("/set-lang", include_in_schema=False)
def set_lang(lang: str = Form(...), redirect_to: str = Form(default="/")):
    lang = lang if lang in config.SUPPORTED_LANGS else config.get().default_lang
    response = RedirectResponse(_safe_redirect(redirect_to, "/"), status_code=303)
    response.set_cookie("lang", lang, max_age=365 * 24 * 3600, httponly=True)
    return response


@app.get("/jobs", response_class=HTMLResponse)
def jobs(
    request: Request,
    conn=Depends(db),
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    jobtype: str = "IP",
    sort: str = "last_day",
    desc: int = 1,
    machine: str = "",
):
    machine = _machine_param(machine)
    ctx = base_ctx(request, conn, "jobs", machine)
    ctx.update(_list_ctx(request, conn, q, date_from, date_to, jobtype, sort, bool(desc), machine))
    return templates.TemplateResponse(request=request, name="jobs.html", context=ctx)


@app.get("/jobs/table", response_class=HTMLResponse)
def jobs_table(
    request: Request,
    conn=Depends(db),
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    jobtype: str = "IP",
    sort: str = "last_day",
    desc: int = 1,
    machine: str = "",
):
    """htmx partial: only the table is swapped, not the full page."""
    ctx = {
        "request": request,
        "lang": request.state.lang,
        "multi_machine": config.get().multi_machine,
    }
    ctx.update(
        _list_ctx(
            request, conn, q, date_from, date_to, jobtype, sort, bool(desc), _machine_param(machine)
        )
    )
    return templates.TemplateResponse(request=request, name="_table.html", context=ctx)


@app.get("/paper", response_class=HTMLResponse)
def paper(
    request: Request,
    conn=Depends(db),
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    jobtype: str = "",
    sort: str = "sheets",
    desc: int = 1,
    machine: str = "",
):
    """Paper usage per grade, all job types by default."""
    machine = _machine_param(machine)
    ctx = base_ctx(request, conn, "paper", machine)
    ctx.update(_paper_ctx(request, conn, q, date_from, date_to, jobtype, sort, bool(desc), machine))
    return templates.TemplateResponse(request=request, name="paper.html", context=ctx)


@app.get("/paper/table", response_class=HTMLResponse)
def paper_table(
    request: Request,
    conn=Depends(db),
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    jobtype: str = "",
    sort: str = "sheets",
    desc: int = 1,
    machine: str = "",
):
    """htmx partial, key figures included — they follow the filter."""
    ctx = {"request": request, "lang": request.state.lang}
    ctx.update(
        _paper_ctx(
            request, conn, q, date_from, date_to, jobtype, sort, bool(desc), _machine_param(machine)
        )
    )
    return templates.TemplateResponse(request=request, name="_paper_table.html", context=ctx)


# :path because the key is free-form — a configured pattern may capture a slash.
@app.get("/job/{job_key:path}", response_class=HTMLResponse)
def job_detail_view(request: Request, job_key: str, conn=Depends(db)):
    job = reports.job_detail(conn, job_key)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_key} not found")
    _band_heights(job["rows"])

    ctx = base_ctx(request, conn, "jobs")
    ctx.update({"job": job, "back_url": _back_to_jobs(request)})
    return templates.TemplateResponse(request=request, name="job_detail.html", context=ctx)


def _unassigned_ctx(conn, q, date_from, date_to, filter, machine="") -> dict:
    # System runs are excluded: their missing keys are expected, and /own-use
    # reports them.
    jobtype = filter if filter in reports.JOBTYPES else None

    rows = reports.unassigned_runs(
        conn,
        date_from=date_from or None,
        date_to=date_to or None,
        q=q or None,
        jobtype=jobtype,
        exclude_jobtype=reports.SYSTEM_JOBTYPE,
        machine=machine or None,
    )
    return {
        "rows": rows,
        "totals": {
            "clicks": sum(r["clicks"] or 0 for r in rows),
            "sheets": sum(r["sheets"] or 0 for r in rows),
        },
        "q": q,
        "date_from": date_from,
        "date_to": date_to,
        "filter": filter,
        "machine": machine,
        "limits": reports.date_limits(conn),
    }


@app.get("/unassigned", response_class=HTMLResponse)
def unassigned(
    request: Request,
    conn=Depends(db),
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    filter: str = "",
    machine: str = "",
):
    machine = _machine_param(machine)
    ctx = base_ctx(request, conn, "unassigned", machine)
    ctx.update(_unassigned_ctx(conn, q, date_from, date_to, filter, machine))
    return templates.TemplateResponse(request=request, name="unassigned.html", context=ctx)


@app.get("/unassigned/table", response_class=HTMLResponse)
def unassigned_table(
    request: Request,
    conn=Depends(db),
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    filter: str = "",
    machine: str = "",
):
    """htmx partial, key figures included — they follow the filter."""
    ctx = {
        "request": request,
        "lang": request.state.lang,
        "multi_machine": config.get().multi_machine,
    }
    ctx.update(_unassigned_ctx(conn, q, date_from, date_to, filter, _machine_param(machine)))
    return templates.TemplateResponse(request=request, name="_unassigned_table.html", context=ctx)


def _own_use_ctx(conn, q, date_from, date_to, machine="") -> dict:
    rows = reports.own_use(
        conn,
        date_from=date_from or None,
        date_to=date_to or None,
        q=q or None,
        machine=machine or None,
    )
    months = reports.own_use_months(
        conn,
        date_from=date_from or None,
        date_to=date_to or None,
        q=q or None,
        machine=machine or None,
    )
    return {
        "rows": rows,
        "months": months,
        "totals": {
            "runs": sum(r["runs"] or 0 for r in rows),
            "clicks": sum(r["clicks"] or 0 for r in rows),
            "sheets": sum(r["sheets"] or 0 for r in rows),
        },
        "q": q,
        "date_from": date_from,
        "date_to": date_to,
        "machine": machine,
        "limits": reports.date_limits(conn),
    }


@app.get("/own-use", response_class=HTMLResponse)
def own_use(
    request: Request,
    conn=Depends(db),
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    machine: str = "",
):
    """What the press printed for itself — calibration, service, meter reports."""
    machine = _machine_param(machine)
    ctx = base_ctx(request, conn, "own-use", machine)
    ctx.update(_own_use_ctx(conn, q, date_from, date_to, machine))
    return templates.TemplateResponse(request=request, name="own_use.html", context=ctx)


@app.get("/own-use/table", response_class=HTMLResponse)
def own_use_table(
    request: Request,
    conn=Depends(db),
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    machine: str = "",
):
    """htmx partial, key figures included — they follow the filter."""
    ctx = {
        "request": request,
        "lang": request.state.lang,
        "multi_machine": config.get().multi_machine,
    }
    ctx.update(_own_use_ctx(conn, q, date_from, date_to, _machine_param(machine)))
    return templates.TemplateResponse(request=request, name="_own_use_table.html", context=ctx)


@app.get("/status", response_class=HTMLResponse)
def status(request: Request, conn=Depends(db)):
    settings = config.get()
    ctx = base_ctx(request, conn, "status")
    ctx.update(
        {
            # One block per press — state, days and history are all per machine.
            "blocks": [
                {
                    "machine": machine,
                    "status": st,
                    "history": reports.sync_history(conn, machine.id),
                }
                for machine, st in zip(settings.machines, ctx["status"], strict=True)
            ],
            "total_rows": conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"],
            "interval": settings.sync_interval_min,
            "raw_dir": str(settings.raw_dir),
        }
    )
    return templates.TemplateResponse(request=request, name="status.html", context=ctx)


@app.post("/sync", include_in_schema=False)
def sync_now(
    background: BackgroundTasks, redirect_to: str = "/status", machine: str = Form(default="")
):
    """Manual sync, in the background so the page does not wait on a
    switched-off machine. Without an id, every configured press in turn.
    """
    background.add_task(run_sync_once, _machine_param(machine) or None)
    return RedirectResponse(
        _safe_redirect(redirect_to, "/status"), status_code=303, background=background
    )


@app.get("/api/jobs")
def api_jobs(
    conn=Depends(db),
    date_from: str = "",
    date_to: str = "",
    q: str = "",
    jobtype: str = "IP",
    result: str = "",
    machine: str = "",
):
    machine = _machine_param(machine)
    jobs = reports.job_list(
        conn,
        date_from=date_from or None,
        date_to=date_to or None,
        q=q or None,
        jobtype=jobtype or None,
        result=result or None,
        machine=machine or None,
    )
    return {
        "filter": {
            "date_from": date_from or None,
            "date_to": date_to or None,
            "jobtype": jobtype or None,
            "result": result or None,
            "machine": machine or None,
        },
        "count": len(jobs),
        "clicks": sum(j["clicks"] or 0 for j in jobs),
        "jobs": jobs,
    }


@app.get("/api/paper")
def api_paper(
    conn=Depends(db),
    date_from: str = "",
    date_to: str = "",
    q: str = "",
    jobtype: str = "",
    machine: str = "",
):
    machine = _machine_param(machine)
    grades = reports.paper_usage(
        conn,
        date_from=date_from or None,
        date_to=date_to or None,
        q=q or None,
        jobtype=jobtype or None,
        machine=machine or None,
    )
    return {
        "filter": {
            "date_from": date_from or None,
            "date_to": date_to or None,
            "q": q or None,
            "jobtype": jobtype or None,
            "machine": machine or None,
        },
        "count": len(grades),
        "sheets": sum(s["sheets"] or 0 for s in grades),
        "grades": grades,
    }


@app.get("/api/job/{job_key:path}")
def api_job(job_key: str, conn=Depends(db)):
    job = reports.job_detail(conn, job_key)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_key} not found")
    return job


@app.get("/api/status")
def api_status(conn=Depends(db)):
    """One entry per configured machine."""
    return {
        "interval_min": config.get().sync_interval_min,
        "machines": [
            {**status, "name": machine.name, "url": machine.url}
            for machine, status in zip(
                config.get().machines, reports.sync_status_all(conn), strict=True
            )
        ],
    }
