#!/usr/bin/env python3
"""Validate the portable and OpenAI Quilombo plugin manifests."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parent.parent
PORTABLE_MANIFEST = ROOT / "plugin.json"
PORTABLE_MCP = ROOT / "mcp.json"
OPENAI_MANIFEST = ROOT / ".codex-plugin" / "plugin.json"
OPENAI_APP = ROOT / ".app.json"
PUBLISHED_MCP_TOOLS = {
    "audit_inventory",
    "bulk_upsert_inventory",
    "delete_inventory_item",
    "find_inventory",
    "get_attribute_profile",
    "get_inventory_snapshot",
    "get_inventory_status",
    "lookup_book_by_isbn",
    "move_inventory",
    "update_inventory_item",
}
MCP_DOCUMENTATION = (ROOT / "README.md", ROOT / "docs" / "mcp.md")
SEMVER_PATTERN = (
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
NON_EMPTY_STRING = {"type": "string", "pattern": r"\S"}
HTTPS_URL = {"type": "string", "format": "https-url"}
OPENAI_FORMAT_CHECKER = FormatChecker()


@OPENAI_FORMAT_CHECKER.checks("https-url")
def is_absolute_https_url(value: object) -> bool:
    if not isinstance(value, str):
        return True
    try:
        parsed = urlsplit(value)
        return parsed.scheme == "https" and bool(parsed.hostname)
    except ValueError:
        return False


OPENAI_MANIFEST_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "version", "description", "author", "interface"],
    "properties": {
        "id": NON_EMPTY_STRING,
        "name": NON_EMPTY_STRING,
        "version": {"type": "string", "pattern": SEMVER_PATTERN},
        "description": NON_EMPTY_STRING,
        "skills": NON_EMPTY_STRING,
        "apps": NON_EMPTY_STRING,
        "mcpServers": {"oneOf": [NON_EMPTY_STRING, {"type": "object"}]},
        "author": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name"],
            "properties": {
                "name": NON_EMPTY_STRING,
                "email": NON_EMPTY_STRING,
                "url": HTTPS_URL,
            },
        },
        "homepage": HTTPS_URL,
        "repository": HTTPS_URL,
        "license": NON_EMPTY_STRING,
        "keywords": {
            "type": "array",
            "items": NON_EMPTY_STRING,
        },
        "interface": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "displayName",
                "shortDescription",
                "longDescription",
                "developerName",
                "category",
                "capabilities",
            ],
            "anyOf": [
                {"required": ["defaultPrompt"]},
                {"required": ["default_prompt"]},
            ],
            "properties": {
                "displayName": NON_EMPTY_STRING,
                "shortDescription": NON_EMPTY_STRING,
                "longDescription": NON_EMPTY_STRING,
                "developerName": NON_EMPTY_STRING,
                "category": NON_EMPTY_STRING,
                "capabilities": {
                    "type": "array",
                    "minItems": 1,
                    "items": NON_EMPTY_STRING,
                },
                "websiteURL": HTTPS_URL,
                "privacyPolicyURL": HTTPS_URL,
                "termsOfServiceURL": HTTPS_URL,
                "brandColor": {"type": "string", "pattern": r"^#[0-9A-Fa-f]{6}$"},
                "composerIcon": NON_EMPTY_STRING,
                "logo": NON_EMPTY_STRING,
                "logoDark": NON_EMPTY_STRING,
                "screenshots": {
                    "type": "array",
                    "items": NON_EMPTY_STRING,
                },
                "defaultPrompt": {
                    "oneOf": [
                        NON_EMPTY_STRING,
                        {"type": "array", "minItems": 1, "items": NON_EMPTY_STRING},
                    ]
                },
                "default_prompt": NON_EMPTY_STRING,
            },
        },
    },
}
OPENAI_APP_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["apps"],
    "properties": {
        "apps": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id"],
                "properties": {
                    "id": {"type": "string", "pattern": r"^asdk_app_[0-9A-Za-z]+$"},
                    "category": NON_EMPTY_STRING,
                },
            },
        }
    },
}


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


def validate_openai_contract(manifest: dict, app: dict) -> None:
    Draft202012Validator(
        OPENAI_MANIFEST_SCHEMA,
        format_checker=OPENAI_FORMAT_CHECKER,
    ).validate(manifest)
    Draft202012Validator(OPENAI_APP_SCHEMA).validate(app)


def require_file(raw_path: str) -> None:
    declared_path = Path(raw_path)
    if declared_path.is_absolute():
        raise ValueError(f"plugin path must be relative: {raw_path}")
    path = (ROOT / declared_path).resolve()
    if not path.is_relative_to(ROOT) or not path.is_file():
        raise ValueError(f"plugin path does not resolve to a file: {raw_path}")


def validate_openai_assets(manifest: dict) -> None:
    interface = manifest["interface"]
    for field in ("composerIcon", "logo", "logoDark"):
        if field in interface:
            require_file(interface[field])
    for screenshot in interface.get("screenshots", []):
        require_file(screenshot)


def validate_mcp_documentation() -> None:
    for path in MCP_DOCUMENTATION:
        document = path.read_text(encoding="utf-8")
        documented = {
            name
            for name in re.findall(r"^\| `([^`]+)` \|", document, flags=re.MULTILINE)
            if "://" not in name
        }
        if documented != PUBLISHED_MCP_TOOLS:
            missing = sorted(PUBLISHED_MCP_TOOLS - documented)
            extra = sorted(documented - PUBLISHED_MCP_TOOLS)
            raise ValueError(
                f"{path.relative_to(ROOT)} MCP tools are out of sync; "
                f"missing={missing}, extra={extra}"
            )
        if "https://quilombo.life/mcp" not in document:
            raise ValueError(f"{path.relative_to(ROOT)} must document the hosted MCP endpoint")


def main() -> None:
    portable = load_json(PORTABLE_MANIFEST)
    portable_mcp = load_json(PORTABLE_MCP)
    openai = load_json(OPENAI_MANIFEST)
    app = load_json(OPENAI_APP)
    with (ROOT / "pyproject.toml").open("rb") as source:
        application_version = tomllib.load(source)["project"]["version"]

    validate_published_schema(portable)
    validate_published_schema(portable_mcp)
    validate_openai_contract(openai, app)
    validate_mcp_documentation()
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
    validate_openai_assets(openai)

    app_id = app["apps"]["quilombo"]["id"]
    if not app_id.startswith("asdk_app_"):
        raise ValueError(".app.json must reference a registered OpenAI MCP app")

    skill = ROOT / "skills" / "manage-quilombo-inventory" / "SKILL.md"
    if not skill.is_file():
        raise ValueError("manage-quilombo-inventory skill is missing")

    print("Portable and OpenAI plugin manifests are valid.")


if __name__ == "__main__":
    main()
