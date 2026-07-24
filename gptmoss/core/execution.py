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

def parse_step_role(description: str) -> Optional[str]:
    desc_lower = description.lower()
    if "architect" in desc_lower or "architecte" in desc_lower:
        return "Architecte"
    elif "security" in desc_lower or "sécurité" in desc_lower or "reviewer" in desc_lower:
        return "Analyste Sécurité"
    elif "developer" in desc_lower or "coder" in desc_lower or "développeur" in desc_lower:
        return "Développeur"
    elif "test" in desc_lower or "qa" in desc_lower or "tester" in desc_lower:
        return "Testeur QA"
    elif "debug" in desc_lower or "bug" in desc_lower or "débug" in desc_lower:
        return "Débugueur"
    elif "writer" in desc_lower or "rédacteur" in desc_lower or "documentation" in desc_lower:
        return "Rédacteur Technique"
    return None


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
    ):
        self.event_bus = event_bus
        self.state_engine = state_engine
        self.context_engine = context_engine
        self.llm_provider = llm_provider
        self.planner = planner
        self.policy_provider = policy_provider
        self.telemetry = telemetry or TraceRecorder()
        self._capabilities: Dict[str, Any] = {}  # capability_name -> instance

    def register_capability(self, capability_name: str, instance: Any):
        """Register instantiated capability."""
        self._capabilities[capability_name.lower()] = instance
        # Ensure standard action methods are populated on instance
        instance.actions = get_actions(instance.__class__)
        logger.info(f"Registered capability: {capability_name}")

    def get_capability(self, capability_name: str) -> Optional[Any]:
        """Retrieve a registered capability by name."""
        return self._capabilities.get(capability_name.lower())

    def get_capabilities_schemas(self, is_sub_agent: bool = False) -> List[Dict[str, Any]]:
        """Generate JSON schemas for all registered capabilities."""
        schemas = []
        for name, inst in self._capabilities.items():
            if is_sub_agent and name.lower() in ("agent", "devteam"):
                continue
            for act_name, method in inst.actions.items():
                schemas.append(generate_action_schema(name, act_name, method))
        return schemas

    async def execute_task(self, execution_id: str, task: str):
        """
        Main execution loop for a task.
        """
        state = self.state_engine.get_execution(execution_id)
        convo = self.state_engine.get_conversation(execution_id)
        self.telemetry.record("execution_started", execution_id, task=task)

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
            schemas = self.get_capabilities_schemas(is_sub_agent=is_sub_agent)
            context = await self.context_engine.compile_context(
                execution_id=execution_id,
                conversation_id=execution_id,
                agent_id="default_agent",
                capabilities_schemas=schemas,
                extra_query=task
            )
            await self.event_bus.publish(Event(
                type="ContextBuilt",
                payload={"execution_id": execution_id, "context_summary": "Initial context compiled."}
            ))

            planning_started = time.perf_counter()
            plan_result = await self.planner.plan(task, context, schemas)
            self.telemetry.record("plan_generated", execution_id, duration_ms=round((time.perf_counter() - planning_started) * 1000, 2), steps=len(plan_result.get("steps", [])))
            state.current_plan = plan_result
            state.current_step = 0
            await self.event_bus.publish(Event(
                type="PlanGenerated",
                payload={"execution_id": execution_id, "plan": plan_result}
            ))

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
                role_name = parse_step_role(step.get("description", ""))
                is_sub_agent = state.variables.get("parent_execution_id") is not None
                
                if role_name and not is_sub_agent:
                    # Spawn sub-agent
                    import uuid
                    sub_id = str(uuid.uuid4())
                    
                    sub_exec = self.state_engine.get_execution(sub_id)
                    sub_exec.status = "pending"
                    sub_exec.variables["role_name"] = role_name
                    sub_exec.variables["parent_execution_id"] = execution_id
                    sub_exec.variables["project_id"] = state.variables.get("project_id", "proj-default")
                    sub_exec.variables["parent_task"] = state.variables.get("parent_task") or task
                    
                    await self.event_bus.publish(Event(
                        type="TaskCreated",
                        payload={
                            "execution_id": sub_id,
                            "task": step["description"],
                            "agent_id": "default_agent"
                        }
                    ))
                    
                    # Run sub-agent task loop in background
                    asyncio.create_task(self.execute_task(sub_id, step["description"]))
                    
                    # Wait for sub-agent completion
                    while True:
                        await asyncio.sleep(1.0)
                        
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
                            asyncio.create_task(self.execute_task(sub_id, step["description"]))
                        
                        if sub_state.status in ("completed", "failed", "cancelled"):
                            break
                            
                    if sub_state.status == "completed":
                        convo = self.state_engine.get_conversation(sub_id)
                        last_response = "Sub-agent execution completed."
                        for msg in reversed(convo.messages):
                            if msg.get("role") == "assistant" and msg.get("content"):
                                last_response = msg["content"]
                                break
                        delivery = self._structured_delivery(last_response)
                        sub_exec.variables["delivery"] = delivery
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
                    state.results["telemetry"] = self.telemetry.metrics(execution_id)
                    self.telemetry.record("execution_completed", execution_id, completed_steps=len(steps))
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
        
        step_desc = step.get("description", "")
        # Sub-prompt for the step: only append if not resuming from a pending approval to preserve tool call message ordering
        if not state.variables.get("pending_approval"):
            convo.messages.append({"role": "system", "content": f"Current Step objectives: {step_desc}. Generate thought and select tools if needed.", "timestamp": time.time()})

        max_iterations = 30
        try:
            import json
            import os
            filesystem_cap = self.get_capability("filesystem")
            if filesystem_cap:
                config_path = os.path.join(filesystem_cap.workspace_root, "config.json")
                if os.path.exists(config_path):
                    with open(config_path, "r", encoding="utf-8") as f:
                        config_data = json.load(f)
                    max_iterations = config_data.get("max_step_iterations", 30)
        except Exception:
            pass

        iteration = 0

        while iteration < max_iterations:
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
                if decision == "allow":
                    # Execute tool call
                    result_str = await self._call_tool(
                        execution_id,
                        pending_app["capability"],
                        pending_app["action"],
                        pending_app["arguments"]
                    )
                else:
                    result_str = f"Execution blocked: human rejection. Reason: {pending_app.get('reason', 'None')}"

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
            schemas = self.get_capabilities_schemas(is_sub_agent=is_sub_agent)

            # Compile context
            context = await self.context_engine.compile_context(
                execution_id=execution_id,
                conversation_id=execution_id,
                agent_id="default_agent",
                capabilities_schemas=schemas
            )

            # Request LLM completion
            llm_messages = []
            role_name = state.variables.get("role_name", "Coordinateur")
            base_prompt = context.get("system_instructions", "")
            
            role_lower = role_name.lower()
            if "architect" in role_lower:
                role_prompt = (
                    "You are the Specialized Architect Agent.\n"
                    "Your role is to analyze software requirements, design specifications, and write technical specifications files (e.g. specs.md).\n"
                    "Focus on clear system design, modular structures, and outlining detailed implementation plans for other sub-agents."
                )
            elif "security" in role_lower or "sécurité" in role_lower or "reviewer" in role_lower:
                role_prompt = (
                    "You are the Specialized Security & Compliance Reviewer.\n"
                    "Your role is to check specifications or code for logical flaws, cryptographic vulnerabilities, or input validation risks.\n"
                    "Write detailed security reviews (e.g. security_review.md) highlighting potential issues and proposing mitigations."
                )
            elif "développeur" in role_lower or "developer" in role_lower or "coder" in role_lower:
                role_prompt = (
                    "You are the Specialized Developer/Coder Agent.\n"
                    "Your role is to write clean, high-quality, and fully functional source code.\n"
                    "Avoid writing comments as placeholders; write actual implementation. Follow specs.md guidelines."
                )
            elif "test" in role_lower or "qa" in role_lower or "tester" in role_lower:
                role_prompt = (
                    "You are the Specialized QA Testing Engineer.\n"
                    "Your role is to design and write robust unit tests (e.g. pytest tests) to verify the code correctness.\n"
                    "Make sure you cover edge cases, input validation, and boundary conditions."
                )
            elif "debug" in role_lower or "débug" in role_lower or "fixer" in role_lower:
                role_prompt = (
                    "You are the Specialized Debugger & Bug Fixer.\n"
                    "Your role is to analyze test failure logs, run commands to inspect state, and modify files to fix code syntax or logical errors."
                )
            elif "writer" in role_lower or "rédacteur" in role_lower or "documentation" in role_lower:
                role_prompt = (
                    "You are the Specialized Technical Writer.\n"
                    "Your role is to write detailed project documentation, README.md files, and help guides for users."
                )
            else:
                role_prompt = base_prompt
                
            if role_name != "Coordinateur":
                role_prompt += ("\\nWhen finished, return a JSON object with keys: summary, artifacts, evidence, risks, next_action. "
                                "Use empty arrays or strings when a field does not apply.")
            llm_messages.append({"role": "system", "content": role_prompt})
            if context.get("context_summary"):
                llm_messages.append({"role": "system", "content": context["context_summary"]})
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
                tool_id = tool_call.get("id")
                func_info = tool_call.get("function", {})
                full_name = func_info.get("name", "")
                args = func_info.get("arguments", {})

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
        try:
            import json
            parsed = json.loads(response)
            if isinstance(parsed, dict):
                return {
                    "summary": str(parsed.get("summary", "")),
                    "artifacts": parsed.get("artifacts", []),
                    "evidence": parsed.get("evidence", []),
                    "risks": parsed.get("risks", []),
                    "next_action": str(parsed.get("next_action", "")),
                }
        except (TypeError, ValueError):
            pass
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
        task = convo = self.state_engine.get_conversation(execution_id).messages[0]["content"]
        # Remove "Task: " prefix if present
        if task.startswith("Task: "):
            task = task[6:]
            
        # Run execution loop asynchronously in the background so it doesn't block the caller
        asyncio.create_task(self.execute_task(execution_id, task))
