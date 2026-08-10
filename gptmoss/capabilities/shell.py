import subprocess
import os
import re
import shlex
import sys
import threading
import time
from typing import Dict, Any, Optional
from gptmoss.interfaces.capability import capability, action

@capability(name="shell", description="Run terminal commands on the local machine.")
class ShellCapability:
    """
    Capability to execute local CLI shell commands.
    """
    def __init__(self, workspace_root: str = ".", state_engine = None, safe_mode: bool = True, timeout_seconds: int = 60, max_output_chars: int = 12_000):
        self.workspace_root = os.path.abspath(workspace_root)
        self.state_engine = state_engine
        self.safe_mode = safe_mode
        self.timeout_seconds = max(0, int(timeout_seconds))
        self.max_output_chars = max(0, int(max_output_chars))
        self._active_processes = {}
        self._active_processes_lock = threading.Lock()

    def update_workspace_config(self, workspace_root: str):
        self.workspace_root = os.path.abspath(workspace_root)

    def update_safety_config(self, safe_mode: bool = True, timeout_seconds: int = 60, max_output_chars: int = 12_000):
        self.safe_mode = safe_mode
        self.timeout_seconds = max(0, int(timeout_seconds))
        self.max_output_chars = max(0, int(max_output_chars))

    def _effective_timeout(self, command: str) -> Optional[int]:
        """Zero selects an adaptive timeout."""
        if self.timeout_seconds:
            return self.timeout_seconds
        normalized = command.lower()
        if any(marker in normalized for marker in ("pytest", "unittest", " test", " build", "compile")):
            return 900
        if any(marker in normalized for marker in ("download", "install", "pip ", "npm ", "cargo ")):
            return 1_800
        return 120

    @staticmethod
    def _has_shell_operators(command: str) -> bool:
        """Detect operators outside quotes so Python argv rewriting stays safe."""
        quote = None
        escaped = False
        for character in command:
            if escaped:
                escaped = False
                continue
            if character == "\\" and quote:
                escaped = True
                continue
            if character in {'"', "'"}:
                if quote == character:
                    quote = None
                elif quote is None:
                    quote = character
                continue
            if quote is None and character in "|&><":
                return True
        return False

    @staticmethod
    def _split_first_shell_operator(command: str):
        """Split a leading command from its first unquoted shell operator."""
        quote = None
        escaped = False
        text = str(command or "")
        for index, character in enumerate(text):
            if escaped:
                escaped = False
                continue
            if character == "\\" and quote:
                escaped = True
                continue
            if character in {'"', "'"}:
                if quote == character:
                    quote = None
                elif quote is None:
                    quote = character
                continue
            if quote is None and character in "|&><":
                operator_index = index
                if character in "><":
                    cursor = index - 1
                    while cursor >= 0 and text[cursor].isdigit():
                        cursor -= 1
                    if cursor < index - 1 and (cursor < 0 or text[cursor].isspace()):
                        operator_index = cursor + 1
                prefix = text[:operator_index].strip()
                suffix = text[operator_index:].strip()
                return prefix, suffix
        return None

    def _blocked_command_reason(self, command: str) -> Optional[str]:
        if not self.safe_mode:
            return None
        normalized = command.lower().replace("\\", "/")
        destructive_patterns = (
            "rm -rf /", "del /s", "format ", "diskpart", "shutdown ",
            "reboot", "reg delete", "remove-item -recurse", "clear-disk",
        )
        if any(pattern in normalized for pattern in destructive_patterns):
            return "Command blocked by shell safe mode because it is destructive."
        return None

    @staticmethod
    def _without_interactive_pager(command: str) -> str:
        """Remove terminal pagers that cannot be operated by an autonomous worker."""
        return re.sub(
            r"\s*\|\s*(?:more|less(?:\s+[^|]+)?)\s*$",
            "",
            str(command or ""),
            flags=re.IGNORECASE,
        ).strip()

    def _register_process(self, execution_id: Optional[str], process) -> None:
        if not execution_id:
            return
        with self._active_processes_lock:
            self._active_processes.setdefault(execution_id, set()).add(process)

    def _unregister_process(self, execution_id: Optional[str], process) -> None:
        if not execution_id:
            return
        with self._active_processes_lock:
            processes = self._active_processes.get(execution_id)
            if processes is not None:
                processes.discard(process)
                if not processes:
                    self._active_processes.pop(execution_id, None)

    @staticmethod
    def _terminate_process_tree(process) -> None:
        if process.poll() is not None:
            return
        try:
            if sys.platform == "win32":
                killer = subprocess.Popen(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                killer.communicate(timeout=5)
            else:
                os.killpg(os.getpgid(process.pid), 15)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def cancel_execution(self, execution_id: str) -> None:
        """Terminate every command still running for an execution."""
        with self._active_processes_lock:
            processes = list(self._active_processes.get(execution_id, set()))
        for process in processes:
            self._terminate_process_tree(process)

    def _execution_cancelled(self, execution_id: Optional[str]) -> bool:
        if not execution_id or not self.state_engine:
            return False
        state = self.state_engine.executions.get(execution_id)
        return bool(state and state.status == "cancelled")

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

    @staticmethod
    def _unquote_argument(value: str) -> str:
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            return value[1:-1]
        return value

    def _portable_python_command(self, command: str):
        """Build argv that restores normal project imports for embeddable Python."""
        if self._has_shell_operators(command):
            return None
        match = re.match(r"^python(?:\.exe)?(?:\s+(.*))?$", command, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            return None
        raw_arguments = match.group(1) or ""
        tokens = [self._unquote_argument(item) for item in shlex.split(raw_arguments, posix=False)]
        interpreter_options = []
        while tokens and tokens[0].startswith("-") and tokens[0] not in {"-m", "-c"}:
            interpreter_options.append(tokens.pop(0))

        bootstrap = "import os,runpy,sys;sys.path.insert(0,os.getcwd());"
        if len(tokens) >= 2 and tokens[0] == "-m":
            module = tokens[1]
            module_arguments = tokens[2:]
            code = bootstrap + f"sys.argv=[{module!r}]+sys.argv[1:];runpy.run_module({module!r},run_name='__main__')"
            return [sys.executable, *interpreter_options, "-c", code, *module_arguments]
        if len(tokens) >= 2 and tokens[0] == "-c":
            return [sys.executable, *interpreter_options, "-c", bootstrap + tokens[1], *tokens[2:]]
        if tokens and not tokens[0].startswith("-"):
            script = tokens[0]
            code = bootstrap + f"sys.argv=[{script!r}]+sys.argv[1:];runpy.run_path({script!r},run_name='__main__')"
            return [sys.executable, *interpreter_options, "-c", code, *tokens[1:]]
        return [sys.executable, *interpreter_options, *tokens]

    def _portable_python_shell_command(self, command: str) -> Optional[str]:
        """Preserve portable import bootstrapping before redirections or pipes."""
        split_command = self._split_first_shell_operator(command)
        if not split_command:
            return None
        prefix, suffix = split_command
        portable_prefix = self._portable_python_command(prefix)
        if portable_prefix is None:
            return None
        rendered_prefix = (
            subprocess.list2cmdline(portable_prefix)
            if sys.platform == "win32"
            else shlex.join(portable_prefix)
        )
        return rendered_prefix + " " + suffix

    @staticmethod
    def _leading_workspace_cd(command: str):
        """Return a leading directory change and remainder, if present."""
        match = re.match(
            r'^(?:cd(?:\s+/d)?|Set-Location)\s+(?:"([^"]+)"|([^&]+?))\s*(?:&&|&)\s*(.+)$',
            str(command or "").strip(),
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return None
        return (match.group(1) or match.group(2) or "").strip(), match.group(3).strip()

    @classmethod
    def _workspace_escape_reason(cls, command: str, cwd_dir: str) -> Optional[str]:
        """Reject a leading cd that escapes the execution's assigned workspace."""
        parsed = cls._leading_workspace_cd(command)
        if not parsed:
            return None
        requested, _ = parsed
        requested_path = os.path.abspath(os.path.join(cwd_dir, requested))
        assigned_path = os.path.abspath(cwd_dir)
        try:
            inside_workspace = (
                os.path.commonpath([requested_path, assigned_path]) == assigned_path
            )
        except ValueError:
            inside_workspace = False
        if not inside_workspace:
            return (
                "Command blocked because its leading directory change escapes the "
                f"assigned project workspace: {assigned_path}"
            )
        return None

    @classmethod
    def _shell_mutation_targets(cls, command: str):
        """Extract targets of common cross-platform file-mutating shell commands."""
        segments = []
        current = []
        quote = None
        for character in str(command or ""):
            if character in {'"', "'"}:
                if quote == character:
                    quote = None
                elif quote is None:
                    quote = character
                current.append(character)
                continue
            if quote is None and character in {"&", "|"}:
                if current:
                    segments.append("".join(current).strip())
                    current = []
                continue
            current.append(character)
        if current:
            segments.append("".join(current).strip())

        targets = []
        for segment in segments:
            if not segment or re.match(r"^\d*>", segment):
                continue
            try:
                tokens = [
                    cls._unquote_argument(token)
                    for token in shlex.split(segment, posix=False)
                ]
            except ValueError:
                continue
            if not tokens:
                continue
            command_name = os.path.basename(tokens[0]).lower()
            command_name = os.path.splitext(command_name)[0]
            arguments = [
                token for token in tokens[1:]
                if token and not re.match(r"^\d*[<>]", token)
            ]
            if sys.platform == "win32":
                arguments = [
                    token for token in arguments
                    if not re.fullmatch(r"/[A-Za-z]+", token)
                ]
            else:
                arguments = [token for token in arguments if not token.startswith("-")]
            if command_name in {"copy", "xcopy", "move", "cp", "mv"} and len(arguments) >= 2:
                targets.append(arguments[-1])
            elif command_name == "robocopy" and len(arguments) >= 2:
                targets.append(arguments[1])
            elif command_name in {
                "del", "erase", "rm", "rmdir", "rd", "mkdir", "md",
                "remove-item", "new-item",
            }:
                targets.extend(arguments)
            elif command_name in {"ren", "rename"} and arguments:
                targets.append(arguments[0])

        targets.extend(
            match.group(1) or match.group(2)
            for match in re.finditer(
                r"(?:^|\s)\d*>>?\s*(?:\"([^\"]+)\"|([^\s&|]+))",
                str(command or ""),
            )
        )
        return targets

    @classmethod
    def _external_mutation_reason(cls, command: str, cwd_dir: str) -> Optional[str]:
        """Reject common shell mutations whose target escapes the assigned project."""
        assigned_path = os.path.abspath(cwd_dir)
        ignored_targets = {"nul", "/dev/null"}
        for raw_target in cls._shell_mutation_targets(command):
            target = str(raw_target or "").strip()
            if not target or target.lower() in ignored_targets:
                continue
            expanded = os.path.expandvars(os.path.expanduser(target))
            if re.search(r"%[^%]+%|\$env:|\$\{", expanded, flags=re.IGNORECASE):
                return (
                    "Command blocked because a mutation target uses an unresolved "
                    "environment path outside the verifiable project boundary."
                )
            target_path = os.path.abspath(os.path.join(cwd_dir, expanded))
            try:
                inside_workspace = (
                    os.path.normcase(os.path.commonpath([target_path, assigned_path]))
                    == os.path.normcase(assigned_path)
                )
            except ValueError:
                inside_workspace = False
            if not inside_workspace:
                return (
                    "Command blocked because a file mutation targets outside the "
                    f"assigned project workspace: {target_path}"
                )
        return None

    @staticmethod
    def _strip_redundant_workspace_cd(command: str, cwd_dir: str) -> str:
        """Drop a leading cd to the workspace already selected by the runtime."""
        cleaned = str(command or "").strip()
        while True:
            parsed = ShellCapability._leading_workspace_cd(cleaned)
            if not parsed:
                return cleaned
            requested, remainder = parsed
            requested = os.path.abspath(os.path.join(cwd_dir, requested))
            if os.path.normcase(requested) != os.path.normcase(os.path.abspath(cwd_dir)):
                return cleaned
            cleaned = remainder

    @action(name="execute", description="Execute a command in the local shell. Returns stdout and stderr.")
    def execute(self, command: str, context: Optional[Dict[str, Any]] = None) -> str:
        """Runs command in subprocess and returns output."""
        execution_id = context.get("execution_id") if context else None
        cwd_dir = self._get_workspace_for_execution(execution_id)
        
        try:
            # Resolve generic python command to current active python binary
            cleaned_cmd = self._without_interactive_pager(command.strip())
            cleaned_cmd = self._strip_redundant_workspace_cd(cleaned_cmd, cwd_dir)
            blocked_reason = self._blocked_command_reason(cleaned_cmd)
            if blocked_reason:
                return f"Error: {blocked_reason}"
            escape_reason = self._workspace_escape_reason(cleaned_cmd, cwd_dir)
            if escape_reason:
                return f"Error: {escape_reason}"
            mutation_reason = self._external_mutation_reason(cleaned_cmd, cwd_dir)
            if mutation_reason:
                return f"Error: {mutation_reason}"
            if sys.platform == "win32":
                # Translate unix-style mkdir -p to windows mkdir and fix separators
                if "mkdir -p " in cleaned_cmd:
                    cleaned_cmd = cleaned_cmd.replace("mkdir -p ", "mkdir ")
                if "mkdir " in cleaned_cmd:
                    cleaned_cmd = cleaned_cmd.replace("/", "\\")

            portable_python_command = self._portable_python_command(cleaned_cmd)
            portable_python_shell_command = (
                self._portable_python_shell_command(cleaned_cmd)
                if portable_python_command is None
                else None
            )
            if (
                portable_python_command is None
                and portable_python_shell_command is None
                and self._has_shell_operators(cleaned_cmd)
            ):
                cleaned_cmd = re.sub(
                    r"^python(?:\.exe)?\b",
                    lambda _: f'"{sys.executable}"',
                    cleaned_cmd,
                    count=1,
                    flags=re.IGNORECASE,
                )
            if portable_python_command is not None:
                command_to_run = portable_python_command
                use_shell = False
            elif sys.platform == "win32":
                # Passing a quoted pipeline as the final item of a cmd.exe argv
                # list triggers cmd's special /C quote stripping. Let
                # subprocess construct the command line instead.
                command_to_run = (
                    "chcp 65001>NUL & "
                    + (portable_python_shell_command or cleaned_cmd)
                )
                use_shell = True
            else:
                command_to_run = portable_python_shell_command or cleaned_cmd
                use_shell = True

            command_environment = os.environ.copy()
            command_environment.setdefault("PYTHONUTF8", "1")
            command_environment.setdefault("PYTHONIOENCODING", "utf-8")
            command_environment.setdefault("PYTHONUNBUFFERED", "1")
            command_environment.setdefault("PAGER", "cat")
            command_environment.setdefault("GIT_PAGER", "cat")

            popen_options = {}
            if sys.platform == "win32":
                popen_options["creationflags"] = getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                )
            else:
                popen_options["start_new_session"] = True
            process = subprocess.Popen(
                command_to_run,
                shell=use_shell,
                cwd=cwd_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=command_environment,
                **popen_options,
            )
            self._register_process(execution_id, process)
            timeout = self._effective_timeout(cleaned_cmd)
            deadline = time.monotonic() + timeout if timeout else None
            try:
                while True:
                    if self._execution_cancelled(execution_id):
                        self._terminate_process_tree(process)
                        process.communicate()
                        return "Error: Command execution cancelled."
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        self._terminate_process_tree(process)
                        process.communicate()
                        return f"Error: Command execution timed out ({timeout}s)."
                    try:
                        stdout, stderr = process.communicate(
                            timeout=min(0.25, remaining) if remaining is not None else 0.25
                        )
                        break
                    except subprocess.TimeoutExpired:
                        continue
            finally:
                self._unregister_process(execution_id, process)

            output = f"EXIT_CODE: {process.returncode}\n"
            if stdout:
                output += f"STDOUT:\n{stdout}\n"
            if stderr:
                output += f"STDERR:\n{stderr}\n"
            if not stdout and not stderr:
                output += "Command produced no output."
                
            if self.max_output_chars and len(output) > self.max_output_chars:
                output = output[:self.max_output_chars] + "\n… [output truncated by shell safety limit]"
            return output
        except subprocess.TimeoutExpired:
            return f"Error: Command execution timed out ({self._effective_timeout(command)}s)."
        except Exception as e:
            return f"Error executing command: {e}"
