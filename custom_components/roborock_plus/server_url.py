"""Helpers for user-supplied Roborock server URLs."""

from __future__ import annotations

from urllib.parse import urlparse

ALLOWED_SCHEMES = ("http", "https")

# Hosts served by Roborock themselves. Requests to these count against the
# official rate limits, so callers must stay conservative when the configured
# base URL points at one of them.
OFFICIAL_CLOUD_HOSTS = (
    "roborock.com",
)


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


def is_official_cloud_url(url: object) -> bool:
    """Return whether a base URL points at Roborock's own servers.

    Unknown or unparseable values are treated as official: being conservative
    about rate limits is the safe default, and only a base URL we can prove is
    self-hosted should unlock aggressive polling.

    A self-hosted server is matched by neither the host suffix nor the
    `api-*.roborock.com` style host, so it returns False.
    """
    if not isinstance(url, str):
        return True
    host = urlparse(url.strip()).hostname
    if not host:
        return True
    host = host.lower()
    return any(
        host == official or host.endswith(f".{official}")
        for official in OFFICIAL_CLOUD_HOSTS
    )
