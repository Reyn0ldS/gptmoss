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
    runtime = tmp_path / "python-3.14.6-embed-amd64"
    runtime.mkdir()
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
        "@echo PYTHON=%GPTMOSS_PYTHON%\n",
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


def test_windows_launchers_share_runtime_detection():
    install = (PROJECT_ROOT / "install.bat").read_text(encoding="utf-8")
    start = (PROJECT_ROOT / "start.bat").read_text(encoding="utf-8")

    assert "scripts\\find_python.bat" in install
    assert "scripts\\find_python.bat" in start
    assert 'GPTMOSS_RUNTIME_KIND!"=="embedded"' in install
    assert "--no-index" in install
    assert '"!GPTMOSS_PYTHON!" "%~dp0main.py" %*' in start
