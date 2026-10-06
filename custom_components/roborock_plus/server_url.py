"""Helpers for user-supplied Roborock server URLs."""

from __future__ import annotations

from urllib.parse import urlparse

ALLOWED_SCHEMES = ("http", "https")


def is_valid_server_url(url: object) -> bool:
    """Return whether a user-supplied server URL is usable as a base URL.

    Used by the manual/custom region flow so a self-hosted server (such as
    Roborock Local Server) can be reached without hard-coding a cloud host.
    """
    if not isinstance(url, str):
        return False
    parsed = urlparse(url.strip())
    return parsed.scheme in ALLOWED_SCHEMES and bool(parsed.netloc)


def normalize_server_url(url: str) -> str:
    """Return the trimmed server URL that should be stored."""
    return url.strip()
