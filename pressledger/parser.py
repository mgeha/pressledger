import csv
import json
import logging
from io import StringIO

log = logging.getLogger(__name__)

# The first column of the header row is '4302'; data rows have '4303'.
# Pure record-type marker, not part of the payload.
RECORD_HEADER = "4302"
RECORD_DATA = "4303"

# Eleven media sub-fields repeat 16 times per record — mediahdr1..16 in Canon's
# grammar. The index is a MEDIA SLOT, not a tray number, and a job using more
# than 16 media collapses the surplus onto slot 16. The DB column is named
# `tray` regardless.
MEDIA_SLOTS = range(1, 17)

# <fs> in Canon's grammar is a semicolon or a comma, set per machine ("Field
# separator" in the Settings Editor) and constant within a file. Ours writes ';'.
FIELD_SEPARATORS = (";", ",")
DEFAULT_SEPARATOR = ";"


def decode_content(content_bytes: bytes) -> str:
    """Decode a raw accounting file.

    The machine writes UTF-8 and the byte-order mark is a separate setting
    ("UTF8-header enabled"), so utf-8-sig handles both. The cp1252 fallback
    cannot raise, which keeps a whole day importable.
    """
    try:
        return content_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        log.warning("Not valid UTF-8 (%s) — decoding as cp1252", exc)
        return content_bytes.decode("cp1252", errors="replace")


def detect_separator(content: str) -> str:
    """Field separator of a log file, read off the 4302 header row.

    The header cannot hold an escaped separator, so the character after the
    record marker is the separator by definition.
    """
    for line in content.splitlines():
        stripped = line.lstrip("\ufeff").lstrip()
        if stripped.startswith(RECORD_HEADER):
            candidate = stripped[len(RECORD_HEADER) : len(RECORD_HEADER) + 1]
            return candidate if candidate in FIELD_SEPARATORS else DEFAULT_SEPARATOR
    return DEFAULT_SEPARATOR


def parse_csv(content: str, separator: str | None = None) -> list[dict]:
    separator = separator or detect_separator(content)
    if separator != DEFAULT_SEPARATOR:
        log.info("Field separator %r instead of %r", separator, DEFAULT_SEPARATOR)
    reader = csv.reader(StringIO(content), delimiter=separator)
    header = None
    rows = []
    for row in reader:
        if not row:
            continue
        if row[0] == RECORD_HEADER:
            header = row
        elif row[0] == RECORD_DATA and header:
            # strict=False: the printer omits trailing empty fields, so short
            # rows are normal. A row longer than the header loses the surplus.
            rows.append(dict(zip(header, row, strict=False)))
    return rows


def _intval(d: dict, key: str, default=0):
    v = (d.get(key) or "").strip()
    try:
        return int(v) if v else default
    except ValueError:
        return default


def _strval(d: dict, key: str) -> str:
    return (d.get(key) or "").strip()


def row_to_db(d: dict, machine_id: str, source_date: str, line_seq: int) -> dict:
    def intval(k, default=0):
        return _intval(d, k, default)

    return {
        # Not from the file, but from the archive directory it was read from.
        "machine_id": machine_id,
        "source_date": source_date,
        "jobid": intval("jobid"),
        "line_seq": line_seq,
        "jobtype": d.get("jobtype", ""),
        "startdate": d.get("startdate", ""),
        "starttime": d.get("starttime", ""),
        "readydate": d.get("readydate", ""),
        "readytime": d.get("readytime", ""),
        "result": d.get("result", ""),
        "username": d.get("username", ""),
        "jobname": d.get("jobname", ""),
        "noffinishedsets": intval("noffinishedsets"),
        "nofprinteda4bw": intval("nofprinteda4bw"),
        "nofprinteda4c": intval("nofprinteda4c"),
        "nofprinteda3bw": intval("nofprinteda3bw"),
        "nofprinteda3c": intval("nofprinteda3c"),
        "nofprintedXLbw": intval("nofprintedXLbw"),
        "nofprintedXLc": intval("nofprintedXLc"),
        "nofbooklets": intval("nofbooklets"),
        "nofsinglestaples": intval("nofsinglestaples"),
        "nofdoublestaples": intval("nofdoublestaples"),
        "nofpunches": intval("nofpunches"),
        "nofcreases": intval("nofcreases"),
        "noffolds": intval("noffolds"),
        "raw_json": raw_json(d),
    }


def raw_json(d: dict) -> str:
    """The non-empty fields of a CSV row as compact JSON, empty ones dropped.

    Keeps the fields that have no database column of their own reachable, so a
    column the machine starts populating needs no reimport.
    """
    payload = {k: v.strip() for k, v in d.items() if k and k != RECORD_HEADER and v and v.strip()}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def extract_media(
    d: dict, machine_id: str, source_date: str, jobid: int, line_seq: int
) -> list[dict]:
    """Media usage as one row per media slot actually used.

    A slot counts as used when a media format is set or sheets were drawn on it.
    The slot number goes into the `tray` column — see MEDIA_SLOTS.
    """
    rows = []
    for slot in MEDIA_SLOTS:
        mediaformat = _strval(d, f"mediaformat{slot}")
        nofsimplex = _intval(d, f"nofsimplex{slot}")
        nofduplex = _intval(d, f"nofduplex{slot}")
        if not mediaformat and not nofsimplex and not nofduplex:
            continue
        rows.append(
            {
                "machine_id": machine_id,
                "source_date": source_date,
                "jobid": jobid,
                "line_seq": line_seq,
                "tray": slot,
                "mediaformat": mediaformat,
                "mediatype": _strval(d, f"mediatype{slot}"),
                "mediaweight": _intval(d, f"mediaweight{slot}", None),
                "mediacolor": _strval(d, f"mediacolor{slot}"),
                "medianame": _strval(d, f"medianame{slot}"),
                "nofsimplex": nofsimplex,
                "nofduplex": nofduplex,
                "isinsert": _strval(d, f"isinsert{slot}"),
                "istab": _strval(d, f"istab{slot}"),
            }
        )
    return rows
