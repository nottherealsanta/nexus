from copy import deepcopy

import httpx
import pytest

from nexus.llmhub.anthropic import AnthropicProvider
from nexus.llmhub.anthropic_schema import strict_schema_cost
from nexus.llmhub.types import Message, Request, Text, ToolSpec


class Auth:
    async def headers(self):
        return {"authorization": "Bearer test"}


def tool(name="probe", *, optional=0, properties=None):
    return ToolSpec(name, name, {"type": "object", "additionalProperties": False,
        "properties": properties if properties is not None else {
            f"p{i}": {"type": "string"} for i in range(optional)}})


def request(*tools):
    return Request("claude-haiku-4-5", "", (Message("user", (Text("go"),)),), tools)


def test_strict_is_opt_in_and_does_not_change_schema_or_literals():
    spec = tool(properties={"patternProperties": {"type": "string", "enum": ["$ref", "oneOf"]}})
    original = deepcopy(spec.parameters)
    req = request(spec, tool("other"))
    assert "strict" not in AnthropicProvider(Auth()).body(req)["tools"][0]
    body = AnthropicProvider(Auth(), strict_tools=("probe",)).body(req)
    assert body["tools"][0]["strict"] is True
    assert "strict" not in body["tools"][1]
    assert spec.parameters == original
    assert body["tools"][0]["input_schema"] == original


@pytest.mark.parametrize("schema", [
    {"type": "object"},
    {"type": "object", "additionalProperties": True},
    {"type": "object", "additionalProperties": {"type": "string"}},
    {"type": "object", "additionalProperties": False, "properties": {"nested": {"type": "object"}}},
    {"type": "object", "additionalProperties": False, "properties": {"value": {}}},
    {"type": "object", "additionalProperties": False, "properties": {"value": {"$ref": "#/$defs/item"}}},
    {"type": "object", "additionalProperties": False, "oneOf": [{"type": "object"}]},
    {"type": "object", "additionalProperties": False, "properties": {"values": {"type": "array"}}},
])
def test_incompatible_schemas_stay_non_strict(schema):
    assert strict_schema_cost(schema) is None
    body = AnthropicProvider(Auth(), strict_tools=("probe",)).body(request(ToolSpec("probe", "", schema)))
    assert "strict" not in body["tools"][0]


def test_optional_budget_demotes_without_nullable_required_conversion():
    specs = [tool("first", optional=24), tool("overflow", optional=1), tool("last")]
    body = AnthropicProvider(Auth(), strict_tools=tuple(t.name for t in specs)).body(request(*specs))
    assert [t.get("strict", False) for t in body["tools"]] == [True, False, True]
    assert "required" not in body["tools"][1]["input_schema"]
    assert body["tools"][1]["input_schema"]["properties"]["p0"] == {"type": "string"}


def test_union_and_tool_budgets_are_request_wide():
    union = {"type": ["string", "null"]}
    first = tool("first", properties={f"p{i}": union for i in range(16)})
    second = tool("second", properties={"p": union})
    body = AnthropicProvider(Auth(), strict_tools=("first", "second")).body(request(first, second))
    assert [t.get("strict", False) for t in body["tools"]] == [True, False]
    specs = [tool(f"t{i}") for i in range(21)]
    body = AnthropicProvider(Auth(), strict_tools=tuple(t.name for t in specs)).body(request(*specs))
    assert sum(t.get("strict", False) for t in body["tools"]) == 20


def test_recursive_cost_charges_every_serialized_occurrence():
    nested = {"type": "object", "additionalProperties": False,
              "properties": {"optional": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}
    schema = tool(properties={"a": nested, "b": nested}).parameters
    assert strict_schema_cost(schema) == (4, 2)
    schema["required"] = ["a", "b"]
    assert strict_schema_cost(schema) == (2, 2)


def test_eager_tools_remain_non_strict():
    body = AnthropicProvider(Auth(), strict_tools=("probe",), eager_tools=("probe",)).body(request(tool()))
    assert body["tools"][0]["eager_input_streaming"] is True
    assert "strict" not in body["tools"][0]


def test_cyclic_schema_is_not_a_strict_candidate():
    schema = {"type": "object", "additionalProperties": False, "properties": {}}
    schema["properties"]["self"] = schema
    assert strict_schema_cost(schema) is None


def test_type_inference_does_not_close_an_open_map():
    assert strict_schema_cost({"additionalProperties": {"type": "string"}, "anyOf": [{"type": "object"}]}) is None


@pytest.mark.asyncio
async def test_strict_http_rejection_is_not_retried_or_downgraded():
    from nexus.llmhub.types import ProviderError, WireRequest

    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(400, text="strict schema rejected")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = AnthropicProvider(Auth(), client=client, strict_tools=("probe",))
        stream = provider.stream(request(tool()))
        wire = await anext(stream)
        assert isinstance(wire, WireRequest) and wire.body["tools"][0]["strict"] is True
        assert "structured-outputs-2025-12-15" in wire.headers["anthropic-beta"]
        with pytest.raises(ProviderError, match="HTTP 400"):
            _ = [event async for event in stream]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_beta_headers_are_final_body_derived_and_deduplicated():
    class BetaAuth:
        async def headers(self):
            return {"anthropic-beta": " oauth-2025-04-20, EXTENDED-CACHE-TTL-2025-04-11,oauth-2025-04-20 "}

    provider = AnthropicProvider(BetaAuth(), strict_tools=("probe",))
    stream = provider.stream(request(ToolSpec("probe", "", {"type": "object"})))
    wire = await anext(stream)
    await stream.aclose()
    betas = wire.headers["anthropic-beta"].lower().split(",")
    assert len(betas) == len(set(betas)) == 2
    assert "structured-outputs-2025-12-15" not in betas
