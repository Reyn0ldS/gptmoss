import argparse
import asyncio
import os
import sys
import logging
import uvicorn
from dotenv import load_dotenv

# Ensure local packages are resolvable even in isolated python environments
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gptmoss.core import EventBus, StateEngine, ContextEngine, ExecutionEngine, RuntimeKernel, Event, DEFAULT_SYSTEM_PROMPT, TraceRecorder, SkillRegistry, ArtifactStore
from gptmoss.providers import QwenProvider
from gptmoss.memory import JSONMemoryProvider
from gptmoss.capabilities import FilesystemCapability, ShellCapability, AgentCapability, DeveloperTeamCapability
from gptmoss.planners import SimplePlanner
from gptmoss.policies import SimplePolicyProvider
from gptmoss.api import init_app

# Setup logging (console and file)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("app.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("gptmoss")

def bootstrap_runtime(workspace_root: str):
    """
    Bootstrap the MOSS Runtime Platform, gluing all plugins and core engines.
    """
    # Create workspace dir if it doesn't exist
    os.makedirs(workspace_root, exist_ok=True)

    # 1. Core Engines
    event_bus = EventBus()
    state_engine = StateEngine(persist_path=os.path.join(workspace_root, "state_store.json"))
    telemetry = TraceRecorder(os.path.join(workspace_root, "telemetry.jsonl"))
    artifact_store = ArtifactStore(workspace_root)
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

    api_key = config_data.get("api_key") or os.getenv("OPENAI_API_KEY") or os.getenv("DASHSCOPE_API_KEY") or "mock-key"
    base_url = config_data.get("base_url") or os.getenv("OPENAI_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model_name = config_data.get("model_name") or os.getenv("OPENAI_MODEL_NAME") or "qwen-turbo"
    ssl_verify = config_data.get("ssl_verify", True)
    ssl_cert_path = config_data.get("ssl_cert_path", "")
    denied_capabilities = config_data.get("denied_capabilities", [])
    approval_required = config_data.get("approval_required_capabilities", ["shell", "devteam.approve_quality_gate"])
    workspace_path = config_data.get("workspace_path") or os.path.abspath(workspace_root)
    restrict_to_workspace = config_data.get("restrict_to_workspace", True)
    allow_subfolders = config_data.get("allow_subfolders", True)
    try:
        max_context_chars = int(config_data.get("max_context_chars", 12_000))
    except (TypeError, ValueError):
        max_context_chars = 12_000
    max_context_chars = max(2_000, min(max_context_chars, 100_000))
    try:
        max_step_iterations = int(config_data.get("max_step_iterations", 30))
    except (TypeError, ValueError):
        max_step_iterations = 30
    max_step_iterations = max(1, min(max_step_iterations, 100))
    safe_shell_mode = bool(config_data.get("safe_shell_mode", True))
    try:
        shell_timeout_seconds = int(config_data.get("shell_timeout_seconds", 60))
    except (TypeError, ValueError):
        shell_timeout_seconds = 60
    shell_timeout_seconds = max(1, min(shell_timeout_seconds, 600))
    try:
        shell_max_output_chars = int(config_data.get("shell_max_output_chars", 12_000))
    except (TypeError, ValueError):
        shell_max_output_chars = 12_000
    shell_max_output_chars = max(1_000, min(shell_max_output_chars, 100_000))
    default_skills = [str(skill).lower() for skill in config_data.get("default_skills", []) if isinstance(skill, str)]
    projects = config_data.get("projects") or [{"id": "proj-default", "name": "Projet Par Défaut"}]
    context_engine.max_history_chars = max_context_chars

    config_data = {
        "api_key": api_key,
        "base_url": base_url,
        "model_name": model_name,
        "ssl_verify": ssl_verify,
        "ssl_cert_path": ssl_cert_path,
        "denied_capabilities": denied_capabilities,
        "approval_required_capabilities": approval_required,
        "workspace_path": workspace_path,
        "restrict_to_workspace": restrict_to_workspace,
        "allow_subfolders": allow_subfolders,
        "max_context_chars": max_context_chars,
        "max_step_iterations": max_step_iterations,
        "safe_shell_mode": safe_shell_mode,
        "shell_timeout_seconds": shell_timeout_seconds,
        "shell_max_output_chars": shell_max_output_chars,
        "default_skills": default_skills,
        "projects": projects
    }
    
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving config.json: {e}")

    # 3. Providers (OpenAI-compatible / Qwen)
    llm_provider = QwenProvider(
        api_key=api_key,
        base_url=base_url,
        default_model=model_name
    )
    llm_provider.update_config(api_key, base_url, ssl_verify, ssl_cert_path, model_name)

    # 4. Planners and Policies
    planner = SimplePlanner(llm_provider)
    policy_provider = SimplePolicyProvider(
        approval_required_capabilities=approval_required,
        denied_capabilities=denied_capabilities
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
    )

    # Register capabilities
    filesystem_cap = FilesystemCapability(workspace_path, state_engine)
    filesystem_cap.update_workspace_config(workspace_path, restrict_to_workspace, allow_subfolders)
    exec_engine.register_capability("filesystem", filesystem_cap)
    
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
    load_dotenv()
    
    parser = argparse.ArgumentParser(description="MOSS Agent Runtime Platform")
    parser.add_argument("--host", default="127.0.0.1", help="API server host")
    parser.add_argument("--port", type=int, default=8000, help="API server port")
    parser.add_argument("--workspace", default="./workspace", help="Agent local workspace folder")
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
        uvicorn.run(app, host=args.host, port=args.port)

if __name__ == "__main__":
    main()
