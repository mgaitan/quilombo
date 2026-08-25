#!/usr/bin/env python3
"""Validate the portable and OpenAI Quilombo plugin manifests."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from urllib.request import urlopen

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
PORTABLE_MANIFEST = ROOT / "plugin.json"
PORTABLE_MCP = ROOT / "mcp.json"
OPENAI_MANIFEST = ROOT / ".codex-plugin" / "plugin.json"
OPENAI_APP = ROOT / ".app.json"


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise ValueError(f"{path.relative_to(ROOT)} must contain a JSON object")
    return value


def validate_published_schema(document: dict) -> None:
    schema_url = document["$schema"]
    with urlopen(schema_url, timeout=15) as response:  # noqa: S310 - canonical HTTPS schemas
        schema = json.load(response)
    Draft202012Validator(schema).validate(document)


def require_file(raw_path: str) -> None:
    path = (ROOT / raw_path).resolve()
    if not path.is_relative_to(ROOT) or not path.is_file():
        raise ValueError(f"plugin path does not resolve to a file: {raw_path}")


def main() -> None:
    portable = load_json(PORTABLE_MANIFEST)
    portable_mcp = load_json(PORTABLE_MCP)
    openai = load_json(OPENAI_MANIFEST)
    app = load_json(OPENAI_APP)
    with (ROOT / "pyproject.toml").open("rb") as source:
        application_version = tomllib.load(source)["project"]["version"]

    validate_published_schema(portable)
    validate_published_schema(portable_mcp)
    if portable["$schema"].rsplit("/", 1)[0] != portable_mcp["$schema"].rsplit("/", 1)[0]:
        raise ValueError("portable manifest and MCP config target different spec versions")

    server = portable_mcp["mcpServers"]["quilombo"]
    if "headers" in server:
        raise ValueError("portable MCP configuration must not contain credentials or headers")
    if server["url"] != "https://quilombo.life/mcp":
        raise ValueError("portable MCP configuration must target the production endpoint")

    if openai["name"] != portable["name"] or openai["version"] != portable["version"]:
        raise ValueError("portable and OpenAI package identity must match")
    if portable["version"] != application_version:
        raise ValueError("plugin and application versions must match")
    if openai.get("skills") != "./skills/":
        raise ValueError("OpenAI package must use the repository skills directory")
    if openai.get("apps") != "./.app.json":
        raise ValueError("OpenAI package must reference .app.json")
    require_file(openai["apps"])
    for field in ("composerIcon", "logo"):
        require_file(openai["interface"][field])

    app_id = app["apps"]["quilombo"]["id"]
    if not app_id.startswith("asdk_app_"):
        raise ValueError(".app.json must reference a registered OpenAI MCP app")

    skill = ROOT / "skills" / "manage-quilombo-inventory" / "SKILL.md"
    if not skill.is_file():
        raise ValueError("manage-quilombo-inventory skill is missing")

    print("Portable and OpenAI plugin manifests are valid.")


if __name__ == "__main__":
    main()
