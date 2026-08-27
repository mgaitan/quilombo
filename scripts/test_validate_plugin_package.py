from copy import deepcopy

import pytest
from jsonschema import ValidationError

from scripts.validate_plugin_package import (
    OPENAI_APP,
    OPENAI_MANIFEST,
    load_json,
    validate_mcp_documentation,
    validate_openai_assets,
    validate_openai_contract,
)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda manifest: manifest["interface"].pop("displayName"),
        lambda manifest: manifest["interface"].update(websiteURL="not-a-url"),
        lambda manifest: manifest["interface"].update(websiteURL="https://?callback"),
        lambda manifest: manifest["interface"].update(displayName="   "),
        lambda manifest: manifest.update(unsupported=True),
    ],
)
def test_openai_contract_rejects_invalid_manifest(mutate):
    manifest = deepcopy(load_json(OPENAI_MANIFEST))
    mutate(manifest)

    with pytest.raises(ValidationError):
        validate_openai_contract(manifest, load_json(OPENAI_APP))


@pytest.mark.parametrize(
    "field,value",
    [
        ("logoDark", "./inventory/static/inventory/missing.png"),
        ("screenshots", ["../outside.png"]),
    ],
)
def test_openai_assets_must_exist_inside_package(field, value):
    manifest = deepcopy(load_json(OPENAI_MANIFEST))
    manifest["interface"][field] = value

    with pytest.raises(ValueError):
        validate_openai_assets(manifest)


def test_mcp_documentation_matches_published_tool_contract():
    validate_mcp_documentation()
