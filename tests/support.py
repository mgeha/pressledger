"""Isolated configuration helpers for tests."""

import contextlib
from dataclasses import replace
from pathlib import Path

from pressledger import config

MACHINE = config.Machine(id="v1000-01", name="imagePRESS V1000", url="http://printer.test")
SECOND_MACHINE = config.Machine(
    id="v1000-02", name="imagePRESS V1000 #2", url="http://printer2.test"
)

# Paths that do not exist, so a test that touches the database or the archive
# has to override them.
BASE = config.Settings(
    path=Path("/nonexistent/pressledger.toml"),
    host="127.0.0.1",
    port=8000,
    db_path=Path("/nonexistent/pressledger.sqlite"),
    raw_dir=Path("/nonexistent/raw"),
    sync_interval_min=30,
    http_timeout=10,
    site_name="",
    custom_css=None,
    default_lang="en",
    job_key_pattern="",
    job_key_re=None,
    # Empty, as in production: the export API is off unless a test says so.
    api_token="",
    machines=(MACHINE,),
)


def settings(**overrides) -> config.Settings:
    """BASE with overrides. Passing job_key_pattern compiles it as load() would."""
    if "job_key_pattern" in overrides and "job_key_re" not in overrides:
        overrides["job_key_re"] = config._job_key_re(overrides["job_key_pattern"])
    return replace(BASE, **overrides)


@contextlib.contextmanager
def configured(**overrides):
    """Install a configuration for the duration of the block."""
    previous = config.get() if config._settings is not None else None
    config.install(settings(**overrides))
    try:
        yield config.get()
    finally:
        if previous is None:
            config.reset()
        else:
            config.install(previous)
