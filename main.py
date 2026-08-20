import argparse
import asyncio
import os
import sys
import logging
import uvicorn
from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# Ensure local packages are resolvable even in isolated python environments
sys.path.insert(0, PROJECT_ROOT)

from gptmoss.core import (EventBus, StateEngine, ContextEngine, ExecutionEngine, RuntimeKernel, Event,
                          DEFAULT_SYSTEM_PROMPT, TraceRecorder, SkillRegistry, ArtifactStore,
                          AgentProfileRegistry, AutonomousSkillLifecycle, RuntimeSettings)
from gptmoss.providers import QwenProvider
from gptmoss.memory import JSONMemoryProvider
from gptmoss.capabilities import (
    AgentCapability,
    DeveloperTeamCapability,
    DocumentCapability,
    FilesystemCapability,
    MemoryCapability,
    ShellCapability,
)
from gptmoss.planners import SimplePlanner
from gptmoss.policies import SimplePolicyProvider
from gptmoss.api import init_app

# Console logging first; the workspace file handler is attached after bootstrap.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("gptmoss")


def _attach_workspace_log(workspace_root: str) -> None:
    """Keep the rotating application log inside the runtime workspace."""
    log_path = os.path.join(os.path.abspath(workspace_root), "app.log")
    root = logging.getLogger()
    for handler in list(root.handlers):
        if not isinstance(handler, logging.FileHandler):
            continue
        current = os.path.abspath(getattr(handler, "baseFilename", "") or "")
        if current == os.path.abspath(log_path):
            return
        root.removeHandler(handler)
        handler.close()
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root.addHandler(file_handler)

def bootstrap_runtime(workspace_root: str):
    """
    Bootstrap the MOSS Runtime Platform, gluing all plugins and core engines.
    """
    # Create workspace dir if it doesn't exist
    os.makedirs(workspace_root, exist_ok=True)
    _attach_workspace_log(workspace_root)

    # 1. Core Engines
    event_bus = EventBus()
    state_engine = StateEngine(persist_path=os.path.join(workspace_root, "state_store.json"))
    telemetry = TraceRecorder(os.path.join(workspace_root, "telemetry.jsonl"))
    skill_registry = SkillRegistry([
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "gptmoss", "skills"),
        os.path.join(workspace_root, "skills"),
    ])



    memory_provider = JSONMemoryProvider(os.path.join(workspace_root, "memories.json"))
    context_engine = ContextEngine(state_engine, memory_provider)

    # 2. Load or initialize config.json
    import json
    config_path = os.path.join(workspace_root, "config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
        except Exception as e:
            logger.error(f"Error loading config.json: {e}")
            config_data = {}
    else:
        config_data = {}

    config_data.setdefault(
        "api_key", os.getenv("OPENAI_API_KEY") or os.getenv("DASHSCOPE_API_KEY") or "mock-key"
    )
    config_data.setdefault("base_url", os.getenv("OPENAI_BASE_URL") or RuntimeSettings().base_url)
    config_data.setdefault("model_name", os.getenv("OPENAI_MODEL_NAME") or "qwen-turbo")
    # Keep first-run bootstrap aligned with the documented .env contract.
    # Pydantic performs the strict boolean conversion (for example
    # ``SSL_VERIFY=False``); an existing config.json remains authoritative.
    if os.getenv("SSL_VERIFY") is not None:
        config_data.setdefault("ssl_verify", os.getenv("SSL_VERIFY"))
    if os.getenv("SSL_CERT_PATH") is not None:
        config_data.setdefault("ssl_cert_path", os.getenv("SSL_CERT_PATH"))
    config_data.setdefault("workspace_path", os.path.abspath(workspace_root))
    settings = RuntimeSettings.model_validate(config_data).normalized()
    api_key = settings.api_key
    base_url = settings.base_url
    model_name = settings.model_name
    vision_mode = settings.vision_mode
    ssl_verify = settings.ssl_verify
    ssl_cert_path = settings.ssl_cert_path
    denied_capabilities = settings.denied_capabilities
    approval_required = settings.approval_required_capabilities
    workspace_full_autonomy = settings.workspace_full_autonomy
    continue_while_progress = settings.continue_while_progress
    adaptive_resource_management = settings.adaptive_resource_management
    strict_skill_capabilities = settings.strict_skill_capabilities
    allow_nested_delegation = settings.allow_nested_delegation
    max_delegation_depth = settings.max_delegation_depth
    autonomous_specialization = settings.autonomous_specialization
    autonomous_skill_creation = settings.autonomous_skill_creation
    autonomous_skill_improvement = settings.autonomous_skill_improvement
    workspace_path = settings.workspace_path
    restrict_to_workspace = settings.restrict_to_workspace
    allow_subfolders = settings.allow_subfolders
    max_context_chars = settings.max_context_chars
    max_upload_bytes = settings.max_upload_bytes
    max_attachment_text_chars = settings.max_attachment_text_chars
    artifact_store = ArtifactStore(
        workspace_path,
        max_bytes=max_upload_bytes,
        max_text_chars=max_attachment_text_chars,
    )
    max_step_iterations = settings.max_step_iterations
    max_step_retries = settings.max_step_retries
    max_parallel_plan_steps = settings.max_parallel_plan_steps
    skill_coverage_threshold = settings.skill_coverage_threshold
    max_autonomous_skills_per_execution = settings.max_autonomous_skills_per_execution
    safe_shell_mode = settings.safe_shell_mode
    shell_timeout_seconds = settings.shell_timeout_seconds
    shell_max_output_chars = settings.shell_max_output_chars
    default_skills = settings.default_skills
    projects = settings.projects
    state_engine.max_transitions_per_execution = settings.max_transitions_per_execution
    context_engine.max_history_chars = max_context_chars

    config_data = settings.model_dump()
    
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving config.json: {e}")

    # 3. Providers (OpenAI-compatible / Qwen)
    llm_provider = QwenProvider(
        api_key=api_key,
        base_url=base_url,
        default_model=model_name,
        ssl_verify=ssl_verify,
        ssl_cert_path=ssl_cert_path,
        context_window_tokens=settings.context_window_tokens,
        context_output_reserve_tokens=settings.context_output_reserve_tokens,
    )
    llm_provider.set_vision_mode(vision_mode)

    # 4. Planners and Policies
    planner = SimplePlanner(llm_provider)
    policy_provider = SimplePolicyProvider(
        approval_required_capabilities=approval_required,
        denied_capabilities=denied_capabilities,
        workspace_full_autonomy=workspace_full_autonomy,
    )
    skill_registry.discover(os.path.join(workspace_path, "skills"))
    agent_profile_registry = AgentProfileRegistry(workspace_path)
    skill_lifecycle = AutonomousSkillLifecycle(
        workspace_path, skill_registry,
        coverage_threshold=skill_coverage_threshold,
        max_skills_per_execution=max_autonomous_skills_per_execution,
        creation_enabled=autonomous_skill_creation,
        improvement_enabled=autonomous_skill_improvement,
    )

    # 4. Execution Engine and register standard capabilities
    exec_engine = ExecutionEngine(
        event_bus=event_bus,
        state_engine=state_engine,
        context_engine=context_engine,
        llm_provider=llm_provider,
        planner=planner,
        policy_provider=policy_provider,
        telemetry=telemetry,
        skill_registry=skill_registry,
        artifact_store=artifact_store,
        default_skills=default_skills,
        max_step_iterations=max_step_iterations,
        max_step_retries=max_step_retries,
        max_parallel_plan_steps=max_parallel_plan_steps,
        continue_while_progress=continue_while_progress,
        agent_profile_registry=agent_profile_registry,
        skill_lifecycle=skill_lifecycle,
        autonomous_specialization=autonomous_specialization,
        adaptive_resource_management=adaptive_resource_management,
        strict_skill_capabilities=strict_skill_capabilities,
        allow_nested_delegation=allow_nested_delegation,
        max_delegation_depth=max_delegation_depth,
        document_engine_enabled=settings.document_engine_enabled,
        document_checkpoint_enabled=settings.document_checkpoint_enabled,
        document_target_section_words=settings.document_target_section_words,
        diagram_rendering=settings.diagram_rendering,
        docx_embed_diagrams=settings.docx_embed_diagrams,
    )

    # Register capabilities
    filesystem_cap = FilesystemCapability(workspace_path, state_engine)
    filesystem_cap.update_workspace_config(workspace_path, restrict_to_workspace, allow_subfolders)
    exec_engine.register_capability("filesystem", filesystem_cap)

    document_cap = DocumentCapability(artifact_store)
    exec_engine.register_capability("documents", document_cap)

    memory_cap = MemoryCapability(memory_provider)
    exec_engine.register_capability("memory", memory_cap)
    
    shell_cap = ShellCapability(workspace_path, state_engine, safe_mode=safe_shell_mode, timeout_seconds=shell_timeout_seconds, max_output_chars=shell_max_output_chars)
    exec_engine.register_capability("shell", shell_cap)
    
    agent_capability = AgentCapability(kernel=None, workspace_root=workspace_path)
    exec_engine.register_capability("agent", agent_capability)

    devteam_capability = DeveloperTeamCapability(kernel=None, workspace_root=workspace_path)
    exec_engine.register_capability("devteam", devteam_capability)

    # 5. Runtime Kernel
    kernel = RuntimeKernel(
        event_bus=event_bus,
        state_engine=state_engine,
        execution_engine=exec_engine
    )

    # Link the kernel to allow the capabilities to spawn sub-agents
    agent_capability.kernel = kernel
    devteam_capability.kernel = kernel

    return kernel, exec_engine, state_engine, event_bus

async def run_cli_mode(task: str, workspace_root: str):
    """Run in standalone CLI loop mode."""
    kernel, exec_engine, state_engine, event_bus = bootstrap_runtime(workspace_root)
    state_engine.start_db_flush_loop(event_bus)

    # Set up console log listener for all event bus events
    async def log_event(event: Event):
        logger.info(f"[EVENT] {event.type} -> {event.payload}")
        if event.type == "ApprovalRequested":
            print(f"\n[APPROVAL REQUIRED] Action: {event.payload.get('capability')}.{event.payload.get('action')}")
            print(f"Arguments: {event.payload.get('arguments')}")
            print(f"Reason: {event.payload.get('reason')}")
            # In CLI mode, prompt user for input
            choice = input("Approve action? (y/n): ").strip().lower()
            decision = "allow" if choice in ('y', 'yes') else "reject"
            reason = "Approved via CLI" if decision == "allow" else "Rejected via CLI"
            
            # Resume execution
            asyncio.create_task(exec_engine.resume_with_decision(
                event.payload["execution_id"],
                decision=decision,
                reason=reason
            ))

    event_bus.subscribe_all(log_event)

    logger.info(f"Submitting CLI task: {task}")
    agent_config = {
        "system_prompt": DEFAULT_SYSTEM_PROMPT
    }
    exec_id = await kernel.submit_task(task, agent_config)

    # Wait until execution completes, fails, or paused (if paused, log_event handler will ask for approval and resume)
    while True:
        await asyncio.sleep(0.5)
        state = state_engine.get_execution(exec_id)
        if state.status in ("completed", "failed", "cancelled"):
            logger.info(f"Task finished with status: {state.status}")
            break

def main():
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
    
    parser = argparse.ArgumentParser(description="MOSS Agent Runtime Platform")
    parser.add_argument("--host", default="127.0.0.1", help="API server host")
    parser.add_argument("--port", type=int, default=8000, help="API server port")
    parser.add_argument(
        "--workspace",
        default=os.path.join(PROJECT_ROOT, "workspace"),
        help="Agent local workspace folder",
    )
    parser.add_argument("--task", help="Run a single task in CLI mode instead of starting the server")
    args = parser.parse_args()

    if args.task:
        # CLI Mode
        try:
            asyncio.run(run_cli_mode(args.task, args.workspace))
        except KeyboardInterrupt:
            logger.info("CLI execution aborted by user.")
    else:
        # Server Mode
        kernel, exec_engine, state_engine, event_bus = bootstrap_runtime(args.workspace)
        app = init_app(kernel, exec_engine, state_engine, event_bus)
        
        logger.info(f"Starting MOSS Runtime Server on http://{args.host}:{args.port}")
        # Legacy Windows consoles render ANSI escape bytes as visible arrows.
        # Keep colored Uvicorn logs where terminals support them reliably.
        uvicorn.run(app, host=args.host, port=args.port, use_colors=os.name != "nt")

if __name__ == "__main__":
    main()
