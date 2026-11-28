from copy import deepcopy

from nexus.llmhub.anthropic_schema import normalize_tool_schema


def test_structural_recursion_preserves_literals_and_property_names():
    schema = {"type": "object", "properties": {
        "pattern": {"type": "string", "pattern": "^a", "default": {"minimum": 4}},
        "enum": {"enum": [{"pattern": "literal"}]},
        "array": {"type": "array", "minItems": 2, "maxItems": 3,
                  "items": {"type": "integer", "minimum": 1},
                  "prefixItems": [{"oneOf": [{"type": "string"}, {"type": "number"}]}]},
        "map": {"type": "object", "additionalProperties": {"type": "string", "maxLength": 4}},
        "nullable": {"type": ["string", "null"], "format": "email"},
    }, "$defs": {"item": {"type": "number", "maximum": 5}},
        "definitions": {"other": {"type": "string", "format": "unsupported"}}}
    before = deepcopy(schema)
    result = normalize_tool_schema(schema)
    assert schema == before
    props = result["properties"]
    assert props["pattern"]["default"] == {"minimum": 4}
    assert props["enum"]["enum"] == [{"pattern": "literal"}]
    assert "pattern" not in props["pattern"] and '"pattern"' in props["pattern"]["description"]
    assert "minItems" not in props["array"] and '"minItems":2' in props["array"]["description"]
    assert "minimum" not in props["array"]["items"]
    assert "oneOf" not in props["array"]["prefixItems"][0]
    assert "maxLength" not in props["map"]["additionalProperties"]
    assert props["nullable"]["format"] == "email"
    assert "maximum" not in result["$defs"]["item"]
    assert "format" not in result["definitions"]["other"]
    assert "additionalProperties" not in result


def test_fold_closed_object_union_and_conjunction():
    branches = [
        {"type": "object", "properties": {"kind": {"const": "a"}, "a": {"type": "string"}},
         "required": ["kind", "a"], "additionalProperties": False},
        {"type": "object", "properties": {"kind": {"const": "b"}, "b": {"type": "number"}},
         "required": ["kind", "b"], "additionalProperties": False},
    ]
    union = normalize_tool_schema({"type": "object", "anyOf": branches})
    assert set(union["properties"]) == {"kind", "a", "b"}
    assert union["properties"]["kind"] == {"anyOf": [{"const": "a"}, {"const": "b"}]}
    assert union["required"] == ["kind"]
    assert "anyOf" not in union and '"anyOf"' in union["description"]
    conjunction = normalize_tool_schema({"type": "object", "allOf": branches})
    assert conjunction["required"] == ["kind", "a", "b"]


def test_open_variants_never_narrow_input_and_nested_combinators_survive():
    union = normalize_tool_schema({"type": "object", "oneOf": [
        {"properties": {"a": {"type": "string"}}}, {"properties": {"b": {"type": "number"}}},
    ]})
    assert union["properties"] == {"a": {}, "b": {}}
    assert "additionalProperties" not in union
    result = normalize_tool_schema({"type": "object", "properties": {
        "item": {"anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}]},
        "map": {"type": "object", "additionalProperties": True},
    }})
    assert "anyOf" in result["properties"]["item"]
    assert result["properties"]["map"]["additionalProperties"] is True
