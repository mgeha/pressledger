# PressLedger

[![CI](https://github.com/mgeha/pressledger/actions/workflows/ci.yml/badge.svg)](https://github.com/mgeha/pressledger/actions/workflows/ci.yml)

Web-based accounting report for Canon PRISMAsync print systems. Fetches job data
from the printer's built-in accounting endpoint, stores it locally in SQLite, and
presents it as a filterable, sortable web interface — with JSON API endpoints for
external data consumers.

## Features

- Per-job click counts (A4/A3/XL, colour/mono) aggregated across print days
- Several machines in one report, with a per-press breakdown on the job detail
- Paper consumption by media type (sheets, weight, format)
- Proof/production run classification
- Automatic sync on a configurable interval
- htmx-driven UI, no JavaScript framework
- JSON API for retrieving flat, ungrouped machine and print-run data (`/api/v1`)

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- Network access to the printer's accounting endpoint

## Quickstart

```bash
git clone https://github.com/mgeha/pressledger.git
cd pressledger
cp pressledger.toml.example pressledger.toml
# Edit pressledger.toml — at minimum the [[machine]] url
uv run pressledger sync          # fetch data from the printer
uv run pressledger serve         # start the web interface on http://localhost:8000
```

For a one-time import from an existing archive of raw CSV files:

```bash
uv run pressledger reimport --rebuild
```

### Without a printer

`tools/fakeprinter.py` serves a local directory the way the press serves
`/accounting/`, so listing, download, archive and import can be walked without
the hardware. Point it at an existing raw archive, or at a hand-written file:

```bash
mkdir -p /tmp/pf
cat > /tmp/pf/99900011120260811.CSV <<'EOF'
4302;jobid;jobtype;jobname;startdate;starttime;readytime;result;noffinishedsets;nofprinteda4c
4303;1;IP;105774_Customer.pdf;2026-08-11;09:14:02;09:21:47;Done;25;120
EOF
uv run python tools/fakeprinter.py /tmp/pf 8098
```

`4302` marks the header row, `4303` a data row. `jobtype` has to be there: the
job list filters on `IP` by default, and a row without one stays invisible until
the filter is cleared. The machine writes 266 columns; any subset of them works,
since the two rows are read against each other. The last eight digits of the file
name are the log day — what precedes them is the machine's serial number, which
PressLedger does not use.

With a `[[machine]]` whose `url` is `http://127.0.0.1:8098`, `sync` then imports
that day and archives the file under `data/raw/<machine id>/`.

## Configuration

One file, `pressledger.toml` in the project root. Copy it from
`pressledger.toml.example`, which documents every key with its default;
`--config PATH` points at a different file. Unknown sections and keys are
refused rather than ignored — a typo must not silently leave a setting at its
default. Relative paths are relative to the configuration file.

```toml
[server]
host = "127.0.0.1"  # widen only behind a firewall/reverse proxy
port = 8000

[paths]
db  = "data/pressledger.sqlite"
raw = "data/raw"          # long-term archive, one directory per machine

[sync]
interval_min = 30
http_timeout = 10

[ui]
site_name  = "Example GmbH"
custom_css = "/etc/pressledger/custom.css"
lang       = "en"          # en or de

[jobs]
# Capture group 1 folds several print runs into one job. Leave it out and there
# is no grouping: one row per print run. An invalid expression stops the start.
key_pattern = '^(\d{6})_'

[api]
# Shared token of the export API. Without it /api/v1 answers 503 — the routes
# are never open. Generate one with secrets.token_urlsafe(32) and paste it here.
# token = ""

[[machine]]
id   = "v1000-01"          # in every stored row and archive directory
name = "imagePRESS V1000"  # shown in the interface
url  = "http://printer.local"
```

### Several machines

Add one `[[machine]]` section per press. The `id` is assigned here — letters,
digits, dash, underscore, and neither a dot nor a colon — and is what every row
in the database and every
directory under `data/raw/` carries; changing it later means rebuilding. The
machines are synced one after the other, because SQLite allows a single writer.

A job that ran on two presses is **one** job: the list shows it once, and the
detail page breaks clicks, sheets and run time down per press. A machine column
and filter appear as soon as more than one press is configured; with one, neither
appears.

## JSON API

The token-protected, read-only `/api/v1` endpoints provide machine, import-day
and print-run data as JSON. They can be used for a custom ERP integration,
reporting, data analysis or other downstream processing.

```bash
curl -H "Authorization: Bearer $TOKEN" \
     "http://pressledger.local:8000/api/v1/runs?date_from=2026-08-01&limit=100"
```

| Route | Description |
|---|---|
| `/api/v1/machines` | every press this database can answer for; `id` is what every row carries |
| `/api/v1/days` | imported log days per press, and whether a day is final |
| `/api/v1/runs` | the print runs, one JSON object per row of the machine's log |

Set `api.token` to enable the routes. Send it as a bearer token in the
`Authorization` header; when the data leaves a trusted network, serve the API
over HTTPS.

`examples/export_client.py` is an annotated reference implementation: it reads
`/api/v1/days` to determine the bookable window, pages through `/api/v1/runs`,
builds a stable de-duplication key, and prints one record per run. Adapt the
`to_record()` function and the booking block at the bottom to your target system.

## Translations

The UI ships in English and German. After changing a translatable string:

```bash
tools/i18n.sh
```

This extracts, updates `pressledger/locales/de/LC_MESSAGES/messages.po` and compiles
the `.mo`. Do not call `pybabel extract` without `-F babel.cfg` — it would then
read `.py` files only and miss every template string.

## Deployment

A sample systemd unit is provided in `deploy/pressledger.service`. It assumes the
application is installed at `/opt/pressledger` and runs as a dedicated `pressledger` user.
Configuration is read from `pressledger.toml` in the working directory.

```bash
sudo cp deploy/pressledger.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pressledger
```

## Routes

| Route | Description |
|---|---|
| `/jobs` | Job list, filterable and sortable |
| `/job/{job_key}` | Job detail: click counts, print runs, media |
| `/paper` | Paper consumption by media type |
| `/unassigned` | Print runs the grouping rule gives no key |
| `/own-use` | What the press printed for itself: calibration, service, meter reports |
| `/status` | State, imported days and sync history, one block per machine |
| `/api/jobs`, `/api/job/{job_key}`, `/api/paper`, `/api/status` | JSON equivalents of the above |
| `/api/v1/machines`, `/api/v1/days`, `/api/v1/runs` | Export API: flat rows, ungrouped, token required |

`job_key` is whatever `jobs.key_pattern` captures — with a pattern of your
own that can be `4711/03` or `Customer #12`, so **percent-encode it** in the path:
`/api/job/4711%2F03`. The pages do that themselves.

## No authentication

The service is designed for internal networks and has no login. Job names may
contain customer names — do not expose it to the public internet.

Default bind is `127.0.0.1`. Widening `server.host` is a deliberate step —
put a firewall or reverse proxy in front; the app enforces nothing itself.

The one exception: `/api/v1` requires the bearer token from `api.token` and
answers 503 without one, since those routes hand out every job name to
whoever asks.

## License

Copyright (C) 2026 Michael Hampicke

PressLedger is free software under the
[GNU Affero General Public License](LICENSE), version 3 or later.

The network clause is the part that matters in practice: if you modify
PressLedger and let others use it over a network, those users must be offered
the source of your modified version. Running the unmodified program, internally
or otherwise, obliges you to nothing.
