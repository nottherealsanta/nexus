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
