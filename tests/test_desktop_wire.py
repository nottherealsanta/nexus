import base64
import hashlib
import json

from nexus.ui.desktop.wire import DesktopWire


def snapshot(**changes):
    value = {
        "schema": 2,
        "revision": 7,
        "generation": 1,
        "agent_page": "chat",
        "title": "Nexus",
        "blocks": [{"id": "a", "rev": 1, "text": "hello"}],
        "blocks_from": 0,
    }
    value.update(changes)
    return value


def test_first_encode_resets_with_every_topic_and_initial_blocks():
    wire = DesktopWire()
    encoded = wire.encode(snapshot())

    assert encoded["reset"] is True
    assert encoded["schema"] == 3
    assert encoded["generation"] == 1
    assert encoded["revision"] == 7
    assert "header" in encoded["topics"]
    assert encoded["blocks"] == {
        "splice": {"from": 0, "blocks": [{"id": "a", "rev": 1, "text": "hello"}]}
    }
    assert "schema" not in encoded["topics"]["header"]
    assert "revision" not in encoded["topics"]["header"]
    assert "generation" not in encoded["topics"]["header"]
    assert "blocks_from" not in encoded["topics"]["header"]


def test_unchanged_snapshot_is_suppressed_even_when_revision_changes():
    wire = DesktopWire()
    wire.encode(snapshot())

    assert wire.encode(snapshot(revision=8)) is None


def test_topics_fingerprint_independently_and_unknown_fields_fall_back_to_header():
    wire = DesktopWire()
    wire.encode(snapshot(composer_key="one", sessions=["s"], unknown_data={"x": 1}))

    encoded = wire.encode(
        snapshot(composer_key="two", sessions=["s"], unknown_data={"x": 1})
    )
    assert encoded["topics"] == {"composer": {"composer_key": "two"}}
    encoded = wire.encode(
        snapshot(composer_key="two", sessions=["s"], unknown_data={"x": 2})
    )
    assert encoded["topics"]["header"] == {
        "agent_page": "chat",
        "title": "Nexus",
        "unknown_data": {"x": 2},
    }


def test_identity_change_forces_full_reset():
    wire = DesktopWire()
    wire.encode(snapshot())

    encoded = wire.encode(snapshot(generation=2, title="Nexus 2"))
    assert encoded["reset"] is True
    assert encoded["blocks"]["splice"]["from"] == 0
    assert set(encoded["topics"]) == {
        "header",
        "composer",
        "sessions",
        "details",
        "logs",
        "panel",
        "form",
        "prompt",
        "history",
    }


def test_agent_page_change_forces_full_reset():
    wire = DesktopWire()
    wire.encode(snapshot())

    encoded = wire.encode(snapshot(agent_page="settings"))

    assert encoded["reset"] is True
    assert encoded["blocks"] == {
        "splice": {"from": 0, "blocks": [{"id": "a", "rev": 1, "text": "hello"}]}
    }


def test_appended_text_uses_append_delta():
    wire = DesktopWire()
    first = snapshot(blocks=[{"id": "a", "rev": 1, "text": "hello"}])
    wire.encode(first)

    encoded = wire.encode(
        snapshot(blocks=[{"id": "a", "rev": 2, "text": "hello world"}])
    )
    assert encoded["blocks"] == {"append": [{"id": "a", "text": " world", "rev": 2}]}


def test_unicode_append_delta_stays_small_with_large_unchanged_topics():
    wire = DesktopWire()
    previous_text = "界" * 60_000
    large_sidebar = ["sidebar entry " * 2_000]
    large_context = ["context entry " * 2_000]
    unchanged = {
        "sessions_sidebar": large_sidebar,
        "context_lines": large_context,
    }
    wire.encode(
        snapshot(blocks=[{"id": "a", "rev": 1, "text": previous_text}], **unchanged)
    )

    encoded = wire.encode(
        snapshot(
            blocks=[{"id": "a", "rev": 2, "text": previous_text + "追加🙂"}],
            **unchanged,
        )
    )

    assert encoded["topics"] == {}
    assert encoded["blocks"] == {"append": [{"id": "a", "text": "追加🙂", "rev": 2}]}
    assert len(json.dumps(encoded, ensure_ascii=False).encode("utf-8")) < 8 * 1024


def test_streaming_a_token_into_a_two_thousand_block_session_stays_under_8_kib():
    # Overhaul plan §0/§3.3: a streamed reply must not resend the whole growing
    # transcript. The last block grows by suffix; every other block and topic is
    # unchanged, so each update is one tiny append op.
    wire = DesktopWire()
    history = [
        {"id": f"block-{index}", "rev": 1, "text": f"prior message {index}"}
        for index in range(1_999)
    ]
    text = "The reply begins."
    blocks = history + [{"id": "stream", "rev": 1, "text": text}]
    wire.encode(snapshot(blocks=blocks))

    total = 0
    for token in range(500):
        previous = text
        text += " token"
        encoded = wire.encode(
            snapshot(blocks=history + [{"id": "stream", "rev": token + 2, "text": text}])
        )
        payload = json.dumps(encoded, ensure_ascii=False).encode("utf-8")
        assert encoded["topics"] == {}
        assert encoded["blocks"] == {
            "append": [{"id": "stream", "text": text[len(previous):], "rev": token + 2}]
        }
        assert len(payload) <= 8 * 1024, f"token {token} sent {len(payload)} bytes"
        total += len(payload)
    # Linear in tokens, not quadratic: 500 appends are far below a single full
    # resend of the 2,000-block transcript.
    assert total < 64 * 1024


def test_changed_block_or_block_structure_uses_splice():
    wire = DesktopWire()
    wire.encode(snapshot(blocks=[{"id": "a", "rev": 1, "text": "hello"}]))

    encoded = wire.encode(snapshot(blocks=[{"id": "a", "rev": 2, "text": "changed"}]))
    assert encoded["blocks"] == {
        "splice": {"from": 0, "blocks": [{"id": "a", "rev": 2, "text": "changed"}]}
    }
    encoded = wire.encode(
        snapshot(
            blocks=[
                {"id": "a", "rev": 2, "text": "changed"},
                {"id": "b", "rev": 1, "text": "new"},
            ]
        )
    )
    assert encoded["blocks"] == {
        "splice": {"from": 1, "blocks": [{"id": "b", "rev": 1, "text": "new"}]}
    }


def test_same_revision_metadata_change_splices_from_changed_block():
    wire = DesktopWire()
    first_blocks = [
        {"id": "a", "rev": 1, "text": "first", "kind": "message"},
        {"id": "b", "rev": 1, "text": "second", "kind": "message"},
    ]
    wire.encode(snapshot(blocks=first_blocks))

    changed_blocks = [
        first_blocks[0],
        {"id": "b", "rev": 1, "text": "second", "kind": "notice"},
    ]
    encoded = wire.encode(snapshot(blocks=changed_blocks))

    assert encoded["blocks"] == {"splice": {"from": 1, "blocks": [changed_blocks[1]]}}


def test_one_shot_composer_fields_repeat_and_then_clear():
    wire = DesktopWire()
    initial = snapshot(insert="paste me")
    wire.encode(initial)

    repeated = wire.encode(initial)
    assert repeated["topics"]["composer"]["insert"] == "paste me"
    cleared = wire.encode(snapshot(insert=""))
    assert cleared["topics"]["composer"]["insert"] == ""
    assert wire.encode(snapshot(insert="")) is None


def test_one_shot_restore_repeats_and_then_clears():
    wire = DesktopWire()
    initial = snapshot(restore={"text": "restored"})
    wire.encode(initial)

    repeated = wire.encode(initial)
    assert repeated["topics"]["composer"]["restore"] == {"text": "restored"}
    cleared = wire.encode(snapshot(restore=None))
    assert cleared["topics"]["composer"]["restore"] is None
    assert wire.encode(snapshot(restore=None)) is None


def test_empty_snapshot_has_a_defined_history_topic_and_stable_fingerprints():
    wire = DesktopWire()
    wire.encode(snapshot())
    changed = wire.encode(snapshot(history=["entry"]))
    assert changed["topics"] == {"history": {"history": ["entry"]}}
    assert wire.encode(snapshot(history=["entry"])) is None


def test_all_topic_fields_are_preserved_in_their_topics():
    fields = {
        "composer_key": "key",
        "queue_lines": [],
        "sessions": [],
        "details_panel": {},
        "logs": [],
        "panel_format": "text",
        "form": {},
        "prompt": {},
        "history": [],
    }
    wire = DesktopWire()
    encoded = wire.encode(snapshot(**fields))
    grouped = {}
    for topic_fields in encoded["topics"].values():
        grouped.update(topic_fields)
    assert all(grouped[key] == value for key, value in fields.items())
    assert set(encoded["topics"]) == {
        "header",
        "composer",
        "sessions",
        "details",
        "logs",
        "panel",
        "form",
        "prompt",
        "history",
    }


def test_same_ids_must_also_be_in_order_for_append():
    wire = DesktopWire()
    wire.encode(
        snapshot(
            blocks=[
                {"id": "a", "rev": 1, "text": "a"},
                {"id": "b", "rev": 1, "text": "b"},
            ]
        )
    )

    encoded = wire.encode(
        snapshot(
            blocks=[
                {"id": "b", "rev": 2, "text": "better"},
                {"id": "a", "rev": 1, "text": "a"},
            ]
        )
    )
    assert "append" not in encoded["blocks"]
    assert encoded["blocks"]["splice"]["from"] == 0


def test_schema_and_revision_never_change_topic_fingerprints():
    wire = DesktopWire()
    wire.encode(snapshot())
    assert wire.encode(snapshot(schema=99, revision=123)) is None


def test_image_put_is_content_addressed_and_payload_is_replaced_by_ref():
    data = base64.b64encode(b"thumbnail bytes").decode("ascii")
    image = {"id": "draft-1", "media": "image/png", "data": data}
    wire = DesktopWire()
    encoded = wire.encode(snapshot(inline_images=[image]))
    image_id = "sha256:" + hashlib.sha256(b"thumbnail bytes").hexdigest()
    assert encoded["images"]["put"] == [{"id": image_id, "media": "image/png", "data": data}]
    assert encoded["topics"]["header"]["inline_images"] == [
        {"id": "draft-1", "media": "image/png", "data_ref": image_id}
    ]

    assert wire.encode(snapshot(inline_images=[image])) is None


def test_absent_inline_images_stay_absent():
    encoded = DesktopWire().encode(snapshot())
    assert "inline_images" not in encoded["topics"]["header"]


def test_python_snapshot_preview_and_inline_image_fields_are_encoded():
    preview = base64.b64encode(b"preview").decode("ascii")
    inline = base64.b64encode(b"inline").decode("ascii")
    encoded = DesktopWire().encode(
        snapshot(
            preview_image=preview,
            preview_image_media="image/jpeg",
            inline_images=[{"id": "draft", "media": "image/png", "data": inline}],
        )
    )
    fields = encoded["topics"]["panel"]
    assert fields["preview_image_media"] == "image/jpeg"
    assert fields["preview_image"].startswith("sha256:")
    assert encoded["images"]["put"][0]["media"] == "image/jpeg"
    assert encoded["topics"]["header"]["inline_images"] == [
        {
            "id": "draft",
            "media": "image/png",
            "data_ref": "sha256:" + hashlib.sha256(b"inline").hexdigest(),
        }
    ]


def test_image_put_and_removal_are_transactional_and_report_drops():
    data = base64.b64encode(b"image").decode("ascii")
    wire = DesktopWire()
    first = wire.encode(snapshot(preview_image=data, preview_image_media="image/png"))
    image_id = first["images"]["put"][0]["id"]
    removed = wire.encode(snapshot(preview_image="", preview_image_media="image/png"))
    assert removed["images"]["drop"] == [image_id]


def test_image_drop_and_reset_repopulate_cache():
    data = base64.b64encode(b"thumbnail bytes").decode("ascii")
    wire = DesktopWire()
    first = wire.encode(snapshot(preview_image=data, preview_image_media="image/jpeg"))
    image_id = "sha256:" + hashlib.sha256(b"thumbnail bytes").hexdigest()
    assert first["images"]["put"][0]["id"] == image_id

    dropped = wire.encode(snapshot(preview_image="", preview_image_media="image/jpeg"))
    assert dropped["images"]["drop"] == [image_id]

    reset = wire.encode(snapshot(generation=2, preview_image=data, preview_image_media="image/jpeg"))
    assert reset["reset"] is True
    assert reset["images"] == {"put": [{"id": image_id, "media": "image/jpeg", "data": data}], "drop": []}


def test_image_cache_is_bounded_to_preview_plus_eight_inline_images():
    wire = DesktopWire()
    inline = [
        {"id": str(index), "media": "image/png", "data": base64.b64encode(f"image{index}".encode()).decode()}
        for index in range(10)
    ]
    encoded = wire.encode(snapshot(inline_images=inline))
    assert len(encoded["images"]["put"]) == 9
    assert sum(bool(item["data_ref"]) for item in encoded["topics"]["header"]["inline_images"]) == 9
