"""Shared SSH access to the Home Assistant host."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager

import paramiko

DEFAULT_HOST = "192.168.166.68"
DEFAULT_USER = "hass"
PASSWORD_ENV = "ROBOROCK_HA_PASSWORD"
MISSING_PASSWORD_MESSAGE = (
    f"{PASSWORD_ENV} is not set.\n"
    "Set it in the environment; it is deliberately not stored in this "
    "repository, which is public."
)


def password() -> str:
    value = os.environ.get(PASSWORD_ENV)
    if not value:
        raise SystemExit(MISSING_PASSWORD_MESSAGE)
    return value


def host() -> str:
    return os.environ.get("ROBOROCK_HA_HOST", DEFAULT_HOST)


def user() -> str:
    return os.environ.get("ROBOROCK_HA_USER", DEFAULT_USER)


@contextmanager
def connect() -> Iterator[paramiko.SSHClient]:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host(), username=user(), password=password(), timeout=20)
    try:
        yield client
    finally:
        client.close()
