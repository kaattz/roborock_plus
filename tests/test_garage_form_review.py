"""Independent regressions for incomplete garage form recommendations."""

import pytest

from test_garage_form_defaults import defaults, timing_page


@pytest.mark.parametrize(
    "known_travel,expected_timeout",
    [({"open_travel_seconds": 300}, 340), ({"close_travel_seconds": 400}, 440)],
)
def test_missing_other_travel_does_not_erase_known_long_travel_suggestion(
    known_travel, expected_timeout,
):
    handler, page = timing_page(
        open_source="timed", close_source="timed", **known_travel,
    )

    assert defaults(page)["operation_timeout_seconds"] == expected_timeout
    missing = next(
        key for key in ("open_travel_seconds", "close_travel_seconds")
        if key not in known_travel
    )
    assert defaults(page)[missing] is None
    assert missing not in handler._garage_values()
