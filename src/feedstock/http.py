"""
http.py — a single shared HTTP session with retry/backoff.

Every outbound data call (Yahoo, frankfurter, EIA, FRED) goes through here so
they all get the same resilience: automatic retries with exponential backoff on
transient failures (429 rate-limits and 5xx), a connection pool, and a default
timeout. A scheduled job that fetches from four flaky free endpoints needs this
— a single 503 shouldn't lose the day's data.

`get()` is a drop-in for `requests.get`: it returns a normal Response, so
`.raise_for_status()` / `.json()` work unchanged at the call sites.
"""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DEFAULT_TIMEOUT = 20
_session: requests.Session | None = None


def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,          # 0.5s, 1s, 2s between retries
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def get(url: str, **kwargs) -> requests.Response:
    """GET via the shared retrying session. Same signature as requests.get."""
    global _session
    if _session is None:
        _session = _build_session()
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    return _session.get(url, **kwargs)
