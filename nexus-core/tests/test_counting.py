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
