import pytest
import os
import tempfile
from types import SimpleNamespace
from gptmoss.capabilities.devteam import DeveloperTeamCapability

def test_check_syntax_errors_valid_code():
    # Setup temporary project directory with valid code
    with tempfile.TemporaryDirectory() as tmp_dir:
        valid_py = os.path.join(tmp_dir, "valid.py")
        with open(valid_py, "w") as f:
            f.write("def hello():\n    return 'world'\n")
            
        devteam = DeveloperTeamCapability(workspace_root=tmp_dir)
        errors = devteam._check_syntax_errors(tmp_dir)
        assert errors == ""

def test_check_syntax_errors_invalid_code():
    # Setup temporary project directory with syntax error
    with tempfile.TemporaryDirectory() as tmp_dir:
        invalid_py = os.path.join(tmp_dir, "invalid.py")
        with open(invalid_py, "w") as f:
            f.write("def hello()\n    return 'world'\n") # Missing colon
            
        devteam = DeveloperTeamCapability(workspace_root=tmp_dir)
        errors = devteam._check_syntax_errors(tmp_dir)
        assert "invalid.py" in errors
        assert "expected" in errors or "syntax" in errors or "Error" in errors or "colon" in errors

def test_approve_quality_gate():
    devteam = DeveloperTeamCapability()
    res = devteam.approve_quality_gate(project_name="my_proj", test_output="failures here")
    assert "my_proj" in res
    assert "approuvé" in res

def test_filesystem_dynamic_project_routing():
    import tempfile
    from gptmoss.capabilities.filesystem import FilesystemCapability
    from gptmoss.core.state import StateEngine, ExecutionState
    
    with tempfile.TemporaryDirectory() as tmp_dir:
        state_engine = StateEngine()
        # Mock execution state belonging to a specific project
        exec_id = "task-uuid-1234"
        exec_state = ExecutionState(execution_id=exec_id)
        exec_state.variables["project_id"] = "proj-calculator"
        state_engine.executions[exec_id] = exec_state
        
        fs = FilesystemCapability(workspace_root=tmp_dir, state_engine=state_engine)
        
        # Call write with context
        context = {"execution_id": exec_id}
        fs.write("hello.txt", "World data", context=context)
        assert fs.list_dir(".", context=context) == "hello.txt"
        assert fs.read("hello.txt", context=context) == "World data"
        
        # Verify that path is created under tmp_dir/projects/proj-calculator/hello.txt
        expected_path = os.path.join(tmp_dir, "projects", "proj-calculator", "hello.txt")
        assert os.path.exists(expected_path)
        with open(expected_path, "r", encoding="utf-8") as f:
            assert f.read() == "World data"

def test_custom_project_path_routing():
    import tempfile
    from gptmoss.core import StateEngine, ExecutionState
    from gptmoss.capabilities.filesystem import FilesystemCapability
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Create a second temp directory representing the custom project folder
        with tempfile.TemporaryDirectory() as custom_dir:
            state_engine = StateEngine()
            exec_id = "task-uuid-custom"
            exec_state = ExecutionState(execution_id=exec_id)
            exec_state.variables["project_id"] = "proj-custom"
            exec_state.variables["project_path"] = custom_dir
            state_engine.executions[exec_id] = exec_state
            
            fs = FilesystemCapability(workspace_root=tmp_dir, state_engine=state_engine)
            
            # Call write with context
            context = {"execution_id": exec_id}
            fs.write("hello_custom.txt", "Custom Project Data", context=context)
            
            # Verify that path is created directly inside custom_dir, NOT tmp_dir
            expected_path = os.path.join(custom_dir, "hello_custom.txt")
            assert os.path.exists(expected_path)
            with open(expected_path, "r", encoding="utf-8") as f:
                assert f.read() == "Custom Project Data"
                
            # Verify that it is NOT in tmp_dir
            assert not os.path.exists(os.path.join(tmp_dir, "projects", "proj-custom", "hello_custom.txt"))

def test_filesystem_rejects_path_traversal_and_prefix_escape():
    from gptmoss.capabilities.filesystem import FilesystemCapability

    with tempfile.TemporaryDirectory() as tmp_dir:
        workspace = os.path.join(tmp_dir, "workspace")
        fs = FilesystemCapability(workspace_root=workspace)
        with pytest.raises(PermissionError):
            fs.write("../outside.txt", "blocked")
        with pytest.raises(PermissionError):
            fs._resolve_path(os.path.join(tmp_dir, "workspace-other", "file.txt"))


@pytest.mark.asyncio
async def test_build_project_runs_roles_tests_repair_and_delivery_in_chronological_order(tmp_path):
    timeline = []

    class ScriptedTeam(DeveloperTeamCapability):
        async def _execute_role_task(self, role_name, system_prompt, task_description, parent_id=None):
            timeline.append(("role", role_name, parent_id))
            project = tmp_path / "demo"
            call_number = sum(1 for item in timeline if item[0] == "role")
            if call_number == 1:
                (project / "specs.md").write_text("# Requirements\n", encoding="utf-8")
            elif call_number == 2:
                (project / "security_review.md").write_text("# Review\n", encoding="utf-8")
            elif call_number == 3:
                (project / "app.py").write_text("def ready():\n    return True\n", encoding="utf-8")
            elif call_number == 4:
                (project / "specs_compliance.md").write_text("All requirements satisfied.\n", encoding="utf-8")
            elif call_number == 5:
                (project / "tests").mkdir(exist_ok=True)
                (project / "tests" / "test_app.py").write_text(
                    "from app import ready\n\ndef test_ready():\n    assert ready()\n", encoding="utf-8",
                )
            elif call_number == 7:
                (project / "README.md").write_text("# Demo\n", encoding="utf-8")
            return "approved" if "Qualit" in role_name else "completed"

    class ScriptedShell:
        def __init__(self):
            self.outputs = ["EXIT_CODE: 1\n1 failed", "EXIT_CODE: 0\n1 passed"]

        def execute(self, command):
            timeline.append(("shell", command, None))
            return self.outputs.pop(0)

    shell = ScriptedShell()
    kernel = SimpleNamespace(execution_engine=SimpleNamespace(
        get_capability=lambda name: shell if name == "shell" else None,
    ))
    team = ScriptedTeam(kernel=kernel, workspace_root=str(tmp_path))

    report = await team.build_project(
        "demo", "Build a verified local application", context={"execution_id": "parent-task"},
    )

    kinds = [item[0] for item in timeline]
    assert kinds == ["role", "role", "role", "role", "role", "shell", "role", "shell", "role"]
    roles = [item[1] for item in timeline if item[0] == "role"]
    assert roles[0] == "Architecte"
    assert roles[1].startswith("Analyste")
    assert roles[2].startswith("D") and roles[3].startswith("V")
    assert roles[4].startswith("Testeur") and "bug" in roles[5].lower()
    assert roles[6].startswith("R")
    assert all(item[2] == "parent-task" for item in timeline if item[0] == "role")
    assert "pytest" in timeline[5][1] and timeline[5][1] == timeline[7][1]
    assert (tmp_path / "demo" / "README.md").is_file()
    assert "demo" in report and "1 passed" in report
