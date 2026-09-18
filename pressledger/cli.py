import argparse
import logging
import sys

from . import config


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _cmd_sync(args) -> int:
    from .sync import sync, sync_all

    if args.machine:
        machine = config.get().machine(args.machine)
        if machine is None:
            known = ", ".join(m.id for m in config.get().machines)
            print(f"Unknown machine id {args.machine!r}. Configured: {known}", file=sys.stderr)
            return 2
        results = [sync(machine)]
    else:
        results = sync_all()

    for result in results:
        if not result.ok:
            # No error exit: a powered-off machine is the normal case and
            # must not mark a cron or service run as failed.
            print(f"{result.machine_id}: unreachable ({result.error}). Data unchanged.")
            continue
        print(
            f"{result.machine_id}: {result.files_imported} file(s) imported, "
            f"{result.rows_imported} rows."
        )
    return 0


def _cmd_reimport(args) -> int:
    from .db import get_conn
    from .sync import InvalidAccountingFile, reimport_from_raw

    try:
        result = reimport_from_raw(rebuild=args.rebuild)
    except InvalidAccountingFile as exc:
        # Raw files lie in the archive root. One line with the `mv`, not a
        # traceback.
        print(exc, file=sys.stderr)
        return 2
    if result.error:
        print(result.error, file=sys.stderr)
        return 1

    # When both ACL and CSV exist for a day, it is read twice — the actual
    # row count in the database is more meaningful than the read count.
    conn = get_conn()
    try:
        row_count = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
    finally:
        conn.close()
    print(f"{result.files_imported} file(s) read, {row_count} rows in the database.")
    return 0


def _cmd_serve(args) -> int:
    import os

    import uvicorn

    # Pass the resolved config path to uvicorn's reload child process, which
    # imports the app independently and would otherwise use the default file.
    os.environ[config.CONFIG_ENV_VAR] = str(config.get().path)

    uvicorn.run(
        "pressledger.web.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=["pressledger"] if args.reload else None,
        log_level="debug" if args.verbose else "info",
    )
    return 0


def _dispatch(args) -> int:
    """Run the chosen command."""
    from .db import SchemaMismatch

    try:
        return args.func(args)
    except SchemaMismatch as exc:
        print(exc, file=sys.stderr)
        return 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="pressledger",
        description="Reporting on Canon digital-print accounting data.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose log output")
    parser.add_argument(
        "--config",
        metavar="PATH",
        help=f"Configuration file (default: {config.DEFAULT_CONFIG_PATH})",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sync = sub.add_parser("sync", help="Fetch data from the printer once")
    p_sync.add_argument(
        "--machine", metavar="ID", help="Only this machine id (default: all configured)"
    )
    p_sync.set_defaults(func=_cmd_sync)

    p_re = sub.add_parser("reimport", help="Rebuild database from data/raw/")
    p_re.add_argument("--rebuild", action="store_true", help="Drop schema and recreate it")
    p_re.set_defaults(func=_cmd_reimport)

    p_serve = sub.add_parser("serve", help="Start the web interface and scheduler")
    # Filled in after the configuration is read — a default here would have to
    # read it before --config is even parsed.
    p_serve.add_argument("--host")
    p_serve.add_argument("--port", type=int)
    p_serve.add_argument("--reload", action="store_true", help="Auto-reload for development")
    p_serve.set_defaults(func=_cmd_serve)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    try:
        settings = config.load(args.config)
        if args.cmd == "serve":
            args.host = args.host or settings.host
            args.port = args.port or settings.port
        return _dispatch(args)
    except config.ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
