import os
import re
import unicodedata
from typing import Dict, Any, Optional
from gptmoss.interfaces.capability import capability, action
from gptmoss.core.durable_io import write_text_atomic

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
            
        if not isinstance(project_id, str) or not project_id or os.path.basename(project_id) != project_id or project_id in {".", ".."}:
            raise PermissionError("Invalid project identifier.")
        target_dir = os.path.join(self.workspace_root, "projects", project_id)
        os.makedirs(target_dir, exist_ok=True)
        return target_dir

    def _resolve_path(self, path: str, execution_id: Optional[str] = None) -> str:
        root_dir = os.path.realpath(self._get_workspace_for_execution(execution_id))
        full_path = os.path.realpath(os.path.join(root_dir, path))
        
        if self.restrict_to_workspace:
            try:
                is_within_root = os.path.commonpath([os.path.normcase(root_dir), os.path.normcase(full_path)]) == os.path.normcase(root_dir)
            except ValueError:
                is_within_root = False
            if not is_within_root:
                raise PermissionError("Access denied: path is outside the workspace root.")
                
        if not self.allow_subfolders:
            # Check that the file/folder's parent directory is exactly the resolved root
            parent_dir = os.path.dirname(full_path)
            if os.path.normcase(parent_dir) != os.path.normcase(root_dir):
                raise PermissionError("Access denied: subfolder operations are blocked by configuration.")
                
        return full_path

    @action(name="read", description="Read the content of a file. Path is relative to the workspace.")
    def read(
        self,
        path: str,
        context: Optional[Dict[str, Any]] = None,
        offset: int = 0,
        limit: int = 0,
    ) -> str:
        """Read text, optionally using bounded character offsets for large files."""
        execution_id = context.get("execution_id") if context else None
        resolved = self._resolve_path(path, execution_id)
        if os.path.isdir(resolved):
            return f"Error: '{path}' is a directory. Use list_dir to view its contents."
        # Open directly after the sandboxed resolution. A separate exists()
        # probe creates a TOCTOU window and has produced false negatives on
        # Windows/network-backed workspaces even when list_dir sees the file.
        try:
            with open(resolved, "r", encoding="utf-8") as f:
                content = f.read()
        except FileNotFoundError:
            return f"Error: File not found at {path}"
        start = max(0, int(offset or 0))
        if start >= len(content):
            return ""
        count = max(0, int(limit or 0))
        return content[start : start + count] if count else content[start:]

    @action(name="write", description="Create or overwrite a file with contents. Path is relative to the workspace.")
    def write(self, path: str, content: str, context: Optional[Dict[str, Any]] = None) -> str:
        """Writes content to a file."""
        execution_id = context.get("execution_id") if context else None
        resolved = self._resolve_path(path, execution_id)
        os.makedirs(os.path.dirname(resolved), exist_ok=True)
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(content)
        return f"File written successfully to {path}"

    @action(
        name="append",
        description=(
            "Append text to a file without replacing existing content. Use this to build "
            "large owned artifacts in bounded sections after the initial write."
        ),
    )
    def append(self, path: str, content: str, context: Optional[Dict[str, Any]] = None) -> str:
        """Append UTF-8 text to an owned workspace file."""
        execution_id = context.get("execution_id") if context else None
        resolved = self._resolve_path(path, execution_id)
        if os.path.isdir(resolved):
            return f"Error: '{path}' is a directory. Append requires a file path."
        os.makedirs(os.path.dirname(resolved), exist_ok=True)
        with open(resolved, "a", encoding="utf-8") as f:
            f.write(content)
        return f"Content appended successfully to {path}"

    @staticmethod
    def _paragraph_key(value: str) -> str:
        without_references = re.sub(
            r"\[[^\[\]\n]+?\s+>\s+[^\[\]\n]+?\]", "", str(value or "")
        )
        decomposed = unicodedata.normalize("NFKD", without_references)
        folded = "".join(
            character for character in decomposed
            if not unicodedata.combining(character)
        ).casefold()
        return " ".join(re.findall(r"[^\W_]+", folded, flags=re.UNICODE))

    @action(
        name="replace_paragraph",
        description=(
            "Replace one paragraph selected by a unique normalized prefix. Use occurrence=2 "
            "to remove the second copy of a duplicated paragraph. New content may be empty "
            "only when removing a duplicate; headings and surrounding paragraphs are preserved."
        ),
    )
    def replace_paragraph(
        self,
        path: str,
        paragraph_prefix: str,
        content: str,
        occurrence: int = 1,
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Atomically replace one blank-line-delimited Markdown paragraph."""
        execution_id = context.get("execution_id") if context else None
        resolved = self._resolve_path(path, execution_id)
        prefix = self._paragraph_key(paragraph_prefix)
        if len(prefix) < 24:
            return "Error: paragraph_prefix must contain at least 24 normalized characters."
        try:
            requested_occurrence = int(occurrence)
        except (TypeError, ValueError):
            return "Error: occurrence must be an integer greater than or equal to 1."
        if requested_occurrence < 1:
            return "Error: occurrence must be an integer greater than or equal to 1."
        if not os.path.isfile(resolved):
            return f"Error: File not found at {path}"
        with open(resolved, "r", encoding="utf-8") as handle:
            original = handle.read()

        # Keep every separator verbatim. A segment may start with a Markdown
        # heading; retain it while replacing only the paragraph body beneath it.
        segments = re.split(r"(\r?\n[ \t]*\r?\n+)", original)
        matches = []
        for index in range(0, len(segments), 2):
            segment = segments[index]
            lines = segment.splitlines()
            body_start = 0
            while body_start < len(lines) and re.match(
                r"^\s*#{1,6}\s+\S", lines[body_start]
            ):
                body_start += 1
            body = " ".join(line.strip() for line in lines[body_start:] if line.strip())
            if self._paragraph_key(body).startswith(prefix):
                matches.append((index, lines[:body_start]))
        if len(matches) < requested_occurrence:
            return (
                f"Error: paragraph prefix matched {len(matches)} occurrence(s), "
                f"cannot replace occurrence {requested_occurrence}. Read the current file "
                "and retry with an exact longer prefix."
            )

        segment_index, heading_lines = matches[requested_occurrence - 1]
        replacement_parts = [*heading_lines]
        if str(content or "").strip():
            replacement_parts.append(str(content).strip())
        segments[segment_index] = "\n".join(replacement_parts)
        updated = "".join(segments)
        if updated == original:
            return "Error: replacement would not change the file."
        write_text_atomic(resolved, updated)
        return (
            f"Paragraph occurrence {requested_occurrence} replaced successfully in {path}"
        )

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
