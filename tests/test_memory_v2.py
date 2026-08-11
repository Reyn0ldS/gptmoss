from __future__ import annotations

import json

import pytest

from gptmoss.capabilities.memory import MemoryCapability
from gptmoss.memory.json_store import JSONMemoryProvider


@pytest.mark.asyncio
async def test_memory_is_pending_deduplicated_and_project_scoped(tmp_path):
    provider = JSONMemoryProvider(str(tmp_path / "memories.json"))
    first = await provider.store(
        "La rétention approuvée est de trente jours",
        kind="decision", scope="project", project_id="alpha",
    )
    duplicate = await provider.store(
        "  La rétention approuvée est de trente jours  ",
        kind="decision", scope="project", project_id="alpha",
    )
    assert duplicate == first
    assert await provider.search("rétention", project_id="alpha") == []

    assert await provider.validate(first, validated_by="operator")
    assert [item["id"] for item in await provider.search("rétention", project_id="alpha")] == [first]
    assert await provider.search("rétention", project_id="beta") == []


@pytest.mark.asyncio
async def test_validated_supersession_hides_obsolete_memory(tmp_path):
    provider = JSONMemoryProvider(str(tmp_path / "memories.json"))
    previous = await provider.store(
        "Le seuil de capacité est 100", validated=True,
        kind="constraint", scope="project", project_id="alpha",
    )
    replacement = await provider.store(
        "Le seuil de capacité est 250", supersedes_id=previous,
        kind="constraint", scope="project", project_id="alpha",
    )
    assert [item["id"] for item in await provider.search("capacité", project_id="alpha")] == [previous]
    await provider.validate(replacement, validated_by="operator")
    assert [item["id"] for item in await provider.search("capacité", project_id="alpha")] == [replacement]
    assert provider.list_memories(project_id="alpha")[1]["superseded_by"] == replacement


@pytest.mark.asyncio
async def test_agent_can_only_propose_then_search_validated_project_memory(tmp_path):
    provider = JSONMemoryProvider(str(tmp_path / "memories.json"))
    capability = MemoryCapability(provider)
    context = {"execution_id": "exec-1", "variables": {"project_id": "alpha"}}
    proposed = json.loads(await capability.propose(
        "Le format de livraison préféré est DOCX", kind="preference",
        source_artifacts=["artifact-1"], context=context,
    ))
    assert proposed["status"] == "pending_human_validation"
    assert json.loads(await capability.search("DOCX", context=context))["memories"] == []
    await provider.validate(proposed["id"], validated_by="operator")
    result = json.loads(await capability.search("DOCX", context=context))
    assert result["memories"][0]["source_execution_id"] == "exec-1"
    assert result["memories"][0]["source_artifacts"] == ["artifact-1"]
