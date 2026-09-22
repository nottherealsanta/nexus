from nexus.core.registry import Registry, RegistryRef


def test_empty_registry():
    registry = Registry()
    assert registry.generation == 0
    assert len(registry) == 0
    assert registry.get("x") is None
    assert registry.get("x", 1) == 1
    assert registry.all() == {}


def test_replace_and_merge_bump_generation_without_mutating_original():
    base = Registry({"a": 1})
    merged = base.merge({"b": 2})
    assert merged.generation == 1
    assert dict(merged.all()) == {"a": 1, "b": 2}
    assert dict(base.all()) == {"a": 1}
    assert merged.get("a") == 1
    assert "b" in merged

    removed = merged.without("a")
    assert removed.generation == 2
    assert set(removed.names()) == {"b"}


def test_replace_is_a_snapshot():
    base = Registry({"a": 1})
    replaced = base.replace({"c": 3})
    assert dict(replaced.all()) == {"c": 3}
    assert dict(base.all()) == {"a": 1}


def test_registry_view_is_read_only():
    registry = Registry({"a": 1})
    view = registry.all()
    try:
        view["b"] = 2
        assert False
    except TypeError:
        pass


def test_registry_ref_swap_returns_previous_atomically():
    ref = RegistryRef(Registry({"a": 1}))
    original = ref.get()
    replacement = original.merge({"b": 2})
    previous = ref.swap(replacement)
    assert previous is original
    assert ref.get() is replacement
    assert ref.get().get("b") == 2
    assert ref.get().generation == 1
