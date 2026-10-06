"""Diagnostics for V1 raw Roborock device messages."""

from __future__ import annotations

import json
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)
_MAX_PAYLOAD_PREVIEW = 120


def describe_v1_raw_message(message: Any) -> dict[str, Any]:
    """Return a compact, log-safe description of a raw V1 message."""
    protocol = getattr(message, "protocol", None)
    payload = getattr(message, "payload", None)
    description: dict[str, Any] = {"protocol": str(protocol)}

    if payload is None:
        description["payload_length"] = 0
        return description

    if isinstance(payload, bytes | bytearray):
        payload_bytes = bytes(payload)
        payload_text = payload_bytes.decode(errors="replace")
    else:
        payload_text = str(payload)
        payload_bytes = payload_text.encode()

    description["payload_length"] = len(payload_bytes)
    parsed_payload = _parse_json_object(payload_text)
    if parsed_payload is not None and isinstance(parsed_payload.get("dps"), dict):
        description["dps"] = parsed_payload["dps"]
        return description

    description["payload_preview"] = _truncate(payload_text)
    return description


def install_v1_raw_message_diagnostics(
    device: Any,
    logger: logging.Logger = _LOGGER,
) -> bool:
    """Wrap the current V1 channel callback to log raw device messages."""
    channel = getattr(device, "_channel", None)
    if channel is None:
        return False
    if getattr(channel, "_roborock_plus_v1_diagnostics_installed", False):
        return False

    original_callback = getattr(channel, "_callback", None)
    if not callable(original_callback):
        return False

    duid = str(getattr(device, "duid", "unknown"))

    def diagnostic_callback(message: Any) -> None:
        logger.debug(
            "Roborock Plus V1 raw message duid=%s data=%s",
            duid,
            describe_v1_raw_message(message),
        )
        original_callback(message)

    setattr(channel, "_roborock_plus_v1_diagnostics_installed", True)
    setattr(channel, "_roborock_plus_v1_diagnostics_original_callback", original_callback)
    setattr(channel, "_callback", diagnostic_callback)
    return True


def _parse_json_object(payload_text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(payload_text)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def _truncate(payload_text: str) -> str:
    if len(payload_text) <= _MAX_PAYLOAD_PREVIEW:
        return payload_text
    return payload_text[: _MAX_PAYLOAD_PREVIEW - 3] + "..."
