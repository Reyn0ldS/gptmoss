import os
from typing import Dict, Any, Optional
from gptmoss.interfaces.capability import capability, action

@capability(name="filesystem", description="Read and write files on the local filesystem.")
class FilesystemCapability:
    """
    Capability to manage local files and folders.
    """
    def __init__(self, workspace_root: str = ".", state_engine = None):
        self.workspace_root = os.path.abspath(workspace_root)
        self.state_engine = state_engine
        self.restrict_to_workspace = True
        self.allow_subfolders = True

    def update_workspace_config(self, workspace_root: str, restrict_to_workspace: bool, allow_subfolders: bool):
        self.workspace_root = os.path.abspath(workspace_root)
        self.restrict_to_workspace = restrict_to_workspace
        self.allow_subfolders = allow_subfolders
        os.makedirs(self.workspace_root, exist_ok=True)

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
            
        target_dir = os.path.join(self.workspace_root, "projects", project_id)
        os.makedirs(target_dir, exist_ok=True)
        return target_dir

    def _resolve_path(self, path: str, execution_id: Optional[str] = None) -> str:
        root_dir = self._get_workspace_for_execution(execution_id)
        full_path = os.path.abspath(os.path.join(root_dir, path))
        
        if self.restrict_to_workspace:
            if not full_path.startswith(root_dir):
                raise PermissionError("Access denied: path is outside the workspace root.")
                
        if not self.allow_subfolders:
            # Check that the file/folder's parent directory is exactly the resolved root
            parent_dir = os.path.dirname(full_path)
            if parent_dir != root_dir:
                raise PermissionError("Access denied: subfolder operations are blocked by configuration.")
                
        return full_path

    @action(name="read", description="Read the content of a file. Path is relative to the workspace.")
    def read(self, path: str, context: Optional[Dict[str, Any]] = None) -> str:
        """Reads contents of a file."""
        execution_id = context.get("execution_id") if context else None
        resolved = self._resolve_path(path, execution_id)
        if not os.path.exists(resolved):
            return f"Error: File not found at {path}"
        if os.path.isdir(resolved):
            return f"Error: '{path}' is a directory. Use list_dir to view its contents."
        with open(resolved, "r", encoding="utf-8") as f:
            return f.read()

    @action(name="write", description="Create or overwrite a file with contents. Path is relative to the workspace.")
    def write(self, path: str, content: str, context: Optional[Dict[str, Any]] = None) -> str:
        """Writes content to a file."""
        execution_id = context.get("execution_id") if context else None
        resolved = self._resolve_path(path, execution_id)
        os.makedirs(os.path.dirname(resolved), exist_ok=True)
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(content)
        return f"File written successfully to {path}"

    @action(name="list_dir", description="List files and directories in a path relative to the workspace.")
    def list_dir(self, path: str = ".", context: Optional[Dict[str, Any]] = None) -> str:
        """Lists directory files."""
        execution_id = context.get("execution_id") if context else None
        resolved = self._resolve_path(path, execution_id)
        if not os.path.exists(resolved):
            return f"Error: Path {path} does not exist."
        if not os.path.isdir(resolved):
            return f"Error: Path {path} is not a directory."
        
        items = os.listdir(resolved)
        return "\n".join(items) if items else "(empty directory)"

    @action(name="delete", description="Delete a file relative to the workspace.")
    def delete(self, path: str, context: Optional[Dict[str, Any]] = None) -> str:
        """Deletes a file."""
        execution_id = context.get("execution_id") if context else None
        resolved = self._resolve_path(path, execution_id)
        if not os.path.exists(resolved):
            return f"Error: File not found at {path}"
        if os.path.isdir(resolved):
            os.rmdir(resolved)
            return f"Directory {path} deleted."
        else:
            os.remove(resolved)
            return f"File {path} deleted."
