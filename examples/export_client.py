"""Example: consuming the PressLedger export API.

Reads print runs from /api/v1 and turns them into records for a downstream
system — billing, time tracking, cost accounting. Standard library only.

Usage:
    python export_client.py

Configuration via environment variables:
    PRESSLEDGER_URL    Base URL of the PressLedger instance
                       (e.g. http://pressledger.local:8000)
    PRESSLEDGER_TOKEN  Bearer token from api.token in pressledger.toml
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

BASE_URL = os.environ.get("PRESSLEDGER_URL", "http://localhost:8000")
TOKEN = os.environ.get("PRESSLEDGER_TOKEN", "")

# How many days back to look. Adjust to match your billing cycle.
LOOKBACK_DAYS = 30


def _get(path: str, params: dict) -> dict:
    url = f"{BASE_URL}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        sys.exit(f"HTTP {exc.code} from {url}:\n{body}")


def final_days(date_from: str, date_to: str) -> list[dict]:
    """Start every run here, not with a hard-coded date range.

    A day with final=False is still the live log file and its rows can change.
    Only this list separates "nothing printed" from "nothing imported".
    """
    data = _get("/api/v1/days", {"date_from": date_from, "date_to": date_to})
    return [d for d in data["days"] if d["final"]]


def iter_runs(date_from: str, date_to: str):
    """Page through /api/v1/runs; `next` carries the cursor for the next call.

    The cursor is pagination, not a watermark: a day synced late lands behind a
    cursor already consumed. Track what you processed by row identity.
    """
    params = {
        "date_from": date_from,
        "date_to": date_to,
        "date_field": "completed_date",  # bill by completion, not by start
        "jobtype": "IP",  # production runs; remove to get all types
        "final_only": "true",
        "limit": 500,
    }
    while True:
        data = _get("/api/v1/runs", params)
        yield from data["runs"]
        if not data["next"]:
            break
        params["after"] = data["next"]


def record_key(run: dict) -> str:
    """Stable de-duplication key: same row, same key on every run.

    machine_id belongs in it, because jobid and line_seq are machine-local.
    Quantities and durations do not — a corrected count would change the key and
    the row would come back as a second record.
    """
    return ":".join(
        [
            "pressledger",
            "v1",
            run["machine_id"],
            str(run["jobid"]),
            str(run["line_seq"]),
            run["source_date"],
            run["completed_at"] or run["startdate"] + "T" + run["starttime"],
        ]
    )


def to_record(run: dict) -> dict:
    """Map a run to whatever the target system needs.

    Null is not zero: `completed_at` and `runtime_s` are null where the press
    reported no usable clock time, and an unknown duration must not be passed on
    as no time at all.
    """
    runtime_s = run["runtime_s"]
    return {
        "key": record_key(run),
        "machine": run["machine_id"],
        "jobname": run["jobname"],
        "date": run["completed_date"],
        # Hours, or None where the press left the timestamps unusable.
        "runtime_h": runtime_s / 3600 if runtime_s is not None else None,
        "clicks_bw": (run["nofprinteda4bw"] + run["nofprinteda3bw"] + run["nofprintedXLbw"]),
        "clicks_colour": (run["nofprinteda4c"] + run["nofprinteda3c"] + run["nofprintedXLc"]),
    }


def main() -> None:
    if not TOKEN:
        sys.exit("Set PRESSLEDGER_TOKEN to the bearer token from pressledger.toml")

    date_to = date.today().isoformat()
    date_from = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()

    print(f"Window: {date_from} … {date_to}")

    days = final_days(date_from, date_to)
    if not days:
        print("No final days in window — nothing to process.")
        return

    print(f"{len(days)} final day(s) available.")

    records = [to_record(run) for run in iter_runs(date_from, date_to)]

    print(f"{len(records)} run(s) fetched.\n")

    # Replace this with the real thing: look the key up in the target system,
    # skip it if it is already there, insert otherwise.
    for rec in records:
        runtime = f"{rec['runtime_h']:.4f}h" if rec["runtime_h"] is not None else "unknown"
        print(
            f"  {rec['date']}  {rec['machine']}  "
            f"bw={rec['clicks_bw']:4d}  colour={rec['clicks_colour']:4d}  "
            f"runtime={runtime:>9}  {rec['jobname']}"
        )


if __name__ == "__main__":
    main()
