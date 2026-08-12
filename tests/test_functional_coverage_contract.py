"""Executable inventory preventing public GPTMOSS features from becoming untested."""

import ast
import json
import re
from pathlib import Path

from gptmoss.capabilities.agent import AgentCapability
from gptmoss.capabilities.devteam import DeveloperTeamCapability
from gptmoss.capabilities.documents import DocumentCapability
from gptmoss.capabilities.filesystem import FilesystemCapability
from gptmoss.capabilities.shell import ShellCapability
from gptmoss.capabilities.memory import MemoryCapability
from gptmoss.interfaces.capability import get_actions


ROOT = Path(__file__).parents[1]

FEATURE_CONTRACTS = {
    "runtime_gui": {
        "routes": {("GET", "/"), ("GET", "/health"), ("GET", "/readiness"),
                   ("GET", "/api/runtime-control"), ("GET", "/api/diagnostics"),
                   ("GET", "/api/audit")},
        "tests": {"test_gui_management_api_complete_flow", "test_runtime_control_only_exposes_a_managed_loopback_supervisor"},
    },
    "execution_lifecycle": {
        "routes": {("POST", "/executions"), ("GET", "/executions"),
                   ("GET", "/executions/{execution_id}"),
                   ("GET", "/executions/{execution_id}/delivery"),
                   ("GET", "/executions/{execution_id}/metrics"),
                   ("GET", "/executions/{execution_id}/unified-feed"),
                   ("POST", "/executions/{execution_id}/approve"),
                   ("POST", "/executions/{execution_id}/reject"),
                   ("POST", "/executions/{execution_id}/pause"),
                   ("POST", "/executions/{execution_id}/resume"),
                   ("POST", "/executions/{execution_id}/cancel"),
                   ("DELETE", "/executions/{execution_id}"),
                   ("POST", "/executions/clear-all")},
        "tests": {"test_execution_control_api_preserves_transition_chronology",
                  "test_approval_endpoints_record_ordered_scope_decisions",
                  "test_delivery_package_contains_docx_manifest_assurance_and_sources",
                  "test_professional_delivery_download_route_is_scoped_to_execution"},
    },
    "projects_and_documents": {
        "routes": {("GET", "/projects"), ("POST", "/projects"),
                   ("POST", "/artifacts"), ("GET", "/artifacts"),
                   ("GET", "/artifacts/search"),
                   ("GET", "/artifacts/{artifact_id}/preview"),
                   ("DELETE", "/artifacts/{artifact_id}")},
        "tests": {"test_gui_management_api_complete_flow",
                  "test_complete_project_workflow_assigns_once_and_aggregates"},
    },
    "skills_memory_evolution": {
        "routes": {("POST", "/skills"), ("POST", "/skills/import"),
                   ("POST", "/skills/{name}/validate"), ("DELETE", "/skills/{name}"),
                   ("GET", "/skills"), ("GET", "/memory"), ("POST", "/memory"),
                   ("PUT", "/memory/{memory_id}"),
                   ("POST", "/memory/{memory_id}/validate"),
                   ("DELETE", "/memory/{memory_id}"),
                   ("GET", "/agent-profiles"), ("GET", "/evolution")},
        "tests": {"test_gui_management_api_complete_flow",
                  "test_hybrid_memory_requires_validation_and_tracks_provenance"},
    },
    "delegation": {
        "routes": {("GET", "/executions/{execution_id}/subagents"),
                   ("POST", "/executions/{execution_id}/subagents")},
        "tests": {"test_agent_status_and_execute_subtask_cover_terminal_modes",
                  "test_complete_project_workflow_assigns_once_and_aggregates"},
    },
    "settings": {
        "routes": {("GET", "/api/settings"), ("POST", "/api/settings"),
                   ("POST", "/api/settings/test-connection"),
                   ("POST", "/api/settings/reveal-secret")},
        "tests": {"test_every_configuration_field_has_a_runtime_owner",
                  "test_api_settings_preserve_secret_and_context_budget"},
    },
    "realtime": {
        "routes": {("WEBSOCKET", "/ws/events"),
                   ("WEBSOCKET", "/ws/executions/{execution_id}")},
        "tests": {"test_connection_manager_routes_events_in_publication_order"},
    },
}

CAPABILITY_MODES = {
    "filesystem": {"read": "sync-read", "write": "sync-mutation", "list_dir": "sync-read", "delete": "sync-mutation"},
    "documents": {"inventory": "sync-read", "search": "sync-read", "read": "sync-read", "read_chunk": "sync-read"},
    "shell": {"execute": "sync-process"},
    "agent": {"spawn": "async-background", "status": "sync-observation", "execute_subtask": "async-blocking"},
    "devteam": {"approve_quality_gate": "human-gate", "build_project": "async-sequential-pipeline"},
    "memory": {"search": "async-read", "propose": "async-pending-mutation"},
}

CONFIGURATION_OWNERS = {
    "api_key": "llm", "base_url": "llm", "model_name": "llm", "vision_mode": "llm",
    "ssl_verify": "llm", "ssl_cert_path": "llm",
    "denied_capabilities": "policy", "approval_required_capabilities": "policy",
    "workspace_full_autonomy": "policy", "continue_while_progress": "execution",
    "adaptive_resource_management": "execution+context", "strict_skill_capabilities": "execution",
    "allow_nested_delegation": "execution", "max_delegation_depth": "execution",
    "autonomous_specialization": "profiles", "autonomous_skill_creation": "skills",
    "autonomous_skill_improvement": "skills", "skill_coverage_threshold": "skills",
    "max_autonomous_skills_per_execution": "skills", "workspace_path": "capabilities+artifacts",
    "restrict_to_workspace": "filesystem", "allow_subfolders": "filesystem",
    "max_context_chars": "context", "max_upload_bytes": "artifacts",
    "max_attachment_text_chars": "artifacts", "max_transitions_per_execution": "state",
    "max_step_iterations": "execution",
    "max_step_retries": "execution", "safe_shell_mode": "shell",
    "shell_timeout_seconds": "shell", "shell_max_output_chars": "shell",
    "default_skills": "execution", "projects": "api",
}


def _test_names():
    names = set()
    for path in (ROOT / "tests").glob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names.update(
            node.name for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
        )
    return names


def test_every_public_route_is_owned_by_a_behavioral_feature():
    source = (ROOT / "gptmoss" / "api" / "server.py").read_text(encoding="utf-8")
    actual = {(method.upper(), path) for method, path in re.findall(
        r'@app\.(get|post|put|delete|websocket)\("([^"]+)"', source
    )}
    declared = set().union(*(contract["routes"] for contract in FEATURE_CONTRACTS.values()))
    assert actual == declared

    available_tests = _test_names()
    for feature, contract in FEATURE_CONTRACTS.items():
        assert contract["tests"], f"{feature} has no behavioral scenario"
        assert contract["tests"] <= available_tests, f"{feature} references a missing test"


def test_every_capability_action_declares_its_execution_mode():
    classes = {
        "filesystem": FilesystemCapability,
        "documents": DocumentCapability,
        "shell": ShellCapability,
        "agent": AgentCapability,
        "devteam": DeveloperTeamCapability,
        "memory": MemoryCapability,
    }
    actual = {name: set(get_actions(cls)) for name, cls in classes.items()}
    declared = {name: set(actions) for name, actions in CAPABILITY_MODES.items()}
    assert actual == declared
    assert all(mode for actions in CAPABILITY_MODES.values() for mode in actions.values())


def test_every_configuration_field_has_a_runtime_owner():
    template = json.loads((ROOT / "config.json.template").read_text(encoding="utf-8"))
    assert set(template) == set(CONFIGURATION_OWNERS)
    assert all(owner for owner in CONFIGURATION_OWNERS.values())
