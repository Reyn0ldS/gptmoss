import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="Windows script")


@WINDOWS_ONLY
def test_embedded_python_configuration_is_idempotent(tmp_path):
    runtime = tmp_path / "gptmoss-main (1)" / "python-3.14.6-embed-amd64"
    runtime.mkdir(parents=True)
    (runtime / "python.exe").touch()
    path_file = runtime / "python314._pth"
    path_file.write_text("python314.zip\n.\n#import site\n", encoding="ascii")

    script = PROJECT_ROOT / "scripts" / "configure_embedded_python.py"
    command = [
        sys.executable,
        str(script),
        "--python-directory",
        str(runtime),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)
    subprocess.run(command, check=True, capture_output=True, text=True)

    lines = path_file.read_text(encoding="ascii").splitlines()
    assert lines.count("Lib") == 1
    assert lines.count(r"Lib\site-packages") == 1
    assert lines.count("import site") == 1
    assert (runtime / "Lib" / "site-packages").is_dir()


@WINDOWS_ONLY
def test_runtime_detector_finds_portable_python_in_path_with_spaces(tmp_path):
    project = tmp_path / "GPTMOSS portable"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / "scripts" / "find_python.bat", scripts)

    python = project / "python-3.14.6-embed-amd64" / "python.exe"
    python.parent.mkdir()
    python.touch()

    runner = project / "run-test.bat"
    runner.write_text(
        f'@call "{scripts / "find_python.bat"}"\n'
        "@echo KIND=%GPTMOSS_RUNTIME_KIND%\n"
        "@echo PYTHON=%GPTMOSS_PYTHON%\n"
        "@echo DIRECTORY=%GPTMOSS_PYTHON_DIRECTORY%\n",
        encoding="ascii",
    )
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", runner.name],
        cwd=project,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "KIND=embedded" in result.stdout
    assert f"PYTHON={python}" in result.stdout
    assert f"DIRECTORY={python.parent}" in result.stdout


def test_windows_launchers_share_runtime_detection():
    install = (PROJECT_ROOT / "install.bat").read_text(encoding="utf-8")
    start = (PROJECT_ROOT / "start.bat").read_text(encoding="utf-8")
    offline_builder = (PROJECT_ROOT / "prepare-offline-source.bat").read_text(encoding="utf-8")

    assert "scripts\\find_python.bat" in install
    assert "scripts\\find_python.bat" in start
    assert 'GPTMOSS_RUNTIME_KIND!"=="embedded"' in install
    assert "--no-index" in install
    assert "PYTHONDONTWRITEBYTECODE=1" in install
    assert "PYTHONDONTWRITEBYTECODE=1" in start
    assert "scripts\\server_supervisor.py" in start
    assert '--python "!GPTMOSS_PYTHON!"' in start
    assert '--main "%~dp0main.py"' in start
    assert 'GPTMOSS_CONTROL_PORT=8765' in start
    assert "prepare_offline_source_launcher.py" in offline_builder
    assert "offline-preparation.log" in offline_builder
    assert "GPTMOSS_NO_PAUSE" in offline_builder
    for launcher in (install, start, offline_builder):
        assert 'pushd "%~dp0"' in launcher
        assert "popd" in launcher


def test_offline_preparation_helper_validates_real_python_and_existing_runtime():
    helper = (PROJECT_ROOT / "scripts" / "prepare_offline_source_launcher.py").read_text(
        encoding="utf-8"
    )

    assert "import pip, platform, sys" in helper
    assert "microsoft\\\\windowsapps" in helper
    assert "verify_existing_runtime" in helper
    assert "--verify-only" in helper
    assert "TeeStream" in helper


@WINDOWS_ONLY
def test_offline_builder_double_click_wrapper_can_verify_bundled_runtime():
    environment = os.environ.copy()
    environment["GPTMOSS_NO_PAUSE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "prepare-offline-source.bat", "--verify-only"],
        cwd=PROJECT_ROOT,
        check=False,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Offline runtime preparation completed" in result.stdout
    assert "already present and operational" in result.stdout


def test_main_anchors_default_runtime_files_to_project_root():
    main_source = (PROJECT_ROOT / "main.py").read_text(encoding="utf-8")

    assert 'os.path.join(PROJECT_ROOT, "app.log")' in main_source
    assert 'load_dotenv(os.path.join(PROJECT_ROOT, ".env"))' in main_source
    assert 'default=os.path.join(PROJECT_ROOT, "workspace")' in main_source
