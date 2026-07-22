import subprocess
import os
import sys
from typing import Dict, Any, Optional
from gptmoss.interfaces.capability import capability, action

@capability(name="shell", description="Run terminal commands on the local machine.")
class ShellCapability:
    """
    Capability to execute local CLI shell commands.
    """
    def __init__(self, workspace_root: str = ".", state_engine = None):
        self.workspace_root = os.path.abspath(workspace_root)
        self.state_engine = state_engine

    def update_workspace_config(self, workspace_root: str):
        self.workspace_root = os.path.abspath(workspace_root)

    def _get_workspace_for_execution(self, execution_id: Optional[str]) -> str:
        if not self.state_engine or not execution_id:
            return self.workspace_root
            
        current_id = execution_id
        project_id = "proj-default"
        custom_path = None
        
        while True:
            state = self.state_engine.executions.get(current_id)
            if not state:
                break
            if "project_id" in state.variables:
                project_id = state.variables["project_id"]
            if "project_path" in state.variables:
                custom_path = state.variables["project_path"]
            
            parent_id = state.variables.get("parent_execution_id")
            if not parent_id or parent_id not in self.state_engine.executions:
                break
            current_id = parent_id
            
        if custom_path:
            return os.path.abspath(custom_path)
            
        if not isinstance(project_id, str) or not project_id or os.path.basename(project_id) != project_id or project_id in {".", ".."}:
            raise PermissionError("Invalid project identifier.")
        target_dir = os.path.join(self.workspace_root, "projects", project_id)
        os.makedirs(target_dir, exist_ok=True)
        return target_dir

    @action(name="execute", description="Execute a command in the local shell. Returns stdout and stderr.")
    def execute(self, command: str, context: Optional[Dict[str, Any]] = None) -> str:
        """Runs command in subprocess and returns output."""
        execution_id = context.get("execution_id") if context else None
        cwd_dir = self._get_workspace_for_execution(execution_id)
        
        try:
            # Resolve generic python command to current active python binary
            cleaned_cmd = command.strip()
            if sys.platform == "win32":
                # Translate unix-style mkdir -p to windows mkdir and fix separators
                if "mkdir -p " in cleaned_cmd:
                    cleaned_cmd = cleaned_cmd.replace("mkdir -p ", "mkdir ")
                if "mkdir " in cleaned_cmd:
                    cleaned_cmd = cleaned_cmd.replace("/", "\\")

            if cleaned_cmd.startswith("python ") or cleaned_cmd == "python":
                cleaned_cmd = f'"{sys.executable}"' + cleaned_cmd[6:]
            elif cleaned_cmd.startswith("python.exe ") or cleaned_cmd == "python.exe":
                cleaned_cmd = f'"{sys.executable}"' + cleaned_cmd[10:]

            result = subprocess.run(
                cleaned_cmd,
                shell=True,
                cwd=cwd_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=60
            )
            
            output = ""
            if result.stdout:
                output += f"STDOUT:\n{result.stdout}\n"
            if result.stderr:
                output += f"STDERR:\n{result.stderr}\n"
            if not output:
                output = f"Command finished with exit code {result.returncode} (no output)."
                
            return output
        except subprocess.TimeoutExpired:
            return "Error: Command execution timed out (60s)."
        except Exception as e:
            return f"Error executing command: {e}"
