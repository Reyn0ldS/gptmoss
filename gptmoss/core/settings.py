"""Single validated configuration contract for bootstrap, API, GUI and tests."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


DEFAULT_MAX_UPLOAD_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_ATTACHMENT_TEXT_CHARS = 5_000_000
DEFAULT_MAX_TRANSITIONS_PER_EXECUTION = 2_000


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    api_key: str = "mock-key"
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model_name: str = "qwen-turbo"
    vision_mode: str = Field(default="auto", pattern=r"^(auto|enabled|disabled)$")
    ssl_verify: bool = True
    ssl_cert_path: str = ""
    denied_capabilities: list[str] = Field(default_factory=list)
    approval_required_capabilities: list[str] = Field(
        default_factory=lambda: ["shell", "devteam.approve_quality_gate"]
    )
    workspace_full_autonomy: bool = False
    continue_while_progress: bool = True
    adaptive_resource_management: bool = True
    strict_skill_capabilities: bool = False
    allow_nested_delegation: bool = True
    max_delegation_depth: int = Field(default=0, ge=0)
    autonomous_specialization: bool = True
    autonomous_skill_creation: bool = True
    autonomous_skill_improvement: bool = True
    skill_coverage_threshold: int = Field(default=4, ge=1)
    max_autonomous_skills_per_execution: int = Field(default=0, ge=0)
    workspace_path: str = ""
    restrict_to_workspace: bool = True
    allow_subfolders: bool = True
    projects: list[dict[str, Any]] = Field(
        default_factory=lambda: [{"id": "proj-default", "name": "Projet Par Défaut"}]
    )
    max_step_iterations: int = Field(default=30, ge=1)
    max_step_retries: int = Field(default=2, ge=0)
    max_context_chars: int = Field(default=12_000, ge=1)
    max_upload_bytes: int = Field(default=DEFAULT_MAX_UPLOAD_BYTES, ge=1)
    max_attachment_text_chars: int = Field(
        default=DEFAULT_MAX_ATTACHMENT_TEXT_CHARS, ge=1
    )
    max_transitions_per_execution: int = Field(
        default=DEFAULT_MAX_TRANSITIONS_PER_EXECUTION, ge=100
    )
    safe_shell_mode: bool = True
    shell_timeout_seconds: int = Field(default=0, ge=0)
    shell_max_output_chars: int = Field(default=12_000, ge=1)
    default_skills: list[str] = Field(default_factory=list)
    # Long-form delivery controls.  These orchestrate the active provider; no
    # additional model or downloaded weights are implied.
    document_engine_enabled: bool = True
    document_checkpoint_enabled: bool = True
    document_target_section_words: int = Field(default=450, ge=80, le=20_000)
    diagram_rendering: bool = True
    docx_embed_diagrams: bool = True

    def normalized(self) -> "RuntimeSettings":
        self.default_skills = [str(item).lower() for item in self.default_skills]
        return self
