import json
from types import SimpleNamespace

import pytest

from gptmoss.core.artifacts import ArtifactStore
from gptmoss.core.delivery import build_delivery_contract
from gptmoss.core.execution import ExecutionEngine, normalize_plan
from gptmoss.core.workload import (
    MAX_SOURCE_PARTITIONS,
    build_workload_profile,
    compile_work_graph,
    partition_attachment_ids,
)
from gptmoss.planners.simple import SimplePlanner
from gptmoss.providers.qwen import QwenProvider
from gptmoss.providers.qwen_support import ContextWindowPolicy


class MetadataStore:
    DOCUMENT_TYPES = {"text/plain"}
    IMAGE_TYPES = {"image/png"}

    def __init__(self, records):
        self.records = {item["id"]: item for item in records}

    def get(self, artifact_id):
        return dict(self.records[artifact_id])


def _source_plan():
    return normalize_plan({
        "analysis": {},
        "requirements": [{"id": "REQ-001", "statement": "Use every source."}],
        "steps": [
            {
                "id": "inventory",
                "role": "architect",
                "specialist": "Corpus analyst",
                "description": "Inventory the local corpus and extract evidence.",
                "dependencies": [],
                "required_artifacts": ["analysis/corpus.md"],
                "owned_paths": ["analysis/corpus.md"],
                "requirement_ids": ["REQ-001"],
            },
            {
                "id": "write",
                "role": "writer",
                "specialist": "Writer",
                "description": "Write the deliverable from validated evidence.",
                "dependencies": ["inventory"],
                "required_artifacts": ["deliverable.md"],
                "owned_paths": ["deliverable.md"],
                "requirement_ids": ["REQ-001"],
            },
            {
                "id": "audit",
                "role": "coordinator",
                "specialist": "Auditor",
                "description": "Audit final requirement coverage.",
                "dependencies": ["write"],
                "requirement_ids": ["REQ-001"],
            },
        ],
    })


def test_large_workload_profile_is_bounded_and_does_not_load_source_content():
    records = [
        {
            "id": f"doc-{index}", "content_type": "text/plain",
            "size_bytes": 2_000, "document_blocks": 4, "document_chunks": 2,
        }
        for index in range(3_647)
    ] + [
        {"id": f"img-{index}", "content_type": "image/png", "size_bytes": 1_000}
        for index in range(4_370)
    ]
    profile = build_workload_profile(
        MetadataStore(records),
        [item["id"] for item in records],
        corpus_summaries=[{"skipped_count": 9_011, "error_count": 92}],
        supports_vision=True,
    )

    assert profile["document_count"] == 3_647
    assert profile["image_count"] == 4_370
    assert profile["ignored_count"] == 9_011
    assert profile["failed_count"] == 92
    assert profile["suggested_partitions"] > 12
    assert profile["suggested_partitions"] <= MAX_SOURCE_PARTITIONS
    assert not any("path" in key or "content" in key for key in profile)


def test_work_graph_compiler_partitions_only_source_work_and_rejoins_dependencies():
    compiled = compile_work_graph(
        _source_plan(), {"suggested_partitions": 4, "document_count": 2_000}
    )
    steps = compiled["steps"]
    shards = [step for step in steps if step.get("source_partition", {}).get("count") == 4]
    consolidation = next(step for step in steps if step["operation"] == "consolidate")
    writer = next(step for step in steps if step["specialist"] == "Writer")

    assert len(shards) == 4
    assert consolidation["dependencies"] == [step["id"] for step in shards]
    assert writer["dependencies"] == [consolidation["id"]]
    assert partition_attachment_ids(
        ["one", "two"], consolidation["source_partition"]
    ) == []
    normalize_plan(compiled)


def test_source_partitions_cover_each_attachment_once():
    artifact_ids = [f"item-{index}" for index in range(103)]
    partitions = [
        partition_attachment_ids(
            artifact_ids, {"index": index, "count": 7, "strategy": "stable_round_robin"}
        )
        for index in range(7)
    ]

    flattened = [item for partition in partitions for item in partition]
    assert len(flattened) == len(set(flattened)) == len(artifact_ids)
    assert set(flattened) == set(artifact_ids)


def test_dependency_handoff_keeps_every_partition_under_one_global_budget():
    results = [
        {
            "step_id": index,
            "role": "architect",
            "description": f"partition {index}",
            "delivery": {"summary": str(index) + ("x" * 4_000)},
        }
        for index in range(12)
    ]

    bounded = ExecutionEngine._bounded_dependency_results(results, 8_000)
    encoded = json.dumps(bounded, ensure_ascii=False)

    assert len(bounded) == 12
    assert {item["step_id"] for item in bounded} == set(range(12))
    assert all(item["delivery_compacted"] for item in bounded)
    assert len(encoded) < 8_000


def test_plan_validation_uses_semantic_coverage_not_a_fixed_step_floor():
    analysis = {
        "level": "very_high", "score": 99, "domains": ["general"],
        "requested_outcomes": 10, "suggested_min_steps": 12,
    }
    plan = _source_plan()

    SimplePlanner._validate_generated_plan(plan, analysis, "auto")
    contract = build_delivery_contract(plan, "Use every source.")
    row = contract["traceability"][0]
    assert row["implementation_steps"]
    assert row["validation_steps"]


def test_provider_preflights_exact_context_window_and_reserves_output():
    provider = object.__new__(QwenProvider)
    provider.context_window_tokens = 262_144
    provider.context_output_reserve_tokens = 8_192
    provider._learned_context_tokens = None
    request = provider._fit_context_request({
        "messages": [
            {"role": "system", "content": "rules " * 100_000},
            {"role": "user", "content": "source " * 300_000},
        ],
        "tools": [{"type": "function", "function": {"name": "read", "parameters": {}}}],
    })
    total = (
        ContextWindowPolicy.estimate_tokens(request["messages"])
        + ContextWindowPolicy.estimate_tokens({"tools": request["tools"], "tool_choice": None})
        + request["max_tokens"]
    )

    assert request["max_tokens"] == 8_192
    assert total < 262_144
    assert "system contract compacted" in request["messages"][0]["content"]


def test_provider_preserves_an_explicit_small_output_limit():
    provider = object.__new__(QwenProvider)
    provider.context_window_tokens = 4_096
    provider.context_output_reserve_tokens = 1_024
    provider._learned_context_tokens = None

    request = provider._fit_context_request({
        "messages": [{"role": "user", "content": "Reply OK"}],
        "max_tokens": 1,
    })

    assert request["max_tokens"] == 1


def test_context_compaction_always_makes_progress_for_oversized_system_contract():
    messages = [
        {"role": "system", "content": "contract " * 30_000},
        {"role": "user", "content": "small request"},
    ]

    compacted = QwenProvider._compact_messages(messages, 2_000)

    assert compacted != messages
    assert QwenProvider._message_chars(compacted) <= 2_000
    assert "system contract compacted" in compacted[0]["content"]


def test_image_token_estimate_uses_visual_charge_not_base64_length():
    small = [{"role": "user", "content": [{
        "type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"},
    }]}]
    huge = [{"role": "user", "content": [{
        "type": "image_url", "image_url": {
            "url": "data:image/png;base64," + ("A" * 1_000_000)
        },
    }]}]

    assert ContextWindowPolicy.estimate_tokens(small) == ContextWindowPolicy.estimate_tokens(huge)


@pytest.mark.asyncio
async def test_provider_reports_the_exact_fitted_multimodal_context():
    provider = object.__new__(QwenProvider)
    provider.context_window_tokens = 8_192
    provider.context_output_reserve_tokens = 1_024
    provider._learned_context_tokens = None
    provider._learned_context_chars = None
    observed = []

    class Completions:
        async def create(self, **kwargs):
            return SimpleNamespace()

    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions())
    )
    messages = [
        {"role": "system", "content": "rules " * 20_000},
        {"role": "user", "content": [
            {"type": "text", "text": "[artifact_id:image-1]"},
            {"type": "image_url", "image_url": {
                "url": "data:image/png;base64,AAAA"
            }},
        ]},
    ]

    await provider._create_with_context_recovery(
        {"model": "test", "messages": messages},
        on_context_fitted=lambda fitted: observed.append(fitted),
    )

    assert observed
    assert ContextWindowPolicy.estimate_tokens(observed[-1]) < 8_192


def test_attachment_context_obeys_global_text_and_image_byte_budgets(tmp_path):
    store = ArtifactStore(str(tmp_path))
    documents = [
        store.save_bytes(f"doc-{index}.txt", ("evidence " * 2_000).encode(), "text/plain")
        for index in range(20)
    ]
    images = [
        store.save_bytes(
            f"image-{index}.png", b"\x89PNG\r\n\x1a\n" + bytes([index]) * 1_000,
            "image/png",
        )
        for index in range(6)
    ]
    items = store.context_items(
        [item["id"] for item in [*documents, *images]],
        supports_vision=True,
        max_text_chars=4_000,
        max_items=5,
        max_images=6,
        max_image_bytes=2_100,
    )

    text_chars = sum(len(str(item.get("text") or "")) for item in items)
    image_payloads = [item["image_url"] for item in items if item.get("image_url")]
    assert text_chars <= 4_000
    assert len(image_payloads) <= 2
    assert any(item["id"] == "corpus-selection-summary" for item in items)
