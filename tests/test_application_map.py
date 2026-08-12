import json
from pathlib import Path

from scripts.validate_application_map import (
    MAP_PATH,
    discover_capabilities,
    discover_events,
    discover_routes,
    validate,
)


def test_living_application_map_matches_repository_contracts():
    assert validate() == []


def test_application_map_covers_the_public_runtime_surfaces():
    mapping = json.loads(MAP_PATH.read_text(encoding="utf-8"))

    assert len(discover_routes()) == len(mapping["api_routes"])
    assert discover_capabilities() == mapping["capabilities"]
    assert discover_events() == set(mapping["events"])
    assert set(mapping["execution_statuses"]) == {
        "pending",
        "running",
        "paused",
        "waiting_provider",
        "cancelled",
        "completed",
        "failed",
    }


def test_cartography_documents_are_linked_and_nonempty():
    root = Path(__file__).resolve().parents[1]
    mapping = json.loads(MAP_PATH.read_text(encoding="utf-8"))

    for relative in mapping["documents"]:
        content = (root / relative).read_text(encoding="utf-8")
        assert len(content.splitlines()) >= 20, relative
