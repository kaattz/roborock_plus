import json
from pathlib import Path


TRANSLATIONS_DIR = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "roborock_plus"
    / "translations"
)


def _flatten(value, out):
    if isinstance(value, dict):
        for nested in value.values():
            _flatten(nested, out)
    elif isinstance(value, list):
        for nested in value:
            _flatten(nested, out)
    elif isinstance(value, str):
        out.append(value)


def test_translation_files_do_not_contain_key_placeholders() -> None:
    bad_values: list[tuple[str, str]] = []
    for path in TRANSLATIONS_DIR.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        strings: list[str] = []
        _flatten(data, strings)
        for value in strings:
            if "%key:" in value:
                bad_values.append((path.name, value))

    assert bad_values == []
