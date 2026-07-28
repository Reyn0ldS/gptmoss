import argparse
from pathlib import Path


def configure_runtime(python_directory: Path) -> Path:
    runtime_directory = python_directory.resolve(strict=True)
    python_executable = runtime_directory / "python.exe"
    if not python_executable.is_file():
        raise FileNotFoundError(f"python.exe was not found in '{runtime_directory}'.")

    path_files = list(runtime_directory.glob("python*._pth"))
    if len(path_files) != 1:
        raise RuntimeError(
            f"Expected exactly one python*._pth file in '{runtime_directory}'; "
            f"found {len(path_files)}."
        )

    path_file = path_files[0]
    lines = path_file.read_text(encoding="utf-8-sig").splitlines()
    normalized_lines: list[str] = []
    required_lines = {
        "lib": "Lib",
        "lib/site-packages": r"Lib\site-packages",
        "import site": "import site",
    }
    seen: set[str] = set()

    for line in lines:
        key = line.strip().replace(chr(92), "/").lower()
        if key == "#import site":
            key = "import site"
        if key in required_lines:
            if key not in seen:
                normalized_lines.append(required_lines[key])
                seen.add(key)
            continue
        normalized_lines.append(line)

    for key, value in required_lines.items():
        if key not in seen:
            normalized_lines.append(value)

    (runtime_directory / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)
    path_file.write_text("\n".join(normalized_lines) + "\n", encoding="ascii")
    return path_file


def main() -> int:
    parser = argparse.ArgumentParser(description="Configure a CPython embeddable runtime for GPTMOSS.")
    parser.add_argument("--python-directory", required=True, type=Path)
    arguments = parser.parse_args()

    configure_runtime(arguments.python_directory)
    print(f"Configured embedded Python runtime: {arguments.python_directory.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
