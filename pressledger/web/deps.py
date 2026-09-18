"""Shared request dependencies, kept separate to avoid circular imports
between app.py and api.py.
"""

import secrets

from fastapi import Header, HTTPException

from .. import config
from ..db import get_conn


def db():
    """One SQLite connection per request — connections are not thread-safe,
    and FastAPI runs synchronous routes in a thread pool."""
    conn = get_conn()
    try:
        yield conn
    finally:
        conn.close()


def bearer_token(header: str | None) -> str:
    """Extract the Bearer credential, or return an empty string.

    Tokens are accepted only in headers to keep them out of URLs and access logs.
    """
    scheme, _, rest = (header or "").partition(" ")
    return rest.strip() if scheme.lower() == "bearer" else ""


def authorize(header: str | None, configured: str) -> None:
    """Raise 503 if exports are disabled, or 401 if authentication fails."""
    if not configured:
        # No open export route — the rows carry customer names.
        raise HTTPException(
            status_code=503,
            detail="The export API is switched off: set api.token in pressledger.toml",
        )
    presented = bearer_token(header)
    # Encoded first: compare_digest() raises TypeError on a non-ASCII str.
    if not presented or not secrets.compare_digest(presented.encode(), configured.encode()):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_token(authorization: str | None = Header(default=None)) -> None:
    authorize(authorization, config.get().api_token)
