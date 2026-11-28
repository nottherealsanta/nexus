from nexus.counting import budget


def test_budget_labels_estimates_and_unknown_window():
    result = budget({"input": "hello"}, None)
    assert result["used"] > 0
    assert result["window"] is None
    assert result["exact"] is False


def test_budget_prefers_measured_usage_including_zero():
    assert budget({"input": "hello"}, 200000, 123) == {
        "used": 123, "window": 200000, "exact": True,
    }
    assert budget({}, 200000, 0)["used"] == 0


def test_estimate_prices_an_inline_image_flat():
    image = "data:image/png;base64," + "A" * 400_000
    assert budget({"input": [{"image_url": image}]}, None)["used"] < 2_000


def test_estimate_ignores_reasoning_effort():
    for body in (
        {"messages": [], "reasoning_effort": "low"},
        {"instructions": "Work.", "reasoning": {"summary": "auto", "effort": "low"}},
        {"system": "Work.", "effort": "low"},
    ):
        without_effort = {key: value for key, value in body.items() if key not in ("reasoning_effort", "effort")}
        if isinstance(without_effort.get("reasoning"), dict):
            without_effort["reasoning"] = {"summary": "auto"}
        assert budget(body, None)["used"] == budget(without_effort, None)["used"]

    chat = {"messages": [{"role": "user", "content": "hi"}], "reasoning_effort": "low"}
    assert budget(chat, None)["used"] == budget({**chat, "reasoning_effort": "high"}, None)["used"]


def test_anthropic_image_and_generation_options_do_not_inflate_context():
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 400000}}
    assert budget({"model": "claude-haiku-4-5", "stream": True, "max_tokens": 128,
                   "messages": [{"content": [image]}]}, None)["used"] < 2000
    body = {"model": "claude-haiku-4-5", "system": [{"type": "text", "text": "hello"}], "messages": [], "stream": True, "max_tokens": 1}
    assert budget(body, None) == budget({**body, "max_tokens": 64000, "thinking": {"type": "enabled", "budget_tokens": 8192},
                                       "output_config": {"effort": "high"}}, None)


def test_non_direct_bodies_keep_generation_fields_and_native_objects():
    body = {"model": "other", "messages": [], "max_tokens": 100000,
            "thinking": {"data": "x" * 100}, "output_config": {"data": "y" * 100}}
    assert budget(body, None)["used"] > budget({"model": "other", "messages": []}, None)["used"]
    image = {"type": "image", "source": {"type": "base64", "data": "a" * 400000}}
    assert budget({"messages": [{"content": [image]}]}, None)["used"] > 100000
