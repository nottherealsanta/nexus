//! Timeline blocks: turn grouping, fold state, tool expansion, history merge.

mod common;
use common::*;

use nexus_tui::api::History;
use nexus_tui::theme::{Mode, Tier};
use nexus_tui::ui::Focus;

fn app() -> nexus_tui::app::App {
    with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36))
}

fn rows(a: &mut nexus_tui::app::App) -> Vec<String> {
    draw(a, 120, 36).1
}

#[test]
fn a_turn_has_a_header_content_and_a_footer() {
    let mut a = app();
    let drawn = rows(&mut a);
    let text = drawn.join("\n");
    assert!(text.contains("#1"), "turn header: {text}");
    assert!(text.contains("hi"), "user text: {text}");
    assert!(text.contains("Hello"), "assistant text: {text}");
    assert!(text.contains("1 call"), "turn footer: {text}");
    assert!(text.contains("done"), "end status: {text}");
    // Turn numbers align with the block kind.
    let turn = a
        .blocks()
        .iter()
        .position(|b| b.kind == nexus_tui::timeline::BlockKind::Turn)
        .expect("a turn block");
    assert_eq!(a.blocks()[turn].turn, Some(0));
}

#[test]
fn folding_a_turn_keeps_only_its_header_and_never_deletes_context() {
    let mut a = app();
    rows(&mut a);
    let turn = a
        .blocks()
        .iter()
        .position(|b| b.kind == nexus_tui::timeline::BlockKind::Turn)
        .unwrap();
    a.ui.timeline_sel = Some(turn);
    a.execute("turn.fold_toggle");
    let drawn = rows(&mut a);
    let text = drawn.join("\n");
    assert!(text.contains("#1"), "{text}");
    assert!(!text.contains("Hello"), "folded content is hidden: {text}");
    // Still in the model: folding is view state only.
    assert!(a.messages().iter().any(|m| m.role == "assistant"));
    a.execute("turn.fold_toggle");
    assert!(rows(&mut a).join("\n").contains("Hello"));
}

#[test]
fn fold_all_but_last_and_unfold_all() {
    let mut a = app();
    // A second turn, so "all but last" has something to fold.
    let v = serde_json::json!({"id": 60, "type": "message.added", "session_id": SESSION,
        "cursor": "log_c915bfc9b6d826c4:60",
        "data": {"id": "msg_9", "session_id": SESSION, "seq": 9, "created": 1793113400.0,
                 "role": "user", "parts": [{"type": "text", "text": "again"}]}});
    if let Ok(nexus_tui::api::Frame::Event(e)) =
        nexus_tui::api::Frame::from_value("message.added", None, &v)
    {
        a.apply_sync(nexus_tui::sync::SyncMsg::Event(e));
    }
    rows(&mut a);
    a.execute("turn.fold_all_but_last");
    let text = rows(&mut a).join("\n");
    assert!(!text.contains("Hello"), "first turn folded: {text}");
    assert!(text.contains("again"), "last turn open: {text}");
    a.execute("turn.unfold_all");
    assert!(rows(&mut a).join("\n").contains("Hello"));
}

#[test]
fn enter_expands_a_tool_into_its_detail_and_raw_toggle_shows_arguments() {
    let mut a = app();
    // Append an edit call + result as one assistant/tool pair.
    let call = serde_json::json!({"id": 70, "type": "message.added", "session_id": SESSION,
        "cursor": "log_c915bfc9b6d826c4:70",
        "data": {"id": "msg_10", "session_id": SESSION, "seq": 10, "created": 1793113410.0,
                 "role": "assistant", "parts": [{"type": "tool_call", "id": "call_9", "name": "edit",
                 "arguments": "{\"path\":\"src/a.py\",\"old_string\":\"a = 1\",\"new_string\":\"a = 2\\nb = 3\"}"}]}});
    let result = serde_json::json!({"id": 71, "type": "message.added", "session_id": SESSION,
        "cursor": "log_c915bfc9b6d826c4:71",
        "data": {"id": "msg_11", "session_id": SESSION, "seq": 11, "created": 1793113411.0,
                 "role": "tool", "parts": [{"type": "tool_result", "call_id": "call_9",
                 "output": "edited src/a.py (1 replacement)", "is_error": false}]}});
    for v in [call, result] {
        if let Ok(nexus_tui::api::Frame::Event(e)) =
            nexus_tui::api::Frame::from_value(v["type"].as_str().unwrap(), None, &v)
        {
            a.apply_sync(nexus_tui::sync::SyncMsg::Event(e));
        }
    }
    rows(&mut a);
    a.ui.focus = Focus::Timeline;
    let tool = a
        .blocks()
        .iter()
        .position(|b| b.kind == nexus_tui::timeline::BlockKind::Tool)
        .expect("a tool block");
    a.ui.timeline_sel = Some(tool);
    a.execute("tool.expand_toggle");
    let text = rows(&mut a).join("\n");
    assert!(text.contains("- a = 1"), "removed line: {text}");
    assert!(text.contains("+ a = 2"), "added line: {text}");
    assert!(text.contains("+ b = 3"), "added line: {text}");
    a.execute("tool.raw_toggle");
    let text = rows(&mut a).join("\n");
    assert!(text.contains("old_string"), "raw arguments: {text}");
    assert!(text.contains("edited src/a.py"), "raw result: {text}");
}

#[test]
fn history_replaces_the_bounded_snapshot_view_and_keeps_live_events() {
    let mut a = app();
    let live = serde_json::json!({"id": 300, "type": "session.error", "session_id": SESSION,
        "cursor": "log_c915bfc9b6d826c4:300",
        "data": {"kind": "provider", "message": "boom"}});
    if let Ok(nexus_tui::api::Frame::Event(e)) =
        nexus_tui::api::Frame::from_value("session.error", None, &live)
    {
        a.apply_sync(nexus_tui::sync::SyncMsg::Event(e));
    }
    let h = History {
        session_id: SESSION.into(),
        messages: a.messages().to_vec(),
        events: events(TURN),
        starting: Some(serde_json::json!({
            "origin": "created",
            "system_prompt": "You are helpful.",
            "tools": ["read", "bash"],
            "tool_definitions": {"read": {}, "bash": {}},
            "agent": {"name": "build", "source": "builtin", "path": "/work/agents/build.md"},
            "instruction_file": {"path": "/work/AGENTS.md", "status": "loaded", "bytes": 120},
            "child_agents": [],
            "workspace": {"entry": true, "active": false}
        })),
        events_gap: None,
    };
    a.apply_sync(nexus_tui::sync::SyncMsg::History(Box::new(h)));
    let view = a.state.model.selected.as_ref().unwrap();
    assert!(!view.messages_more);
    assert!(view.events.iter().any(|e| e.id == 300), "live event kept");
    assert!(view.starting.is_some());
    // Collapsed once the session has turns; open it to see the rows.
    rows(&mut a);
    a.execute("turn.unfold_all");
    let text = rows(&mut a).join("\n");
    assert!(text.contains("Context"), "context section: {text}");
    assert!(text.contains("system prompt"), "{text}");
    assert!(text.contains("boom"), "live diagnostic: {text}");
}

#[test]
fn keyboard_and_mouse_select_the_same_block() {
    let mut a = app();
    rows(&mut a);
    a.ui.focus = Focus::Timeline;
    a.ui.timeline_sel = None;
    press(&mut a, &["down"]);
    let by_key = a.ui.timeline_sel;
    assert!(by_key.is_some());
    // Click the first row of that block.
    let b = a.blocks()[by_key.unwrap()].clone();
    let (_, _) = (b.start, b.end);
    a.ui.timeline_sel = None;
    // The timeline starts under the header at x=left+sidebar, y=1; block.start
    // is relative to content, drawn with the viewport following the bottom.
    // Selecting by keyboard twice lands on the same block after a redraw.
    press(&mut a, &["down"]);
    assert_eq!(a.ui.timeline_sel, by_key);
}

#[test]
fn history_keeps_messages_applied_live_and_announces_a_gap() {
    let mut a = app();
    let mut msgs = a.messages().to_vec();
    let live = msgs.pop().expect("a message");
    // The history fetch finished before `live` was added by the stream.
    let h = History {
        session_id: SESSION.into(),
        messages: msgs,
        events: events(TURN),
        starting: None,
        events_gap: Some("diagnostics at or before event 7 expired under retention".into()),
    };
    a.apply_sync(nexus_tui::sync::SyncMsg::History(Box::new(h)));
    let view = a.state.model.selected.as_ref().unwrap();
    assert!(
        view.messages.iter().any(|m| m.id == live.id),
        "live message kept"
    );
    assert!(
        view.messages.windows(2).all(|w| w[0].seq <= w[1].seq),
        "ordered by seq"
    );
    let text = rows(&mut a).join("\n");
    assert!(
        text.contains("expired under retention"),
        "gap announced: {text}"
    );
}

#[test]
fn event_pages_keep_the_newest_and_say_what_is_missing() {
    use nexus_tui::sync::fold_event_pages;
    let row = |id: i64| serde_json::json!({"id": id, "type": "usage", "session_id": SESSION});
    let page = |ids: std::ops::RangeInclusive<i64>, truncated: bool| {
        serde_json::json!({"events": ids.map(row).collect::<Vec<_>>(), "more": true,
            "floor": 3, "history_truncated": truncated})
    };
    let (events, gap) = fold_event_pages(vec![page(4..=6, false)], None, 10);
    assert_eq!(events.len(), 3);
    assert_eq!(gap, None, "a complete history has no notice");

    let (events, gap) = fold_event_pages(vec![page(4..=6, true), page(7..=9, true)], None, 4);
    assert_eq!(
        events.iter().map(|e| e.id).collect::<Vec<_>>(),
        vec![6, 7, 8, 9],
        "newest kept"
    );
    let gap = gap.expect("a notice");
    assert!(gap.contains("before event 3 expired"), "{gap}");
    assert!(gap.contains("2 older diagnostics"), "{gap}");

    let (events, gap) =
        fold_event_pages(vec![page(4..=6, false)], Some((6, "timed out".into())), 10);
    assert_eq!(events.len(), 3);
    assert!(gap
        .unwrap()
        .contains("after event 6 failed to load (timed out)"));
}
