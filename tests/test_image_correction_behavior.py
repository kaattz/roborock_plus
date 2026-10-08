"""Behavioural tests for the map image entity's correction path.

The other wiring tests read `image.py` as text. Text assertions cannot tell
whether the correction actually reaches the returned bytes -- they only show the
call is written down, and they passed a mutation that disabled the correction's
result. These execute the *shipped* `async_image` body against fakes instead, so
the value that comes out is checked rather than the code that produced it.

The method is taken from `image.py` by parsing the module's AST, not retyped
here. A copy would keep passing after the real method changed, which is exactly
the failure these tests exist to catch.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import sys
import textwrap
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

COMPONENT = (
    Path(__file__).resolve().parent.parent / "custom_components" / "roborock_plus"
)


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, COMPONENT / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


render = _load("render_for_image_test", "v1_map_render.py")


def _shipped_async_image():
    """Compile and return the `async_image` method exactly as shipped.

    The body is compiled with the names it refers to injected, so it runs
    unchanged: editing the real method changes what these tests execute.
    """
    source = (COMPONENT / "image.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_image":
            snippet = ast.get_source_segment(source, node)
            assert snippet is not None
            # Dedent to module level so the compiled body can be bound to a
            # plain object as a stand-in for `self`.
            dedented = textwrap.dedent(snippet)
            namespace: dict = {
                "rerender_map_image": render.rerender_map_image,
                "partial": partial,
            }
            exec(compile(dedented, "<image.py:async_image>", "exec"), namespace)
            return namespace["async_image"]
    raise AssertionError("async_image not found in image.py")


SHIPPED_ASYNC_IMAGE = _shipped_async_image()


class FakeHass:
    """Minimal HomeAssistant stand-in that runs executor jobs inline."""

    def __init__(self) -> None:
        self.executor_calls = 0

    async def async_add_executor_job(self, func, *args):
        self.executor_calls += 1
        return func(*args)


class FakeCoordinator:
    def __init__(self, *, resolved, properties_api=None) -> None:
        self._resolved = resolved
        self.properties_api = properties_api or SimpleNamespace()

    def resolve_vacuum_position(self):
        return self._resolved


class FakeImageEntity:
    """The collaborators `async_image` needs, and nothing else."""

    async_image = SHIPPED_ASYNC_IMAGE

    def __init__(self, *, map_content, resolved, raw=b"RAW") -> None:
        self.hass = FakeHass()
        self.coordinator = FakeCoordinator(resolved=resolved)
        self._map_content_value = map_content
        self._corrected_cache: tuple[tuple, bytes] | None = None
        self._raw = raw

    @property
    def _map_content(self):
        return self._map_content_value

    def _parse_map_bytes(self, raw: bytes):
        return SimpleNamespace(image_content=b"RENDERED:" + raw)

    @property
    def _map_content_trait(self):
        return SimpleNamespace(
            converter=SimpleNamespace(parse_map_content=self._parse_map_bytes)
        )

    async def run(self) -> bytes | None:
        """Run the shipped method against these fakes."""
        return await type(self).async_image(self)


def build_map_bytes() -> bytes:
    """Build a real map payload with the live measured positions."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_v1_map_render import build_map

    return build_map(robot=(25727, 24232), charger=(25688, 28538))


def make_content(raw: bytes, *, drawn=(25727, 24232)):
    """Build a map-content stand-in carrying the given raw bytes."""
    position = None if drawn is None else SimpleNamespace(x=drawn[0], y=drawn[1])
    return SimpleNamespace(
        image_content=b"LIBRARY-PNG",
        map_data=SimpleNamespace(vacuum_position=position),
        raw_api_response=raw,
    )


def test_corrected_bytes_are_returned_not_the_library_image() -> None:
    """The whole point: a redrawn map must be what comes out."""
    raw = build_map_bytes()
    entity = FakeImageEntity(
        map_content=make_content(raw),
        resolved=SimpleNamespace(
            x=25688.0, y=28538.0, trusted=True, from_dock=True
        ),
    )
    result = asyncio.run(entity.run())
    assert result is not None
    assert result != b"LIBRARY-PNG"
    assert result.startswith(b"RENDERED:")


def test_library_image_is_returned_when_no_redraw_is_needed() -> None:
    raw = build_map_bytes()
    entity = FakeImageEntity(
        map_content=make_content(raw),
        resolved=SimpleNamespace(
            x=25727.0, y=24232.0, trusted=True, from_dock=False
        ),
    )
    assert asyncio.run(entity.run()) == b"LIBRARY-PNG"


def test_the_executor_is_used_for_the_render() -> None:
    """Blocking work must not run on the event loop."""
    raw = build_map_bytes()
    entity = FakeImageEntity(
        map_content=make_content(raw),
        resolved=SimpleNamespace(
            x=25688.0, y=28538.0, trusted=True, from_dock=True
        ),
    )
    asyncio.run(entity.run())
    assert entity.hass.executor_calls == 1


def test_a_second_identical_fetch_is_served_from_cache() -> None:
    raw = build_map_bytes()
    entity = FakeImageEntity(
        map_content=make_content(raw),
        resolved=SimpleNamespace(
            x=25688.0, y=28538.0, trusted=True, from_dock=True
        ),
    )
    first = asyncio.run(entity.run())
    second = asyncio.run(entity.run())
    assert first == second
    assert entity.hass.executor_calls == 1, "the second fetch re-rendered"


def test_a_changed_position_re_renders() -> None:
    raw = build_map_bytes()
    entity = FakeImageEntity(
        map_content=make_content(raw),
        resolved=SimpleNamespace(
            x=25688.0, y=28538.0, trusted=True, from_dock=True
        ),
    )
    asyncio.run(entity.run())
    entity.coordinator._resolved = SimpleNamespace(
        x=25600.0, y=28400.0, trusted=True, from_dock=True
    )
    asyncio.run(entity.run())
    assert entity.hass.executor_calls == 2


@pytest.mark.parametrize(
    "drawn",
    [(25727, 24232), None],
)
def test_redraw_happens_for_a_docked_robot_either_way(drawn) -> None:
    """Whether or not the map carried a position, the docked case is drawn."""
    raw = build_map_bytes()
    entity = FakeImageEntity(
        map_content=make_content(raw, drawn=drawn),
        resolved=SimpleNamespace(
            x=25688.0, y=28538.0, trusted=True, from_dock=True
        ),
    )
    result = asyncio.run(entity.run())
    assert result is not None
    assert result.startswith(b"RENDERED:")
