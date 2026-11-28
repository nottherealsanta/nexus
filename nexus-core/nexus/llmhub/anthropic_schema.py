"""Wire-only Claude tool schema compatibility; local validation keeps the original."""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

_COMBINATORS = ("anyOf", "oneOf", "allOf")
_UNIVERSAL = {"$ref", "$defs", "$schema", "definitions", "type", "anyOf", "allOf",
              "enum", "const", "description", "title", "default", "nullable"}
_FORMATS = {"date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid"}


def _kind(schema: dict[str, Any]) -> str | None:
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), None)
    if isinstance(kind, str):
        return kind
    if "properties" in schema:
        return "object"
    if "items" in schema or "prefixItems" in schema:
        return "array"
    return None


def _fold_root(schema: dict[str, Any]) -> dict[str, Any]:
    result = dict(schema)
    properties = dict(schema.get("properties", {}))
    required = list(schema.get("required", []))
    for key in _COMBINATORS:
        variants = schema.get(key)
        if not isinstance(variants, list) or not variants:
            continue
        conjunctive = key == "allOf"
        foldable = all(isinstance(v, dict) and _kind(v) in {None, "object"}
                       and not any(k in v for k in (*_COMBINATORS, "$ref", "not", "if"))
                       and (conjunctive or v.get("additionalProperties", False) is False) for v in variants)
        if not foldable:
            continue
        declared: dict[str, list[Any]] = {}
        shared = list(variants[0].get("required", []))
        for variant in variants:
            for name, value in variant.get("properties", {}).items():
                choices = declared.setdefault(name, [])
                if value not in choices:
                    choices.append(value)
            keys = variant.get("required", [])
            if conjunctive:
                required.extend(k for k in keys if k not in required)
            else:
                shared = [k for k in shared if k in keys]
        if not conjunctive:
            required.extend(k for k in shared if k not in required)
        for name, choices in declared.items():
            if name not in properties:
                unconstrained = not conjunctive and any(name not in v.get("properties", {})
                                                       and v.get("additionalProperties", True) is not False for v in variants)
                properties[name] = {} if unconstrained else choices[0] if len(choices) == 1 else {
                    "allOf" if conjunctive else "anyOf": choices}
    if properties or "properties" in schema:
        result["properties"] = properties
    if required or "required" in schema:
        result["required"] = required
    return result


def _normalize(schema: Any, *, root: bool = False) -> Any:
    if not isinstance(schema, dict):
        return deepcopy(schema)
    kind = _kind(schema)
    keep = _UNIVERSAL | {
        "object": {"properties", "required", "additionalProperties"},
        "array": {"items", "prefixItems", "minItems"},
        "string": {"format"},
    }.get(kind, set())
    result, spill = {}, {}
    for key, value in schema.items():
        supported = key in keep and not (root and key in _COMBINATORS)
        if key == "format":
            supported = supported and isinstance(value, str) and value in _FORMATS
        if key == "minItems":
            supported = supported and not isinstance(value, bool) and value in (0, 1)
        (result if supported else spill)[key] = deepcopy(value)
    for key in ("properties", "$defs", "definitions"):
        if isinstance(result.get(key), dict):
            result[key] = {name: _normalize(node) for name, node in result[key].items()}
    for key in ("items", "additionalProperties"):
        if isinstance(result.get(key), dict):
            result[key] = _normalize(result[key])
        elif isinstance(result.get(key), list):
            result[key] = [_normalize(node) for node in result[key]]
    for key in ("prefixItems", "anyOf", "allOf"):
        if isinstance(result.get(key), list):
            result[key] = [_normalize(node) for node in result[key]]
    if spill:
        result["description"] = (str(result.get("description", "")) +
                                 "\nInput must also satisfy these JSON Schema constraints: " +
                                 json.dumps(spill, separators=(",", ":"))).lstrip()
    return result


def normalize_tool_schema(schema: dict[str, Any]) -> dict[str, Any]:
    # Do not close unspecified/open maps: normalization must not narrow accepted inputs.
    return _normalize(_fold_root(schema), root=True)


def strict_schema_cost(schema: Any, _active: frozenset[int] = frozenset()) -> tuple[int, int] | None:
    """Count optional/union grammar slots without changing the accepted input shape."""
    if not isinstance(schema, dict):
        return None
    if id(schema) in _active:
        return None
    active = _active | {id(schema)}
    if any(key in schema for key in ("oneOf", "allOf", "$ref", "patternProperties", "propertyNames")):
        return None
    kind = _kind(schema)
    if kind is None and not any(key in schema for key in ("anyOf", "enum", "const")):
        return None
    if (kind == "object" or "additionalProperties" in schema) and schema.get("additionalProperties") is not False:
        return None
    optional = 0
    unions = int(isinstance(schema.get("type"), list) or isinstance(schema.get("anyOf"), list))
    children = []
    properties = schema.get("properties", {})
    if isinstance(properties, dict):
        optional = len(set(properties) - set(schema.get("required", [])))
        children.extend(properties.values())
    for key in ("$defs", "definitions"):
        if isinstance(schema.get(key), dict):
            children.extend(schema[key].values())
    for key in ("items", "prefixItems", "anyOf"):
        value = schema.get(key)
        if isinstance(value, list):
            children.extend(value)
        elif isinstance(value, dict):
            children.append(value)
    if kind == "array" and "items" not in schema:
        return None
    for child in children:
        cost = strict_schema_cost(child, active)
        if cost is None:
            return None
        optional += cost[0]
        unions += cost[1]
    return optional, unions
