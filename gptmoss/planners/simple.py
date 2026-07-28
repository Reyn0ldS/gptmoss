import json
import logging
from typing import Dict, Any, List
from gptmoss.interfaces.planner import PlannerProvider
from gptmoss.interfaces.llm import LLMProvider

logger = logging.getLogger("gptmoss.planners.simple")

class SimplePlanner(PlannerProvider):
    """
    Simple Planner that uses the LLM to generate a plan of action.
    Falls back to a single-step execution plan if LLM call fails.
    """
    def __init__(self, llm_provider: LLMProvider):
        self.llm_provider = llm_provider

    @staticmethod
    def _fallback_plan(task: str) -> Dict[str, Any]:
        task_lower = task.lower()
        software_markers = (
            "project", "projet", "application", "software", "logiciel", "code",
            "api", "website", "site web", "script", "module", "programme",
        )
        if any(marker in task_lower for marker in software_markers):
            return {
                "steps": [
                    {"id": 0, "role": "architect", "description": "Architect/Analyst: analyze requirements and write technical specifications.", "dependencies": [], "status": "pending"},
                    {"id": 1, "role": "security", "description": "Security Reviewer: review the specifications and document risks and mitigations.", "dependencies": [0], "status": "pending"},
                    {"id": 2, "role": "developer", "description": "Developer/Coder: implement the complete project from the validated specifications.", "dependencies": [0, 1], "status": "pending"},
                    {"id": 3, "role": "qa", "description": "QA Testing Engineer: create and run tests for the implementation.", "dependencies": [2], "status": "pending"},
                    {"id": 4, "role": "debugger", "description": "Debugger & Bug Fixer: inspect test evidence, correct failures, and rerun relevant tests.", "dependencies": [3], "status": "pending"},
                    {"id": 5, "role": "writer", "description": "Technical Writer: write project documentation from the implementation.", "dependencies": [2], "status": "pending"},
                    {"id": 6, "role": "coordinator", "description": "Final Summary: synthesize all validated deliveries and report the final project result.", "dependencies": [4, 5], "status": "pending"},
                ],
                "rationale": "Deterministic multi-agent fallback used because the model did not return a valid plan.",
            }
        return {
            "steps": [{"id": 0, "role": "coordinator", "description": f"Perform the user task: {task}", "dependencies": [], "status": "pending"}],
            "rationale": "Deterministic single-agent fallback plan.",
        }

    async def plan(
        self,
        task: str,
        context: Dict[str, Any],
        capabilities_schemas: List[Dict[str, Any]],
        **kwargs
    ) -> Dict[str, Any]:
        """
        Asks LLM to plan the task, returning a JSON structured plan.
        """
        # Skip planning if this is a sub-agent task to avoid recursion
        parent_id = kwargs.get("parent_execution_id")
        if not parent_id and context and "variables" in context:
            parent_id = context["variables"].get("parent_execution_id")
            
        if parent_id:
            logger.info(f"Bypassing planning for sub-agent execution. Parent ID: {parent_id}")
            return {
                "steps": [
                    {"id": 0, "description": task, "dependencies": [], "status": "pending"}
                ],
                "rationale": "Direct execution of sub-agent task."
            }

        capabilities_list = [s["function"]["name"] for s in capabilities_schemas]
        
        prompt = (
            "You are the Planning Engine of the MOSS Agent Runtime.\n"
            "Break down the user task into a logical Directed Acyclic Graph (DAG) of steps.\n"
            "Each step must be actionable, described clearly, declare its dependencies, and have one explicit role.\n"
            "Allowed roles are: architect, security, developer, qa, debugger, writer, coordinator.\n\n"
            "IMPORTANT: For any software development, file creation, or complex project task, you MUST structure your plan "
            "systematically around a collaborative multi-agent workflow containing the following roles and phases:\n"
            "1. Architect/Analyst: Analyze needs and write technical specifications (specs.md).\n"
            "2. Security & Compliance Reviewer: Inspect specifications for logical flaws or risks (security_review.md). (Depends on Step 1)\n"
            "3. Developer/Coder: Write complete, functional source code files. (Depends on Step 1 & 2)\n"
            "4. QA Testing Engineer: Write unit tests (pytest) to check code viability. (Depends on Step 3)\n"
            "5. Debugger & Bug Fixer: Execute tests and perform code correction loops on failure. (Depends on Step 4)\n"
            "6. Technical Writer: Write documentation (README.md). (Depends on Step 3)\n"
            "7. Final Summary: Write a brief final summary explaining what was accomplished and how. (Depends on Step 5 & 6)\n\n"
            f"User Task: {task}\n"
            f"Available Capabilities: {capabilities_list}\n\n"
            "Response MUST be a JSON object with keys 'steps' and 'rationale'.\n"
            "For each step, specify 'dependencies' as an array of step IDs that MUST be completed before this step can run. "
            "Steps that can run immediately should have \"dependencies\": [].\n"
            "Format:\n"
            "{\n"
            '  "steps": [\n'
            '    {"id": 0, "role": "architect", "description": "Step 1 description", "dependencies": [], "status": "pending"},\n'
            '    {"id": 1, "role": "security", "description": "Step 2 description", "dependencies": [0], "status": "pending"}\n'
            '  ],\n'
            '  "rationale": "Explanation for this plan"\n'
            "}\n"
            "Only return raw valid JSON. Do not wrap in markdown fences or text outside the JSON."
        )

        try:
            messages = [
                {"role": "system", "content": "You are a precise JSON planning coordinator. Output only raw JSON."},
                {"role": "user", "content": prompt}
            ]
            response = await self.llm_provider.completion(messages=messages, temperature=0.1)
            content = response.get("content", "").strip()
            
            # Use robust JSON block extraction
            plan_data = None
            try:
                plan_data = json.loads(content)
            except Exception:
                pass
 
            if not plan_data:
                if "```json" in content:
                    try:
                        block = content.split("```json")[1].split("```")[0].strip()
                        plan_data = json.loads(block)
                    except Exception:
                        pass
                elif "```" in content:
                    try:
                        block = content.split("```")[1].split("```")[0].strip()
                        plan_data = json.loads(block)
                    except Exception:
                        pass
 
            if not plan_data:
                first_idx = content.find("{")
                last_idx = content.rfind("}")
                if first_idx != -1 and last_idx != -1 and last_idx > first_idx:
                    try:
                        block = content[first_idx:last_idx+1].strip()
                        plan_data = json.loads(block)
                    except Exception:
                        pass
                        
            if not plan_data:
                raise ValueError("Could not parse JSON planning response from content.")
            
            # Validate format
            if "steps" not in plan_data:
                raise ValueError("Missing 'steps' field in generated plan.")
            for step in plan_data["steps"]:
                if "dependencies" not in step:
                    step["dependencies"] = []
                step["status"] = "pending"
            return plan_data

        except Exception as e:
            logger.warning(f"Error during LLM planning, using fallback single-step plan: {e}")
            return self._fallback_plan(task)
