"""Contract tests for checked-in v1 graph schema fixtures."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import jsonschema
import pytest

from minotaur.graph_model.document import GraphDocument

ROOT = Path(__file__).parents[1]
INVALID_FIXTURES = ROOT / "tests/fixtures/minotaur-graph-v1/invalid"
FORMAT_CHECKER = jsonschema.FormatChecker()


@FORMAT_CHECKER.checks("date-time")
def _is_valid_rfc3339_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError:
        return False
    return True


def _validator() -> jsonschema.Draft202012Validator:
    schema = json.loads((ROOT / "schemas/minotaur-graph/v1.json").read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema, format_checker=FORMAT_CHECKER)


@pytest.mark.parametrize(
    "fixture_name",
    [
        "invalid-generated-at.json",
        "missing-curated-rule.json",
        "unsafe-path.json",
        "wrong-position-type.json",
    ],
)
def test_structurally_invalid_fixtures_fail_schema_and_model_loading(fixture_name: str) -> None:
    data = json.loads((INVALID_FIXTURES / fixture_name).read_text(encoding="utf-8"))

    with pytest.raises(jsonschema.ValidationError):
        _validator().validate(data)
    with pytest.raises(ValueError):
        GraphDocument.from_dict(data)


def test_dangling_endpoint_is_schema_valid_until_semantic_validation_exists() -> None:
    data = json.loads((INVALID_FIXTURES / "dangling-relationship.json").read_text(encoding="utf-8"))

    _validator().validate(data)
    assert GraphDocument.from_dict(data).to_dict() == data
