"""The tool registry's argument check, used for every call and for POST /tools (AOCE-69)."""

import pytest

from aoce.config import Settings
from aoce.tools import build_registry, schema_errors

SCHEMA = {
    "type": "object",
    "properties": {
        "signal": {"type": "string", "enum": ["a", "b"]},
        "service": {"type": ["string", "null"]},
        "limit": {"type": "integer"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["signal", "limit"],
    "additionalProperties": False,
}


@pytest.mark.parametrize(
    ("value", "errors"),
    [
        ({"signal": "a", "limit": 1}, []),
        ({"signal": "a", "limit": 1, "service": None, "tags": ["x"]}, []),
        ({"limit": 1}, ["input.signal: required"]),
        ({"signal": "c", "limit": 1}, ["input.signal: must be one of ['a', 'b']"]),
        ({"signal": "a", "limit": True}, ["input.limit: expected integer, got bool"]),
        ({"signal": "a", "limit": 1.5}, ["input.limit: expected integer, got float"]),
        ({"signal": "a", "limit": 1, "tags": [1]}, ["input.tags[0]: expected string, got int"]),
        ({"signal": "a", "limit": 1, "extra": 1}, ["input.extra: unexpected property"]),
        ([], ["input: expected object, got list"]),
    ],
)
def test_schema_errors(value, errors):
    assert schema_errors(SCHEMA, value) == errors


def test_every_registered_schema_accepts_its_own_shape():
    # A schema keyword the checker does not know would silently pass; keep them to the supported subset.
    supported = {"type", "enum", "properties", "required", "additionalProperties", "items", "description"}

    def keywords(schema):
        yield from schema
        for sub in schema.get("properties", {}).values():
            yield from keywords(sub)
        if "items" in schema:
            yield from keywords(schema["items"])

    for definition in build_registry(Settings()).definitions():
        assert set(keywords(definition["input_schema"])) <= supported, definition["name"]
