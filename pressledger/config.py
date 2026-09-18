"""Settings from pressledger.toml — see pressledger.toml.example for every key.

`--config PATH` points at a different file; PRESSLEDGER_CONFIG does the same for
the child process `uvicorn --reload` spawns, which imports the app itself.

Every reader goes through get(); nothing caches a value out of here.
"""

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).parent.parent

DEFAULT_CONFIG_PATH = ROOT / "pressledger.toml"
CONFIG_ENV_VAR = "PRESSLEDGER_CONFIG"

# UI languages. Not configurable — a language needs a translation catalogue.
SUPPORTED_LANGS: frozenset[str] = frozenset({"en", "de"})


class ConfigError(RuntimeError):
    """A setting is missing or unusable — the process must not start."""


# The id becomes a directory name (data/raw/<id>/), so it is restricted rather
# than escaped later. No dot rules out `.` and `..`; no colon, which separates a
# drive or a stream on Windows.
MACHINE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class Machine:
    """One press. `id` is assigned in the config file and carried by every stored
    row; never the machine's serial number."""

    id: str
    name: str
    url: str


@dataclass(frozen=True)
class Settings:
    path: Path
    host: str
    port: int
    db_path: Path
    raw_dir: Path
    sync_interval_min: int
    http_timeout: int
    site_name: str
    custom_css: Path | None
    default_lang: str
    # The raw pattern for messages, the compiled one for the query layer. None
    # means no grouping is configured — see jobkey.py.
    job_key_pattern: str
    job_key_re: re.Pattern | None
    # Shared token of the export API (/api/v1/*). Empty means those routes are
    # switched off — never open. See web/deps.py.
    api_token: str
    machines: tuple[Machine, ...]

    def machine(self, machine_id: str) -> Machine | None:
        for m in self.machines:
            if m.id == machine_id:
                return m
        return None

    @property
    def multi_machine(self) -> bool:
        """Whether the interface shows the machine column and filter at all."""
        return len(self.machines) > 1


# What the file may contain. Unknown tables and keys are refused, so a typo
# names itself instead of silently leaving a default in place.
_TABLES: dict[str, set[str]] = {
    "server": {"host", "port"},
    "paths": {"db", "raw"},
    "sync": {"interval_min", "http_timeout"},
    "ui": {"site_name", "custom_css", "lang"},
    "jobs": {"key_pattern"},
    "api": {"token"},
}
_MACHINE_KEYS = {"id", "name", "url"}


def _reject_unknown(data: dict) -> None:
    known = set(_TABLES) | {"machine"}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(
            f"Unknown section(s) in the configuration: {', '.join(unknown)}. "
            f"Known: {', '.join(sorted(known))}"
        )
    for table, keys in _TABLES.items():
        section = data.get(table)
        if section is None:
            continue
        if not isinstance(section, dict):
            raise ConfigError(f"[{table}] must be a section, not a value")
        extra = sorted(set(section) - keys)
        if extra:
            raise ConfigError(
                f"Unknown key(s) in [{table}]: {', '.join(extra)}. Known: {', '.join(sorted(keys))}"
            )


def _get(data: dict, table: str, key: str, default, kind: type):
    value = data.get(table, {}).get(key, default)
    # No key here is a boolean, and bool is a subclass of int: `port = true`
    # would pass an isinstance check for int.
    if isinstance(value, bool) or not isinstance(value, kind):
        raise ConfigError(f"{table}.{key} must be {kind.__name__}, got {type(value).__name__}")
    return value


def _machine_str(entry: dict, key: str, index: int) -> str:
    """One machine field, as strict as _get is for the tables — `id = 1000` would
    otherwise land in a directory name. Missing is allowed; the caller decides
    which fields are required.
    """
    value = entry.get(key, "")
    if not isinstance(value, str):
        raise ConfigError(f"[[machine]] #{index}: {key} must be str, got {type(value).__name__}")
    return value.strip()


def _resolve(base: Path, value: str) -> Path:
    """A relative path in the file is relative to the FILE, not to the working
    directory."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path)


def _machines(data: dict) -> tuple[Machine, ...]:
    raw = data.get("machine", [])
    if not isinstance(raw, list) or not raw:
        raise ConfigError(
            "No machine configured. Add at least one [[machine]] section with "
            "id, name and url — see pressledger.toml.example"
        )

    machines: list[Machine] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            raise ConfigError(f"[[machine]] #{index} must be a section")
        extra = sorted(set(entry) - _MACHINE_KEYS)
        if extra:
            raise ConfigError(
                f"Unknown key(s) in [[machine]] #{index}: {', '.join(extra)}. "
                f"Known: {', '.join(sorted(_MACHINE_KEYS))}"
            )

        machine_id = _machine_str(entry, "id", index)
        if not machine_id:
            raise ConfigError(f"[[machine]] #{index} has no id")
        if not MACHINE_ID_RE.match(machine_id):
            raise ConfigError(
                f"Invalid machine id {machine_id!r}: only letters, digits, dash "
                "and underscore — no dot, no colon. It is used as a directory "
                "name in the raw archive."
            )
        if machine_id in seen:
            raise ConfigError(f"Duplicate machine id {machine_id!r}")
        seen.add(machine_id)

        url = _machine_str(entry, "url", index).rstrip("/")
        if not url:
            raise ConfigError(f"Machine {machine_id!r} has no url")

        machines.append(
            Machine(
                id=machine_id,
                name=_machine_str(entry, "name", index) or machine_id,
                url=url,
            )
        )
    return tuple(machines)


def _job_key_re(pattern: str) -> re.Pattern | None:
    """Compile the grouping rule, or None if none is configured.

    Compiled here so a broken pattern stops the start: no grouping is a valid
    setting, and an unusable pattern must never degrade to it.
    """
    if not pattern:
        return None
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ConfigError(f"Invalid jobs.key_pattern: {exc}") from exc


def _api_token(data: dict) -> str:
    """Read api.token and reject anything that is not plain ASCII.

    It travels in an HTTP header, which is decoded as latin-1 — a non-ASCII
    token would not survive the round trip.
    """
    token = _get(data, "api", "token", "", str).strip()
    if not token.isascii():
        raise ConfigError(
            "api.token must be plain ASCII. Generate one with: "
            'python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    return token


def _build(path: Path, data: dict) -> Settings:
    _reject_unknown(data)
    base = path.parent
    custom_css = _get(data, "ui", "custom_css", "", str).strip()
    lang = _get(data, "ui", "lang", "en", str).strip()
    if lang not in SUPPORTED_LANGS:
        raise ConfigError(f"ui.lang is {lang!r}, supported: {', '.join(sorted(SUPPORTED_LANGS))}")
    pattern = _get(data, "jobs", "key_pattern", "", str)
    return Settings(
        path=path,
        # Loopback by default: the pages carry no login.
        host=_get(data, "server", "host", "127.0.0.1", str),
        port=_get(data, "server", "port", 8000, int),
        db_path=_resolve(base, _get(data, "paths", "db", "data/pressledger.sqlite", str)),
        raw_dir=_resolve(base, _get(data, "paths", "raw", "data/raw", str)),
        sync_interval_min=_get(data, "sync", "interval_min", 30, int),
        # Also the delay one offline press adds before the next one is tried.
        http_timeout=_get(data, "sync", "http_timeout", 10, int),
        site_name=_get(data, "ui", "site_name", "", str).strip(),
        # Loaded after the default stylesheet, so it can override the variables.
        custom_css=_resolve(base, custom_css) if custom_css else None,
        default_lang=lang,
        job_key_pattern=pattern,
        job_key_re=_job_key_re(pattern),
        api_token=_api_token(data),
        machines=_machines(data),
    )


def default_path() -> Path:
    raw = (os.environ.get(CONFIG_ENV_VAR) or "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_CONFIG_PATH


_settings: Settings | None = None


def load(path: Path | str | None = None) -> Settings:
    """Read, validate and install the configuration. Entry points call this."""
    global _settings
    path = Path(path).expanduser() if path else default_path()
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(
            f"Configuration file not found: {path}\n"
            "Copy pressledger.toml.example to pressledger.toml and adjust it, "
            "or pass --config PATH."
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from exc

    _settings = _build(path.resolve(), data)
    return _settings


def get() -> Settings:
    """The active configuration, loading the default file on first use.

    The lazy load is what the uvicorn reload child needs — it imports the app
    without going through cli.main().
    """
    if _settings is None:
        return load()
    return _settings


def install(settings: Settings) -> Settings:
    """Install an already-built configuration instead of reading a file."""
    global _settings
    _settings = settings
    return settings


def reset() -> None:
    """Forget the loaded configuration — for tests."""
    global _settings
    _settings = None
