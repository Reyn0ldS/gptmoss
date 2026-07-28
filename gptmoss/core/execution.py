import asyncio
import json
import time
import logging
import inspect
from typing import Dict, Any, List, Optional
from gptmoss.core.event_bus import Event, EventBus
from gptmoss.core.state import StateEngine
from gptmoss.core.context import ContextEngine
from gptmoss.interfaces.llm import LLMProvider
from gptmoss.interfaces.planner import PlannerProvider
from gptmoss.interfaces.policy import PolicyProvider, PolicyDecision
from gptmoss.interfaces.capability import generate_action_schema, get_actions
from gptmoss.core.observability import TraceRecorder
from gptmoss.core.skills import SkillRegistry
from gptmoss.core.artifacts import ArtifactStore

ROLE_DISPLAY_NAMES = {
    "architect": "Architecte",
    "security": "Analyste Sécurité",
    "developer": "Développeur",
    "qa": "Testeur QA",
    "debugger": "Débugueur",
    "writer": "Rédacteur Technique",
    "coordinator": "Coordinateur",
}

ROLE_ALIASES = {
    "architect": "architect", "architecte": "architect", "analyst": "architect", "analyste": "architect",
    "security": "security", "sécurité": "security", "reviewer": "security", "analyste sécurité": "security",
    "developer": "developer", "développeur": "developer", "coder": "developer", "codeur": "developer",
    "qa": "qa", "tester": "qa", "testeur": "qa", "testeur qa": "qa",
    "debugger": "debugger", "debug": "debugger", "débugueur": "debugger", "bug fixer": "debugger",
    "writer": "writer", "rédacteur": "writer", "rédacteur technique": "writer", "documentation": "writer",
    "coordinator": "coordinator", "coordinateur": "coordinator", "summary": "coordinator",
}

def canonical_step_role(value: Any) -> Optional[str]:
    if value is None:
        return None
    return ROLE_ALIASES.get(str(value).strip().lower())

def infer_step_role(description: str) -> Optional[str]:
    desc_lower = str(description or "").lower()
    # Debugger descriptions commonly contain "tests"; match them before QA.
    if any(marker in desc_lower for marker in ("debug", "bug fixer", "débug", "corriger les erreurs")):
        return "debugger"
    if any(marker in desc_lower for marker in ("architect", "architecte", "technical specification", "spécification technique")):
        return "architect"
    if any(marker in desc_lower for marker in ("security", "sécurité", "compliance reviewer", "revue de conformité")):
        return "security"
    if any(marker in desc_lower for marker in ("qa", "tester", "testeur", "testing engineer", "unit tests")):
        return "qa"
    if any(marker in desc_lower for marker in ("developer", "coder", "développeur", "codeur")):
        return "developer"
    if any(marker in desc_lower for marker in ("technical writer", "writer", "rédacteur", "documentation")):
        return "writer"
    return None

def parse_step_role(description: str) -> Optional[str]:
    role = infer_step_role(description)
    return ROLE_DISPLAY_NAMES.get(role) if role else None

def normalize_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a planner response and normalize its stable execution contract."""
    if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list):
        raise ValueError("A plan must contain a list of steps.")
    steps = plan["steps"]
    identifiers = []
    identifier_keys = set()
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(f"Plan step {index} must be an object.")
        step_id = step.get("id", index)
        identifier_key = str(step_id)
        if (
            isinstance(step_id, bool) or not isinstance(step_id, (int, str))
            or identifier_key in identifier_keys
        ):
            raise ValueError(f"Plan step {index} has an invalid or duplicate id.")
        identifiers.append(step_id)
        identifier_keys.add(identifier_key)
        step["id"] = step_id
        step["description"] = str(step.get("description") or "").strip()
        if not step["description"]:
            raise ValueError(f"Plan step {step_id} has no description.")
        dependencies = step.get("dependencies") or []
        if (
            not isinstance(dependencies, list)
            or any(isinstance(dep, bool) or not isinstance(dep, (int, str)) for dep in dependencies)
            or len(set(map(str, dependencies))) != len(dependencies)
        ):
            raise ValueError(f"Plan step {step_id} has invalid dependencies.")
        step["dependencies"] = dependencies
        requested_role = step.get("role")
        role = canonical_step_role(requested_role) if requested_role is not None else infer_step_role(step["description"])
        if requested_role is not None and not role:
            raise ValueError(f"Plan step {step_id} has unsupported role '{requested_role}'.")
        if role:
            step["role"] = role
        step["status"] = step.get("status", "pending")

    identifier_set = set(identifiers)
    for step in steps:
        if step["id"] in step["dependencies"] or any(dep not in identifier_set for dep in step["dependencies"]):
            raise ValueError(f"Plan step {step['id']} references an invalid dependency.")

    completed = set()
    while len(completed) < len(steps):
        ready = [step["id"] for step in steps if step["id"] not in completed and set(step["dependencies"]) <= completed]
        if not ready:
            raise ValueError("Plan contains cyclical dependencies.")
        completed.update(ready)
    return plan


logger = logging.getLogger("gptmoss.execution")

class ExecutionEngine:
    """
    Execution Engine handles the execution loop of tasks step-by-step.
    Orchestrates LLM calls, capability execution, policy checks, and human approval flows.
    """
    def __init__(
        self,
        event_bus: EventBus,
        state_engine: StateEngine,
        context_engine: ContextEngine,
        llm_provider: LLMProvider,
        planner: PlannerProvider,
        policy_provider: PolicyProvider,
        telemetry: Optional[TraceRecorder] = None,
        skill_registry: Optional[SkillRegistry] = None,
        artifact_store: Optional[ArtifactStore] = None,
        default_skills: Optional[List[str]] = None,
        max_step_iterations: int = 30,
    ):
        self.event_bus = event_bus
        self.state_engine = state_engine
        self.context_engine = context_engine
        self.llm_provider = llm_provider
        self.planner = planner
        self.policy_provider = policy_provider
        self.telemetry = telemetry or TraceRecorder()
        self.skill_registry = skill_registry
        self.artifact_store = artifact_store
        self.default_skills = [str(skill).lower() for skill in (default_skills or [])]
        self.max_step_iterations = max(1, min(int(max_step_iterations), 100))
        self._capabilities: Dict[str, Any] = {}  # capability_name -> instance
        self._execution_locks: Dict[str, asyncio.Lock] = {}

    def register_capability(self, capability_name: str, instance: Any):
        """Register instantiated capability."""
        self._capabilities[capability_name.lower()] = instance
        # Ensure standard action methods are populated on instance
        instance.actions = get_actions(instance.__class__)
        logger.info(f"Registered capability: {capability_name}")

    def get_capability(self, capability_name: str) -> Optional[Any]:
        """Retrieve a registered capability by name."""
        return self._capabilities.get(capability_name.lower())

    def get_capabilities_schemas(self, is_sub_agent: bool = False, allowed_capabilities: Optional[set[str]] = None) -> List[Dict[str, Any]]:
        """Generate JSON schemas for all registered capabilities."""
        schemas = []
        for name, inst in self._capabilities.items():
            if allowed_capabilities is not None and name.lower() not in allowed_capabilities:
                continue
            if is_sub_agent and name.lower() in ("agent", "devteam"):
                continue
            for act_name, method in inst.actions.items():
                schemas.append(generate_action_schema(name, act_name, method))
        return schemas

    def _active_skills(self, state, task: str):
        if not self.skill_registry:
            return []
        requested = state.variables.get("requested_skills") or self.default_skills
        selected = self.skill_registry.select(task, requested=requested)
        state.variables["active_skills"] = [{"name": skill.name, "digest": skill.digest} for skill in selected]
        return selected

    @staticmethod
    def _allowed_capabilities(skills) -> Optional[set[str]]:
        allowed = set().union(*(set(skill.allowed_capabilities) for skill in skills)) if skills else set()
        return allowed or None

    async def execute_task(self, execution_id: str, task: str):
        """Run an execution once, even if resume/reconnect schedules it repeatedly."""
        lock = self._execution_locks.setdefault(execution_id, asyncio.Lock())
        if lock.locked():
            self.telemetry.record("duplicate_execution_skipped", execution_id, task=task)
            return
        async with lock:
            state = self.state_engine.get_execution(execution_id)
            if state.status in ("completed", "failed", "cancelled"):
                return
            try:
                await self._execute_task_unlocked(execution_id, task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state.status = "failed"
                state.results["error"] = str(exc)
                self.telemetry.record("execution_failed", execution_id, error=str(exc))
                await self.event_bus.publish(Event(
                    type="ExecutionFailed",
                    payload={"execution_id": execution_id, "error": str(exc)},
                ))

    async def _execute_task_unlocked(self, execution_id: str, task: str):
        """
        Main execution loop for a task.
        """
        state = self.state_engine.get_execution(execution_id)
        convo = self.state_engine.get_conversation(execution_id)
        state.variables.setdefault("task", task)
        self.telemetry.record("execution_started", execution_id, task=task)
        skills = self._active_skills(state, task)
        allowed_capabilities = self._allowed_capabilities(skills)

        # 1. Initialize states if new
        if state.status == "pending":
            state.status = "running"
            
            parent_task = state.variables.get("parent_task")
            if not parent_task:
                parent_id = state.variables.get("parent_execution_id")
                if parent_id and parent_id in self.state_engine.executions:
                    parent_exec = self.state_engine.get_execution(parent_id)
                    parent_task = parent_exec.variables.get("parent_task")
            if not parent_task:
                parent_task = task
                
            state.variables["parent_task"] = parent_task
            
            await self.event_bus.publish(Event(
                type="ExecutionStarted",
                payload={"execution_id": execution_id, "task": task}
            ))
            
            # Initial convo message containing parent context if sub-agent
            if parent_task and parent_task != task:
                convo.messages.append({
                    "role": "user",
                    "content": f"Main Project Task: {parent_task}\nYour Specific Subtask: {task}",
                    "timestamp": time.time()
                })
            else:
                convo.messages.append({"role": "user", "content": f"Task: {task}", "timestamp": time.time()})

        # 2. Plan generation (if not already planned)
        if not state.current_plan:
            is_sub_agent = state.variables.get("parent_execution_id") is not None
            schemas = self.get_capabilities_schemas(is_sub_agent=is_sub_agent, allowed_capabilities=allowed_capabilities)
            context = await self.context_engine.compile_context(
                execution_id=execution_id,
                conversation_id=execution_id,
                agent_id="default_agent",
                capabilities_schemas=schemas,
                extra_query=task
            )
            context["skills"] = [{"name": skill.name, "description": skill.description} for skill in skills]
            await self.event_bus.publish(Event(
                type="ContextBuilt",
                payload={"execution_id": execution_id, "context_summary": "Initial context compiled."}
            ))

            planning_started = time.perf_counter()
            plan_result = await self.planner.plan(
                task, context, schemas,
                parent_execution_id=state.variables.get("parent_execution_id"),
            )
            plan_result = normalize_plan(plan_result)
            self.telemetry.record("plan_generated", execution_id, duration_ms=round((time.perf_counter() - planning_started) * 1000, 2), steps=len(plan_result.get("steps", [])))
            state.current_plan = plan_result
            state.current_step = 0
            await self.event_bus.publish(Event(
                type="PlanGenerated",
                payload={"execution_id": execution_id, "plan": plan_result}
            ))

        state.current_plan = normalize_plan(state.current_plan)
        steps = state.current_plan.get("steps", [])

        # Ensure all steps have a status, resetting stuck 'running' states to 'pending' for resumption
        for step in steps:
            if "status" not in step or step.get("status") == "running":
                step["status"] = "pending"

        # Maintain a map of running asyncio Tasks keying by step ID
        running_tasks = {}
        
        async def run_step(step):
            step["status"] = "running"
            step_index = steps.index(step)
            await self.event_bus.publish(Event(
                type="StepStarted",
                payload={"execution_id": execution_id, "step_index": step_index, "description": step.get("description")}
            ))
            
            try:
                role_key = canonical_step_role(step.get("role")) or infer_step_role(step.get("description", ""))
                role_name = ROLE_DISPLAY_NAMES.get(role_key) if role_key else None
                is_sub_agent = state.variables.get("parent_execution_id") is not None
                
                if role_name and role_key != "coordinator" and not is_sub_agent:
                    # Persist the assignment before scheduling it. A resumed parent
                    # reuses the same child instead of performing the step twice.
                    import uuid
                    sub_id = step.get("assigned_execution_id") or str(uuid.uuid4())
                    is_new_assignment = "assigned_execution_id" not in step
                    step["assigned_execution_id"] = sub_id

                    dependency_results = []
                    for dependency_id in step.get("dependencies", []):
                        dependency_step = next(item for item in steps if item.get("id") == dependency_id)
                        dependency_results.append({
                            "step_id": dependency_id,
                            "role": dependency_step.get("role"),
                            "description": dependency_step.get("description"),
                            "delivery": dependency_step.get("delivery") or dependency_step.get("result"),
                        })
                    handoff = json.dumps(dependency_results, ensure_ascii=False)
                    if len(handoff) > 8_000:
                        handoff = handoff[:8_000] + "\n… [dependency handoff truncated]"
                    sub_task = step["description"]
                    if dependency_results:
                        sub_task += (
                            "\n\nValidated outputs from prerequisite steps are provided below. "
                            "Reuse them; do not redo their work.\n" + handoff
                        )

                    sub_exec = self.state_engine.get_execution(sub_id)
                    if is_new_assignment:
                        sub_exec.status = "pending"
                    sub_exec.variables["role_name"] = role_name
                    sub_exec.variables["role_key"] = role_key
                    sub_exec.variables["parent_execution_id"] = execution_id
                    sub_exec.variables["project_id"] = state.variables.get("project_id", "proj-default")
                    sub_exec.variables["parent_task"] = state.variables.get("parent_task") or task
                    sub_exec.variables["task"] = sub_exec.variables.get("task") or sub_task
                    sub_exec.variables["plan_step_id"] = step.get("id")
                    sub_exec.variables["dependency_results"] = dependency_results
                    sub_exec.variables["attachment_ids"] = state.variables.get("attachment_ids", [])
                    sub_exec.variables["agent_config"] = {
                        "system_prompt": f"You are the specialized {role_name} for this project.",
                        "role_name": role_name,
                    }
                    if state.variables.get("project_path"):
                        sub_exec.variables["project_path"] = state.variables["project_path"]

                    if is_new_assignment:
                        await self.event_bus.publish(Event(
                            type="TaskCreated",
                            payload={
                                "execution_id": sub_id,
                                "parent_execution_id": execution_id,
                                "plan_step_id": step.get("id"),
                                "role": role_key,
                                "task": sub_exec.variables["task"],
                                "agent_id": "default_agent"
                            }
                        ))

                    if sub_exec.status in ("pending", "running"):
                        asyncio.create_task(self.execute_task(sub_id, sub_exec.variables["task"]))
                    
                    # Wait for sub-agent completion
                    while True:
                        await asyncio.sleep(0.1)
                        
                        parent_state = self.state_engine.get_execution(execution_id)
                        sub_state = self.state_engine.get_execution(sub_id)
                        
                        if parent_state.status == "cancelled":
                            if sub_state.status in ("running", "paused", "pending"):
                                sub_state.status = "cancelled"
                                await self.event_bus.publish(Event(
                                    type="ExecutionCancelled",
                                    payload={"execution_id": sub_id}
                                ))
                            break
                        elif parent_state.status == "paused":
                            if sub_state.status == "running":
                                sub_state.status = "paused"
                                await self.event_bus.publish(Event(
                                    type="ExecutionPaused",
                                    payload={"execution_id": sub_id}
                                ))
                            continue
                        
                        # Resume child if parent is resumed
                        if parent_state.status == "running" and sub_state.status == "paused" and not sub_state.variables.get("pending_approval"):
                            sub_state.status = "running"
                            asyncio.create_task(self.execute_task(sub_id, sub_exec.variables["task"]))
                        
                        if sub_state.status in ("completed", "failed", "cancelled"):
                            break
                            
                    if sub_state.status == "completed":
                        delivery = sub_exec.variables.get("delivery")
                        if not isinstance(delivery, dict):
                            sub_conversation = self.state_engine.get_conversation(sub_id)
                            last_response = "Sub-agent execution completed."
                            for msg in reversed(sub_conversation.messages):
                                if msg.get("role") == "assistant" and msg.get("content"):
                                    last_response = msg["content"]
                                    break
                            delivery = self._structured_delivery(last_response)
                        sub_exec.variables["delivery"] = delivery
                        step["delivery"] = delivery
                        result = json.dumps(delivery, ensure_ascii=False)
                    else:
                        raise RuntimeError(f"Sub-agent {role_name} stopped with status: {sub_state.status}")
                else:
                    # Execute step loop locally
                    result = await self._execute_step_loop(execution_id, step)
                
                # If the step execution suspended (e.g. paused for approval), reset to pending
                parent_state = self.state_engine.get_execution(execution_id)
                if parent_state.status == "paused":
                    step["status"] = "pending"
                    return "suspended"
                
                step["status"] = "completed"
                step["result"] = result
                step_record = {
                    "step_id": step.get("id"),
                    "description": step.get("description"),
                    "role": step.get("role") or "coordinator",
                    "execution_id": step.get("assigned_execution_id") or execution_id,
                    "result": step.get("delivery") or result,
                }
                state.results.setdefault("steps", {})[str(step.get("id"))] = step_record
                await self.event_bus.publish(Event(
                    type="StepCompleted",
                    payload={"execution_id": execution_id, "step_index": step_index, "result": result}
                ))
                return "completed"
                
            except Exception as e:
                step["status"] = "failed"
                step["error"] = str(e)
                await self.event_bus.publish(Event(
                    type="StepFailed",
                    payload={"execution_id": execution_id, "step_index": step_index, "error": str(e)}
                ))
                raise e

        # Loop until all steps are completed or execution finishes/pauses/cancels
        while state.status in ("running", "pending"):
            if state.status == "pending":
                state.status = "running"
                
            parent_state = self.state_engine.get_execution(execution_id)
            if parent_state.status in ("paused", "cancelled", "completed", "failed"):
                break
                
            # Find steps ready to execute (all dependencies completed)
            ready_steps = []
            for step in steps:
                step_id = step.get("id")
                if step.get("status") == "pending" and step_id not in running_tasks:
                    deps = step.get("dependencies", [])
                    deps_satisfied = True
                    for dep_id in deps:
                        dep_step = next((s for s in steps if s.get("id") == dep_id), None)
                        if not dep_step or dep_step.get("status") != "completed":
                            deps_satisfied = False
                            break
                    if deps_satisfied:
                        ready_steps.append(step)
                        
            # If no steps ready and none running, planning is done
            if not ready_steps and not running_tasks:
                all_completed = all(s.get("status") == "completed" for s in steps)
                if all_completed:
                    state.status = "completed"
                    state.results["deliveries"] = [
                        state.results.get("steps", {}).get(str(step.get("id"))) for step in steps
                    ]
                    state.results["deliveries"] = [item for item in state.results["deliveries"] if item]
                    if steps:
                        state.results["final_output"] = steps[-1].get("delivery") or steps[-1].get("result")
                    self.telemetry.record("execution_completed", execution_id, completed_steps=len(steps))
                    state.results["telemetry"] = self.telemetry.metrics(execution_id)
                    await self.event_bus.publish(Event(
                        type="ExecutionCompleted",
                        payload={"execution_id": execution_id, "results": state.results}
                    ))
                break
                
            # Launch all ready steps concurrently
            for step in ready_steps:
                step_id = step.get("id")
                running_tasks[step_id] = asyncio.create_task(run_step(step))
                
            # Wait for at least one step to complete
            if running_tasks:
                done, pending_tasks = await asyncio.wait(
                    running_tasks.values(),
                    return_when=asyncio.FIRST_COMPLETED
                )
                
                step_failure = None
                step_suspended = False
                
                for task_obj in done:
                    step_id = next((sid for sid, tobj in running_tasks.items() if tobj == task_obj), None)
                    if step_id is not None:
                        del running_tasks[step_id]
                        
                    try:
                        res = task_obj.result()
                        if res == "suspended":
                            step_suspended = True
                    except Exception as exc:
                        step_failure = exc
                        
                # Update current step count
                state.current_step = sum(1 for s in steps if s.get("status") == "completed")
                
                if step_suspended:
                    state.status = "paused"
                    for t in running_tasks.values():
                        t.cancel()
                    return
                    
                if step_failure:
                    state.status = "failed"
                    state.results["error"] = str(step_failure)
                    self.telemetry.record("execution_failed", execution_id, error=str(step_failure))
                    for t in running_tasks.values():
                        t.cancel()
                    await self.event_bus.publish(Event(
                        type="ExecutionFailed",
                        payload={"execution_id": execution_id, "error": str(step_failure)}
                    ))
                    return
            else:
                logger.error(f"Execution {execution_id} has unresolvable cyclical step dependencies.")
                state.status = "failed"
                await self.event_bus.publish(Event(
                    type="ExecutionFailed",
                    payload={"execution_id": execution_id, "error": "Cyclical step dependencies detected in plan."}
                ))
                break

    async def _execute_step_loop(self, execution_id: str, step: Dict[str, Any]) -> str:
        """
        Executes a step by running a ReAct-style dialog loop with the LLM.
        """
        state = self.state_engine.get_execution(execution_id)
        convo = self.state_engine.get_conversation(execution_id)
        skills = self._active_skills(state, state.variables.get("parent_task") or step.get("description", ""))
        allowed_capabilities = self._allowed_capabilities(skills)
        
        step_desc = step.get("description", "")
        prerequisite_outputs = state.variables.get("dependency_results") or []
        if not prerequisite_outputs and state.current_plan:
            role_for_step = canonical_step_role(step.get("role")) or infer_step_role(step_desc)
            if role_for_step == "coordinator":
                dependency_ids = [
                    item.get("id") for item in state.current_plan.get("steps", [])
                    if item is not step and item.get("status") == "completed"
                ]
            else:
                dependency_ids = step.get("dependencies", [])
            for dependency_id in dependency_ids:
                dependency_step = next(
                    (item for item in state.current_plan.get("steps", []) if item.get("id") == dependency_id),
                    None,
                )
                if dependency_step:
                    prerequisite_outputs.append({
                        "step_id": dependency_id,
                        "role": dependency_step.get("role"),
                        "description": dependency_step.get("description"),
                        "delivery": dependency_step.get("delivery") or dependency_step.get("result"),
                    })
        # Sub-prompt for the step: only append if not resuming from a pending approval to preserve tool call message ordering
        if not state.variables.get("pending_approval"):
            reuse_instruction = ""
            if prerequisite_outputs:
                reuse_instruction = " Reuse the validated prerequisite outputs supplied in the task; do not repeat their work."
            convo.messages.append({"role": "system", "content": f"Current Step objectives: {step_desc}.{reuse_instruction} Generate thought and select tools if needed.", "timestamp": time.time()})

        iteration = 0

        while iteration < self.max_step_iterations:
            if state.status in ("paused", "cancelled", "failed") and not state.variables.get("pending_approval", {}).get("decision"):
                return f"Execution suspended with status: {state.status}."
            iteration += 1

            # Check if there is a pending approval we just resumed
            pending_app = state.variables.get("pending_approval")
            if pending_app:
                # We have a pending tool call that is now approved or rejected!
                # Remove from pending list
                state.variables.pop("pending_approval")
                tool_call_id = pending_app["tool_call_id"]
                
                # Check if user decision is approved
                decision = pending_app.get("decision", "reject")
                completed_tool_calls = state.variables.setdefault("completed_tool_calls", {})
                if tool_call_id in completed_tool_calls:
                    result_str = completed_tool_calls[tool_call_id]
                elif decision == "allow":
                    # Execute tool call
                    result_str = await self._call_tool(
                        execution_id,
                        pending_app["capability"],
                        pending_app["action"],
                        pending_app["arguments"]
                    )
                else:
                    result_str = f"Execution blocked: human rejection. Reason: {pending_app.get('reason', 'None')}"
                completed_tool_calls[tool_call_id] = result_str

                convo.messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": f"{pending_app['capability']}__{pending_app['action']}",
                    "content": result_str,
                    "timestamp": time.time()
                })
                # Re-emit that the tool completed
                await self.event_bus.publish(Event(
                    type="ToolCompleted",
                    payload={
                        "execution_id": execution_id,
                        "tool_call_id": tool_call_id,
                        "result": result_str
                    }
                ))
                # Continue loop to ask LLM for next thought
                continue

            # Build tools list
            is_sub_agent = state.variables.get("parent_execution_id") is not None
            delegated_plan = (not is_sub_agent) and any(
                canonical_step_role(item.get("role")) not in (None, "coordinator")
                for item in (state.current_plan or {}).get("steps", [])
            )
            schemas = self.get_capabilities_schemas(
                is_sub_agent=is_sub_agent or delegated_plan,
                allowed_capabilities=allowed_capabilities,
            )

            # Compile context
            context = await self.context_engine.compile_context(
                execution_id=execution_id,
                conversation_id=execution_id,
                agent_id="default_agent",
                capabilities_schemas=schemas
            )
            if self.artifact_store and state.variables.get("attachment_ids"):
                context["attachments"] = self.artifact_store.context_items(
                    state.variables["attachment_ids"], getattr(self.llm_provider, "supports_vision", False)
                )

            # Request LLM completion
            llm_messages = []
            role_name = state.variables.get("role_name", "Coordinateur")
            role_key = state.variables.get("role_key") or canonical_step_role(role_name) or "coordinator"
            base_prompt = context.get("system_instructions", "")
            if skills:
                base_prompt += "\\n\\nActive skills:\\n" + "\\n\\n".join(
                    f"[{skill.name}]\\n{skill.instructions}" for skill in skills
                )
            
            if role_key == "architect":
                specialized_prompt = (
                    "You are the Specialized Architect Agent.\n"
                    "Your role is to analyze software requirements, design specifications, and write technical specifications files (e.g. specs.md).\n"
                    "Focus on clear system design, modular structures, and outlining detailed implementation plans for other sub-agents."
                )
            elif role_key == "security":
                specialized_prompt = (
                    "You are the Specialized Security & Compliance Reviewer.\n"
                    "Your role is to check specifications or code for logical flaws, cryptographic vulnerabilities, or input validation risks.\n"
                    "Write detailed security reviews (e.g. security_review.md) highlighting potential issues and proposing mitigations."
                )
            elif role_key == "developer":
                specialized_prompt = (
                    "You are the Specialized Developer/Coder Agent.\n"
                    "Your role is to write clean, high-quality, and fully functional source code.\n"
                    "Avoid writing comments as placeholders; write actual implementation. Follow specs.md guidelines."
                )
            elif role_key == "qa":
                specialized_prompt = (
                    "You are the Specialized QA Testing Engineer.\n"
                    "Your role is to design and write robust unit tests (e.g. pytest tests) to verify the code correctness.\n"
                    "Make sure you cover edge cases, input validation, and boundary conditions."
                )
            elif role_key == "debugger":
                specialized_prompt = (
                    "You are the Specialized Debugger & Bug Fixer.\n"
                    "Your role is to analyze test failure logs, run commands to inspect state, and modify files to fix code syntax or logical errors."
                )
            elif role_key == "writer":
                specialized_prompt = (
                    "You are the Specialized Technical Writer.\n"
                    "Your role is to write detailed project documentation, README.md files, and help guides for users."
                )
            else:
                specialized_prompt = "Coordinate the current step and synthesize prerequisite results without repeating completed work."

            role_prompt = (base_prompt + "\n\n" + specialized_prompt).strip()

            if role_key != "coordinator":
                role_prompt += ("\\nWhen finished, return a JSON object with keys: summary, artifacts, evidence, risks, next_action. "
                                "Use empty arrays or strings when a field does not apply.")
            llm_messages.append({"role": "system", "content": role_prompt})
            for attachment in context.get("attachments", []):
                if attachment.get("text") is not None:
                    llm_messages.append({"role": "user", "content": f"Attached file {attachment['filename']}:\\n{attachment['text']}"})
                elif attachment.get("image_url"):
                    llm_messages.append({"role": "user", "content": [
                        {"type": "text", "text": f"Attached image: {attachment['filename']}"},
                        {"type": "image_url", "image_url": {"url": attachment["image_url"]}},
                    ]})
                else:
                    llm_messages.append({"role": "system", "content": f"Attachment {attachment['filename']}: {attachment['note']}"})
            if context.get("context_summary"):
                llm_messages.append({"role": "system", "content": context["context_summary"]})
            if prerequisite_outputs:
                llm_messages.append({
                    "role": "system",
                    "content": "Validated prerequisite deliveries to synthesize; do not redo them:\n" + json.dumps(
                        prerequisite_outputs, ensure_ascii=False
                    )[:8_000],
                })
            llm_messages.extend(context["conversation_history"])

            await self.event_bus.publish(Event(
                type="LLMRequest",
                payload={"execution_id": execution_id, "messages": llm_messages}
            ))

            llm_started = time.perf_counter()
            llm_response = await self.llm_provider.completion(
                messages=llm_messages,
                tools=schemas if schemas else None
            )
            self.telemetry.record("llm_completed", execution_id, duration_ms=round((time.perf_counter() - llm_started) * 1000, 2), message_count=len(llm_messages), tool_calls=len(llm_response.get("tool_calls") or []))

            await self.event_bus.publish(Event(
                type="LLMResponse",
                payload={"execution_id": execution_id, "response": llm_response}
            ))

            # Store LLM assistant message
            assistant_msg = {
                "role": "assistant",
                "content": llm_response.get("content"),
                "timestamp": time.time()
            }
            if llm_response.get("tool_calls"):
                assistant_msg["tool_calls"] = llm_response["tool_calls"]
            convo.messages.append(assistant_msg)

            # Check for tool calls
            tool_calls = llm_response.get("tool_calls")
            if not tool_calls:
                # If this is the first iteration and no tools have been called yet,
                # let's prompt the agent to perform actions if needed rather than early-exiting.
                has_called_tools_in_step = any(msg.get("role") == "tool" for msg in convo.messages)
                if iteration == 1 and not has_called_tools_in_step:
                    convo.messages.append({
                        "role": "system",
                        "content": "System: You did not call any tools. If you need to perform actions (read/write files, run commands), please call the appropriate tools. If you are fully finished, please summarize your final output.",
                        "timestamp": time.time()
                    })
                    continue
                else:
                    # No tools called. Step is completed. Return content as result
                    return llm_response.get("content") or "Step completed without response text."

            # Process tool calls
            for tool_call in tool_calls:
                tool_id = str(tool_call.get("id") or "").strip()
                func_info = tool_call.get("function", {})
                full_name = func_info.get("name", "")
                args = func_info.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                if not isinstance(args, dict):
                    args = {}
                if not tool_id:
                    tool_id = f"anonymous-{iteration}-{len(state.variables.setdefault('completed_tool_calls', {}))}"

                completed_tool_calls = state.variables.setdefault("completed_tool_calls", {})
                if tool_id in completed_tool_calls:
                    result_str = completed_tool_calls[tool_id]
                    convo.messages.append({
                        "role": "tool", "tool_call_id": tool_id, "name": full_name,
                        "content": result_str, "timestamp": time.time(),
                    })
                    await self.event_bus.publish(Event(
                        type="ToolReused",
                        payload={"execution_id": execution_id, "tool_call_id": tool_id, "result": result_str},
                    ))
                    continue

                # Parse capability and action
                if "__" in full_name:
                    cap_name, act_name = full_name.split("__", 1)
                else:
                    cap_name = full_name
                    act_name = ""

                # Evaluate policy
                policy_desc = await self.policy_provider.check_action(
                    execution_id=execution_id,
                    capability=cap_name,
                    action=act_name,
                    arguments=args,
                    context=context
                )

                await self.event_bus.publish(Event(
                    type="ToolCalled",
                    payload={
                        "execution_id": execution_id,
                        "tool_call_id": tool_id,
                        "capability": cap_name,
                        "action": act_name,
                        "arguments": args
                    }
                ))

                if policy_desc.decision == "deny":
                    result_str = f"Execution blocked: Policy Denied. Reason: {policy_desc.reason}"
                    completed_tool_calls[tool_id] = result_str
                    convo.messages.append({
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "name": full_name,
                        "content": result_str,
                        "timestamp": time.time()
                    })
                    await self.event_bus.publish(Event(
                        type="ToolCompleted",
                        payload={"execution_id": execution_id, "tool_call_id": tool_id, "result": result_str}
                    ))
                elif policy_desc.decision == "approval":
                    # Human-in-the-loop: Pause execution and await approval
                    state.status = "paused"
                    state.variables["pending_approval"] = {
                        "tool_call_id": tool_id,
                        "capability": cap_name,
                        "action": act_name,
                        "arguments": args,
                    }
                    await self.event_bus.publish(Event(
                        type="ApprovalRequested",
                        payload={
                            "execution_id": execution_id,
                            "tool_call_id": tool_id,
                            "capability": cap_name,
                            "action": act_name,
                            "arguments": args,
                            "reason": policy_desc.reason
                        }
                    ))
                    await self.event_bus.publish(Event(
                        type="ExecutionPaused",
                        payload={"execution_id": execution_id}
                    ))
                    # Stop the loop here, we will resume when decision arrives
                    return "Paused waiting for human approval."
                else:
                    # 'allow' -> Execute the capability
                    result_str = await self._call_tool(execution_id, cap_name, act_name, args)
                    completed_tool_calls[tool_id] = result_str
                    convo.messages.append({
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "name": full_name,
                        "content": result_str,
                        "timestamp": time.time()
                    })
                    await self.event_bus.publish(Event(
                        type="ToolCompleted",
                        payload={"execution_id": execution_id, "tool_call_id": tool_id, "result": result_str}
                    ))

        return "Reached maximum step iterations."

    async def _call_tool(self, execution_id: str, capability: str, action: str, arguments: Dict[str, Any]) -> str:
        """Helper to invoke the registered capability class method."""
        cap_inst = self._capabilities.get(capability.lower())
        if not cap_inst:
            return f"Error: Capability '{capability}' not registered."

        method = cap_inst.actions.get(action)
        if not method:
            return f"Error: Capability '{capability}' has no action '{action}'."

        try:
            # Check signatures and pass self/context if required.
            # In python, calling instance method pass `self` automatically if retrieved from instance,
            # but since getmembers retrieves functions, method may be unbound or bound.
            # Let's retrieve bound method from the instance itself to be safe.
            bound_method = getattr(cap_inst, method.__name__)
            
            sig = inspect.signature(bound_method)
            kwargs = dict(arguments)
            if "context" in sig.parameters:
                # Compile context to pass along
                context = await self.context_engine.compile_context(
                    execution_id=execution_id,
                    conversation_id=execution_id,
                    agent_id="default_agent",
                    capabilities_schemas=[]
                )
                kwargs["context"] = context

            started = time.perf_counter()
            res = bound_method(**kwargs)
            if inspect.isawaitable(res):
                res = await res
            result = str(res)
            self.telemetry.record("tool_completed", execution_id, capability=capability, action=action, duration_ms=round((time.perf_counter() - started) * 1000, 2), result=result)
            return result
        except Exception as e:
            self.telemetry.record("tool_failed", execution_id, capability=capability, action=action, error=str(e))
            logger.error(f"Error executing action {capability}.{action}: {e}", exc_info=True)
            return f"Error executing tool: {e}"

    @staticmethod
    def _structured_delivery(response: str) -> Dict[str, Any]:
        """Normalize a sub-agent response into a stable parent-agent contract."""
        parsed = None
        try:
            parsed = json.loads(response)
        except (TypeError, ValueError):
            text = str(response or "")
            first, last = text.find("{"), text.rfind("}")
            if first >= 0 and last > first:
                try:
                    parsed = json.loads(text[first:last + 1])
                except ValueError:
                    parsed = None
        if isinstance(parsed, dict):
            return {
                "summary": str(parsed.get("summary", "")),
                "artifacts": parsed.get("artifacts", []) if isinstance(parsed.get("artifacts", []), list) else [],
                "evidence": parsed.get("evidence", []) if isinstance(parsed.get("evidence", []), list) else [],
                "risks": parsed.get("risks", []) if isinstance(parsed.get("risks", []), list) else [],
                "next_action": str(parsed.get("next_action", "")),
            }
        return {"summary": response, "artifacts": [], "evidence": [], "risks": [], "next_action": ""}

    async def resume_with_decision(self, execution_id: str, decision: str, reason: Optional[str] = None):
        """
        Resumes a paused execution with the user decision ('allow' or 'reject').
        """
        state = self.state_engine.get_execution(execution_id)
        if state.status != "paused":
            raise ValueError(f"Execution {execution_id} is not paused.")

        pending_app = state.variables.get("pending_approval")
        if not pending_app:
            raise ValueError(f"No pending approval found for execution {execution_id}.")

        pending_app["decision"] = decision
        pending_app["reason"] = reason or ""

        # Set status back to running and resume execution
        state.status = "running"
        await self.event_bus.publish(Event(
            type="ExecutionResumed",
            payload={"execution_id": execution_id, "decision": decision}
        ))

        # Re-start execution process (it will load the step again, find the pending approval, and process it)
        task = state.variables.get("task") or self.state_engine.get_conversation(execution_id).messages[0]["content"]
        if task.startswith("Task: "):
            task = task[6:]
            
        # Run execution loop asynchronously in the background so it doesn't block the caller
        asyncio.create_task(self.execute_task(execution_id, task))
