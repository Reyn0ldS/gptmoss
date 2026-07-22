from typing import Dict, Any, List
from gptmoss.core.state import StateEngine
from gptmoss.interfaces.memory import MemoryProvider

class ContextEngine:
    """
    Context Engine compiles information from multiple independent sources
    to build optimized prompts and state descriptions for the planner/executor.
    """
    def __init__(self, state_engine: StateEngine, memory_provider: MemoryProvider):
        self.state_engine = state_engine
        self.memory_provider = memory_provider

    async def compile_context(
        self,
        execution_id: str,
        conversation_id: str,
        agent_id: str,
        capabilities_schemas: List[Dict[str, Any]],
        extra_query: str = ""
    ) -> Dict[str, Any]:
        """
        Gathers raw context data from all independent sources and constructs the compiled context.
        """
        exec_state = self.state_engine.get_execution(execution_id)
        convo_state = self.state_engine.get_conversation(conversation_id)
        agent_state = self.state_engine.get_agent(agent_id)
        
        # Search memory if there is a query, or summarize
        memory_summary = ""
        if extra_query:
            memories = await self.memory_provider.search(extra_query, limit=3)
            memory_summary = "\n".join([str(m) for m in memories])
        else:
            memory_summary = await self.memory_provider.summarize()

        import sys
        import os
        os_name = "Windows" if sys.platform == "win32" else "Linux/macOS"
        path_sep = os.sep

        context = {
            "execution_id": execution_id,
            "conversation_history": convo_state.messages,
            "current_plan": exec_state.current_plan,
            "current_step": exec_state.current_step,
            "variables": exec_state.variables,
            "working_memory": memory_summary,
            "capabilities": capabilities_schemas,
            "system_instructions": agent_state.config.get("system_prompt", "You are a helpful MOSS runtime agent."),
            "environment": {
                "operating_system": os_name,
                "path_separator": path_sep,
                "shell": "cmd.exe" if sys.platform == "win32" else "bash/sh"
            }
        }
        
        return context
