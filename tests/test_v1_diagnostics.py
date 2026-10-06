from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "roborock_plus"
    / "v1_diagnostics.py"
)
SPEC = spec_from_file_location("roborock_plus_v1_diagnostics", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

describe_v1_raw_message = MODULE.describe_v1_raw_message
install_v1_raw_message_diagnostics = MODULE.install_v1_raw_message_diagnostics


class CapturingLogger:
    def __init__(self) -> None:
        self.debug_messages: list[tuple[str, tuple[object, ...]]] = []

    def debug(self, message: str, *args: object) -> None:
        self.debug_messages.append((message, args))


def test_describe_v1_raw_message_extracts_dps_json_payload() -> None:
    message = SimpleNamespace(protocol=5, payload=b'{"dps":{"121":17,"122":100}}')

    description = describe_v1_raw_message(message)

    assert description == {
        "protocol": "5",
        "payload_length": 28,
        "dps": {"121": 17, "122": 100},
    }


def test_describe_v1_raw_message_truncates_non_json_payload() -> None:
    message = SimpleNamespace(protocol=102, payload=b"x" * 180)

    description = describe_v1_raw_message(message)

    assert description["protocol"] == "102"
    assert description["payload_length"] == 180
    assert description["payload_preview"] == ("x" * 117) + "..."


def test_install_v1_raw_message_diagnostics_wraps_existing_callback() -> None:
    calls: list[object] = []

    def original_callback(message: object) -> None:
        calls.append(message)

    channel = SimpleNamespace(_callback=original_callback)
    device = SimpleNamespace(duid="abc123", _channel=channel)
    logger = CapturingLogger()
    raw_message = SimpleNamespace(protocol=5, payload=b'{"dps":{"121":17}}')

    assert install_v1_raw_message_diagnostics(device, logger) is True

    channel._callback(raw_message)

    assert calls == [raw_message]
    assert logger.debug_messages == [
        (
            "Roborock Plus V1 raw message duid=%s data=%s",
            (
                "abc123",
                {
                    "protocol": "5",
                    "payload_length": 18,
                    "dps": {"121": 17},
                },
            ),
        )
    ]


def test_install_v1_raw_message_diagnostics_is_idempotent() -> None:
    channel = SimpleNamespace(_callback=lambda message: None)
    device = SimpleNamespace(duid="abc123", _channel=channel)
    logger = CapturingLogger()

    assert install_v1_raw_message_diagnostics(device, logger) is True
    wrapped = channel._callback
    assert install_v1_raw_message_diagnostics(device, logger) is False

    assert channel._callback is wrapped
