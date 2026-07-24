import base64
from pathlib import Path

import pytest

from gptmoss.core.artifacts import ArtifactStore
from gptmoss.core.skills import SkillRegistry


def test_skill_registry_discovers_and_selects_builtin_skill():
    registry = SkillRegistry([str(Path(__file__).resolve().parents[1] / "gptmoss" / "skills")])
    selected = registry.select("Write Python code with tests", requested=["secure-python"])
    assert selected[0].name == "secure-python"
    assert selected[0].allowed_capabilities == ["filesystem", "shell"]


def test_skill_compatibility_report_maps_known_external_tools(tmp_path):
    path = tmp_path / "SKILL.md"
    path.write_text("Use shell_command and apply_patch, then image_gen.", encoding="utf-8")
    report = SkillRegistry().compatibility_report(str(path))
    assert report["mapped"] == {"apply_patch": "filesystem", "shell_command": "shell"}
    assert report["unsupported"] == ["image_gen"]


def test_artifact_store_handles_text_and_rejects_invalid_image(tmp_path):
    store = ArtifactStore(str(tmp_path))
    payload = base64.b64encode(b"# Notes\\nUse a blue theme.").decode("ascii")
    metadata = store.save_base64("notes.md", payload, "text/markdown")
    context = store.context_items([metadata["id"]])
    assert "blue theme" in context[0]["text"]
    assert context[0]["sha256"] == metadata["sha256"]

    with pytest.raises(ValueError, match="Invalid PNG"):
        store.save_base64("bad.png", payload, "image/png")
