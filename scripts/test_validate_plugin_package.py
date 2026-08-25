from copy import deepcopy

import pytest
from jsonschema import ValidationError

from scripts.validate_plugin_package import (
    OPENAI_APP,
    OPENAI_MANIFEST,
    load_json,
    validate_openai_contract,
)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda manifest: manifest["interface"].pop("displayName"),
        lambda manifest: manifest["interface"].update(websiteURL="not-a-url"),
        lambda manifest: manifest.update(unsupported=True),
    ],
)
def test_openai_contract_rejects_invalid_manifest(mutate):
    manifest = deepcopy(load_json(OPENAI_MANIFEST))
    mutate(manifest)

    with pytest.raises(ValidationError):
        validate_openai_contract(manifest, load_json(OPENAI_APP))
