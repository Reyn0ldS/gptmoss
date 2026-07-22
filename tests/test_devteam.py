import pytest
import os
import tempfile
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
