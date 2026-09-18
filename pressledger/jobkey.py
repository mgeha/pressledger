"""Group print runs using a configured job-name regex at query time.

Matching keys group runs across machines. Without a pattern, each row's
identity is its grouping key. Exports always return ungrouped runs.
Changing the pattern requires a restart but no reimport.
"""

from . import config


def apply_pattern(rx, jobname: str | None) -> str | None:
    """Return the first capture group, or the whole match if there are no groups.

    Return None for a missing pattern, empty job name or empty match.
    """
    if rx is None or not jobname:
        return None
    m = rx.search(jobname)
    if not m:
        return None
    return (m.group(1) if m.groups() else m.group(0)) or None


def job_key(jobname: str | None) -> str | None:
    """Grouping key of a run. Registered as an SQL function.

    None means "no key" — those runs are what /unassigned lists.
    """
    return apply_pattern(config.get().job_key_re, jobname)


def grouping_enabled() -> bool:
    """Whether a job-name grouping pattern is configured."""
    return config.get().job_key_re is not None


def key_sql(alias: str = "") -> str:
    """SQL expression for the configured grouping key or the row identity.

    Pass a table alias when joining jobs and job_media to qualify shared columns.
    """
    prefix = f"{alias}." if alias else ""
    if config.get().job_key_re is not None:
        return f"job_key({prefix}jobname)"
    # Job IDs are machine-local, so the row key includes machine_id.
    return (
        f"({prefix}machine_id || '.' || {prefix}source_date || '.' || "
        f"{prefix}jobid || '.' || {prefix}line_seq)"
    )
