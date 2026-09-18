import sqlite3

from . import config, jobkey

# Bumped whenever an incompatible schema change requires a reimport from
# data/raw/. The public schema starts at version 1.
SCHEMA_VERSION = 1

SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS jobs (
        -- The id from pressledger.toml, and the name of the raw archive
        -- directory. Leads the primary key because jobid is machine-local.
        machine_id       TEXT    NOT NULL,
        source_date      TEXT    NOT NULL,
        jobid            INTEGER NOT NULL,
        line_seq         INTEGER NOT NULL,
        jobtype          TEXT,
        startdate        TEXT,
        starttime        TEXT,
        readydate        TEXT,
        readytime        TEXT,
        result           TEXT,
        username         TEXT,
        -- No column holds the job grouping: the key is derived from jobname at
        -- query time via a configurable pattern. See jobkey.py.
        jobname          TEXT,
        noffinishedsets  INTEGER,
        nofprinteda4bw   INTEGER,
        nofprinteda4c    INTEGER,
        nofprinteda3bw   INTEGER,
        nofprinteda3c    INTEGER,
        nofprintedXLbw   INTEGER,
        nofprintedXLc    INTEGER,
        nofbooklets      INTEGER,
        nofsinglestaples INTEGER,
        nofdoublestaples INTEGER,
        nofpunches       INTEGER,
        nofcreases       INTEGER,
        noffolds         INTEGER,
        raw_json         TEXT,
        -- No export-state column: `reimport --rebuild` drops this table, so a
        -- copy of it here would be lost with it.
        PRIMARY KEY (machine_id, source_date, jobid, line_seq)
    );

    CREATE INDEX IF NOT EXISTS idx_jobs_startdate ON jobs(startdate);
    CREATE INDEX IF NOT EXISTS idx_jobs_jobtype   ON jobs(jobtype);
    CREATE INDEX IF NOT EXISTS idx_jobs_machine    ON jobs(machine_id, startdate);

    -- One row per used media slot (up to 16). The tray column stores the slot number.
    CREATE TABLE IF NOT EXISTS job_media (
        machine_id   TEXT    NOT NULL,
        source_date  TEXT    NOT NULL,
        jobid        INTEGER NOT NULL,
        line_seq     INTEGER NOT NULL,
        tray         INTEGER NOT NULL,
        mediaformat  TEXT,
        mediatype    TEXT,
        mediaweight  INTEGER,
        mediacolor   TEXT,
        medianame    TEXT,
        nofsimplex   INTEGER,
        nofduplex    INTEGER,
        isinsert     TEXT,
        istab        TEXT,
        PRIMARY KEY (machine_id, source_date, jobid, line_seq, tray),
        FOREIGN KEY (machine_id, source_date, jobid, line_seq)
            REFERENCES jobs(machine_id, source_date, jobid, line_seq) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_job_media_material ON job_media(mediaweight, medianame);
    CREATE INDEX IF NOT EXISTS idx_job_media_source   ON job_media(machine_id, source_date);

    -- One record per calendar day and machine: which file, CSV (final) or ACL (live).
    CREATE TABLE IF NOT EXISTS sync_log (
        machine_id   TEXT NOT NULL,
        source_date  TEXT NOT NULL,
        source_type  TEXT NOT NULL,
        filename     TEXT NOT NULL,
        last_synced  TEXT NOT NULL,
        row_count    INTEGER NOT NULL,
        PRIMARY KEY (machine_id, source_date)
    );

    -- One record per sync ATTEMPT and machine. ok=0 means the file listing
    -- could not be fetched — a press that was off, a timeout, an HTTP error.
    CREATE TABLE IF NOT EXISTS sync_run (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        machine_id     TEXT NOT NULL,
        started_at     TEXT NOT NULL,
        finished_at    TEXT,
        ok             INTEGER NOT NULL DEFAULT 0,
        error          TEXT,
        files_seen     INTEGER DEFAULT 0,
        files_imported INTEGER DEFAULT 0,
        rows_imported  INTEGER DEFAULT 0
    );

    CREATE INDEX IF NOT EXISTS idx_sync_run_started ON sync_run(machine_id, started_at);

    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    );
"""

# Everything this schema owns, in drop order.
TABLES = ["job_media", "jobs", "sync_log", "sync_run", "meta"]


class SchemaMismatch(RuntimeError):
    """The existing database does not match this PressLedger version."""


def register_functions(conn: sqlite3.Connection) -> None:
    """Derivations the queries call in SQL.

    Separate from get_conn() so tests can register them on an in-memory
    connection; without them the queries fail with "no such function".
    """
    conn.create_function("job_key", 1, jobkey.job_key, deterministic=True)


def get_conn() -> sqlite3.Connection:
    db_path = config.get().db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: FastAPI may open the connection and use it in
    # different threadpool workers, though never at the same time.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    register_functions(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def reset_db(conn: sqlite3.Connection) -> None:
    """Drop all tables and recreate the schema from scratch.

    The job data can be reimported from a complete raw archive. The sync history
    in sync_run cannot — it is not in the archive and is lost here.
    """
    with conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        for table in TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create an empty database, or refuse an incompatible existing one."""
    existing = {
        r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if existing:
        version = None
        if "meta" in existing:
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            version = row["value"] if row else None
        if version != str(SCHEMA_VERSION):
            raise SchemaMismatch(
                f"Incompatible database schema {version or 'unknown'}; "
                f"expected version {SCHEMA_VERSION}.\n"
                "Rebuild from raw data:  uv run pressledger reimport --rebuild"
            )
    init_db(conn)
