import tomllib
from pathlib import Path

PYPROJECT_PATH = Path(__file__).resolve().parent.parent / "pyproject.toml"
VERSION = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))["project"]["version"]
