"""Pure helpers for safe-zone entity construction."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TypeVar

TCoordinator = TypeVar("TCoordinator")
TEntity = TypeVar("TEntity")


def build_safe_zone_entities(
    coordinators: Iterable[TCoordinator],
    has_entity_factory: Callable[[TCoordinator], TEntity],
    in_entity_factory: Callable[[TCoordinator], TEntity],
    clear_entity_factory: Callable[[TCoordinator], TEntity],
) -> list[TEntity]:
    """Build a flat list of safe-zone entities for the provided coordinators."""
    return [
        entity
        for coordinator in coordinators
        for entity in (
            has_entity_factory(coordinator),
            in_entity_factory(coordinator),
            clear_entity_factory(coordinator),
        )
    ]
