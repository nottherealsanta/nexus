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
    assert!(text.contains("#1") && text.contains("hi"), "{text}");
    let folded = a
        .blocks()
        .iter()
        .find(|b| b.kind == nexus_tui::timeline::BlockKind::Turn)
        .unwrap();
    assert_eq!(
        folded.end - folded.start,
        1,
        "folded turn is a single filled row"
    );
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
    let group = a
        .blocks()
        .iter()
        .position(|b| b.kind == nexus_tui::timeline::BlockKind::Group)
        .expect("activity group");
    a.ui.timeline_sel = Some(group);
    a.execute("tool.expand_toggle");
    rows(&mut a);
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

#[test]
fn user_block_has_padding_and_rail_only_without_surface_colour() {
    use nexus_tui::timeline::{build, BlockKind};
    for tier in [Tier::TrueColor, Tier::Ansi256, Tier::Ansi16, Tier::Mono] {
        let app = with_turn(opts(Mode::Dark, tier), (120, 36));
        let timeline = build(&app, &app.theme, 0, 64);
        let block = timeline
            .blocks
            .iter()
            .find(|b| b.kind == BlockKind::Turn)
            .unwrap();
        assert_eq!(block.end - block.start, 3);
        assert_eq!(timeline.lines[block.start].spans[0].content, "▾");
        assert_eq!(timeline.lines[block.end - 1].spans[0].content, " ");
        let prefix = timeline.lines[block.start + 1].spans[0].content.as_ref();
        assert_eq!(
            prefix,
            if matches!(tier, Tier::Ansi16 | Tier::Mono) {
                "▌"
            } else {
                " "
            }
        );
    }
}

#[test]
fn hovering_user_block_reveals_copy_and_copies_that_message() {
    use nexus_tui::app::Effect;
    use nexus_tui::timeline::BlockKind;
    use nexus_tui::ui::Target;
    let mut a = app();
    rows(&mut a);
    let user = a
        .blocks()
        .iter()
        .position(|b| b.kind == BlockKind::Turn)
        .unwrap();
    let rect = a.hits.rect_of(&Target::Block(user)).unwrap();
    a.mouse = Some((rect.x + 2, rect.y));
    rows(&mut a);
    let copy = a
        .hits
        .rect_of(&Target::CopyBlock(user))
        .expect("hover reveals copy");
    a.effects.clear();
    click(&mut a, copy.x, copy.y);
    assert!(a
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Copy(text) if text == "hi")));
}

#[test]
fn activity_groups_respect_text_and_child_boundaries_and_preserve_details() {
    use nexus_tui::timeline::{build, BlockKind};
    let mut a = app();
    let msgs = serde_json::json!([
        {"id":"u", "session_id":SESSION, "seq":1, "created":1.0, "role":"user", "parts":[{"type":"text","text":"start"}]},
        {"id":"a", "session_id":SESSION, "seq":2, "created":2.0, "role":"assistant", "parts":[
            {"type":"tool_call","id":"c1","name":"read","arguments":"{\"path\":\"a\"}"},
            {"type":"reasoning","summary":"considering"},
            {"type":"tool_call","id":"c2","name":"bash","arguments":"{\"command\":\"date\"}"},
            {"type":"text","text":"middle"},
            {"type":"tool_call","id":"c3","name":"edit","arguments":"{}"},
            {"type":"tool_call","id":"child","name":"session","arguments":"{}"},
            {"type":"tool_call","id":"c4","name":"write","arguments":"{}"}
        ]},
        {"id":"t", "session_id":SESSION, "seq":3, "created":3.0, "role":"tool", "parts":[
            {"type":"tool_result","call_id":"c1","output":"one","is_error":false},
            {"type":"tool_result","call_id":"c2","output":"two","is_error":true},
            {"type":"tool_result","call_id":"c3","output":"three","is_error":false},
            {"type":"tool_result","call_id":"c4","output":"four","is_error":false}
        ]}
    ]);
    let view = a.state.model.selected.as_mut().unwrap();
    view.messages = serde_json::from_value(msgs).unwrap();
    view.events.clear();
    let timeline = build(&a, &a.theme, 0, 100);
    let groups: Vec<_> = timeline
        .blocks
        .iter()
        .filter(|b| b.kind == BlockKind::Group)
        .collect();
    assert_eq!(groups.len(), 3);
    let first = &timeline.lines[groups[0].start];
    let text = first
        .spans
        .iter()
        .map(|s| s.content.as_ref())
        .collect::<String>();
    assert!(
        text.contains("Read 1 file")
            && text.contains("Ran 1 command")
            && text.contains("Thought")
            && text.contains("1 failed"),
        "{text}"
    );
    assert!(timeline.blocks.iter().any(|b| b.kind == BlockKind::Child));
    assert!(!timeline.blocks.iter().any(|b| b.kind == BlockKind::Tool));
    a.ui.timeline_open.insert(groups[0].key.clone(), true);
    let expanded = build(&a, &a.theme, 0, 100);
    assert!(expanded.blocks.iter().any(|b| b.key == "tool:c1"));
    assert!(expanded.blocks.iter().any(|b| b.key == "reason:a:1"));
    assert!(expanded.blocks.iter().any(|b| b.key == "tool:c2"));
}

#[test]
fn waiting_groups_stay_open_and_live_groups_settle_without_changing_key() {
    use nexus_tui::timeline::{build, BlockKind};
    let mut a = app();
    let msgs = serde_json::json!([
        {"id":"u", "session_id":SESSION, "seq":1, "created":1.0, "role":"user", "parts":[{"type":"text","text":"start"}]},
        {"id":"a", "session_id":SESSION, "seq":2, "created":2.0, "role":"assistant", "parts":[{"type":"tool_call","id":"c1","name":"bash","arguments":"{\"command\":\"date\"}"}]}
    ]);
    let view = a.state.model.selected.as_mut().unwrap();
    view.messages = serde_json::from_value(msgs).unwrap();
    view.events.clear();
    busy(&mut a);
    let live = build(&a, &a.theme, 0, 100);
    let group = live
        .blocks
        .iter()
        .find(|b| b.kind == BlockKind::Group)
        .unwrap();
    let key = group.key.clone();
    assert_eq!(group.default_open, Some(true));
    assert!(live.blocks.iter().any(|b| b.key == "tool:c1"));
    assert_eq!(
        live.lines[group.start].spans.len(),
        2,
        "reduced motion stays static"
    );
    let mut options = a.theme.opts;
    options.reduced_motion = false;
    a.theme = a.theme.with(options);
    let frame0 = build(&a, &a.theme, 0, 100);
    let frame1 = build(&a, &a.theme, 1, 100);
    let index0 = frame0
        .blocks
        .iter()
        .find(|b| b.kind == BlockKind::Group)
        .unwrap()
        .start;
    let index1 = frame1
        .blocks
        .iter()
        .find(|b| b.kind == BlockKind::Group)
        .unwrap()
        .start;
    assert_ne!(
        frame0.lines[index0].spans[1].style, frame1.lines[index1].spans[1].style,
        "highlight moves between frames"
    );
    a.state.model.approvals.insert(
        "p".into(),
        serde_json::from_value(
            serde_json::json!({"id":"p", "session_id":SESSION, "call_id":"c1", "tool":"bash"}),
        )
        .unwrap(),
    );
    a.ui.timeline_open.insert(key.clone(), false);
    a.ui.timeline_open
        .insert(format!("turn:{SESSION}:0"), false);
    let waiting = build(&a, &a.theme, 0, 100);
    let group = waiting
        .blocks
        .iter()
        .find(|b| b.kind == BlockKind::Group)
        .unwrap();
    assert!(!group.toggle);
    assert!(waiting.blocks.iter().any(|b| b.key == "tool:c1"));
    a.state.model.approvals.clear();
    a.ui.timeline_open.remove(&format!("turn:{SESSION}:0"));
    a.state.model.running.clear();
    a.state.model.sessions.get_mut(SESSION).unwrap().status = Some("idle".into());
    a.state.model.selected.as_mut().unwrap().messages.push(serde_json::from_value(serde_json::json!({"id":"t", "session_id":SESSION, "seq":3, "created":3.0, "role":"tool", "parts":[{"type":"tool_result","call_id":"c1","output":"ok","is_error":false}]})).unwrap());
    let done = build(&a, &a.theme, 0, 100);
    let group = done
        .blocks
        .iter()
        .find(|b| b.kind == BlockKind::Group)
        .unwrap();
    assert_eq!(group.key, key);
    assert_eq!(group.default_open, Some(false));
    assert!(!done.blocks.iter().any(|b| b.kind == BlockKind::Tool));
}

#[test]
fn live_group_announces_clipping_and_explicit_expansion_restores_members() {
    use nexus_tui::timeline::{build, BlockKind};
    let mut a = app();
    let calls: Vec<_> = (0..7).map(|n| serde_json::json!({"type":"tool_call", "id":format!("c{n}"), "name":"read", "arguments":"{}"})).collect();
    a.state.model.selected.as_mut().unwrap().messages = serde_json::from_value(serde_json::json!([
        {"id":"u", "session_id":SESSION, "seq":1, "created":1.0, "role":"user", "parts":[{"type":"text","text":"start"}]},
        {"id":"a", "session_id":SESSION, "seq":2, "created":2.0, "role":"assistant", "parts":calls}
    ])).unwrap();
    a.state.model.selected.as_mut().unwrap().events.clear();
    busy(&mut a);
    let t = build(&a, &a.theme, 0, 100);
    let group = t
        .blocks
        .iter()
        .find(|b| b.kind == BlockKind::Group)
        .unwrap();
    assert_eq!(
        t.blocks
            .iter()
            .filter(|b| b.kind == BlockKind::Tool)
            .count(),
        5
    );
    let tool_rows: Vec<String> = t
        .blocks
        .iter()
        .filter(|b| b.kind == BlockKind::Tool)
        .map(|b| {
            t.lines[b.start]
                .spans
                .iter()
                .map(|s| s.content.as_ref())
                .collect()
        })
        .collect();
    assert!(tool_rows.first().unwrap().starts_with("├─ Read"));
    assert!(tool_rows.last().unwrap().starts_with("╰─ Read"));
    let tools: Vec<_> = t
        .blocks
        .iter()
        .filter(|b| b.kind == BlockKind::Tool)
        .collect();
    assert!(tools.windows(2).all(|pair| pair[0].end == pair[1].start));
    assert!(t
        .lines
        .iter()
        .flat_map(|l| &l.spans)
        .any(|s| s.content.contains("2 earlier")));
    a.ui.timeline_open.insert(group.key.clone(), true);
    let t = build(&a, &a.theme, 0, 100);
    assert_eq!(
        t.blocks
            .iter()
            .filter(|b| b.kind == BlockKind::Tool)
            .count(),
        7
    );
}

#[test]
fn denied_approval_is_labelled_separately_from_failure() {
    use nexus_tui::api::Event;
    use nexus_tui::timeline::{build, BlockKind};
    let mut a = app();
    let view = a.state.model.selected.as_mut().unwrap();
    view.messages = serde_json::from_value(serde_json::json!([
        {"id":"u", "session_id":SESSION, "seq":1, "created":1.0, "role":"user", "parts":[{"type":"text","text":"start"}]},
        {"id":"a", "session_id":SESSION, "seq":2, "created":2.0, "role":"assistant", "parts":[{"type":"tool_call","id":"c1","name":"bash","arguments":"{\"command\":\"date\"}"}]},
        {"id":"t", "session_id":SESSION, "seq":3, "created":3.0, "role":"tool", "parts":[{"type":"tool_result","call_id":"c1","output":"denied","is_error":true}]}
    ])).unwrap();
    view.events = [
        (
            10,
            "permission.asked",
            serde_json::json!({"id":"p1", "call_id":"c1"}),
        ),
        (
            11,
            "permission.replied",
            serde_json::json!({"id":"p1", "outcome":"denied"}),
        ),
    ]
    .into_iter()
    .map(|(id, kind, data)| Event {
        id,
        cursor: None,
        seq: None,
        time: id as f64,
        kind: kind.into(),
        session_id: Some(SESSION.into()),
        data,
    })
    .collect();
    let t = build(&a, &a.theme, 0, 100);
    let group = t
        .blocks
        .iter()
        .find(|b| b.kind == BlockKind::Group)
        .unwrap();
    let text: String = t.lines[group.start]
        .spans
        .iter()
        .map(|s| s.content.as_ref())
        .collect();
    assert!(
        text.contains("1 denied") && !text.contains("failed"),
        "{text}"
    );
    a.ui.timeline_open.insert(group.key.clone(), true);
    let t = build(&a, &a.theme, 0, 100);
    let tool = t.blocks.iter().find(|b| b.kind == BlockKind::Tool).unwrap();
    let text: String = t.lines[tool.start]
        .spans
        .iter()
        .map(|s| s.content.as_ref())
        .collect();
    assert!(
        text.contains("denied") && !text.contains("failed"),
        "{text}"
    );
}

#[test]
fn large_user_message_preview_expands_without_changing_context_or_turn_key() {
    use nexus_tui::app::Effect;
    use nexus_tui::timeline::{build, BlockKind};
    let mut a = app();
    let original = "word ".repeat(1100);
    let user = a
        .state
        .model
        .selected
        .as_mut()
        .unwrap()
        .messages
        .iter_mut()
        .find(|m| m.role == "user")
        .unwrap();
    user.parts = vec![serde_json::json!({"type":"text", "text": original})];
    let t = build(&a, &a.theme, 0, 64);
    let turn = t.blocks.iter().find(|b| b.kind == BlockKind::Turn).unwrap();
    let turn_key = turn.key.clone();
    assert!(
        turn.end - turn.start <= 8,
        "preview has at most six text rows and two padding rows"
    );
    let control = t.blocks.iter().find(|b| b.kind == BlockKind::User).unwrap();
    assert_eq!(control.default_open, Some(false));
    assert!(t.lines[control.start]
        .spans
        .iter()
        .any(|s| s.content.contains("sent whole")));
    a.ui.timeline_open.insert(control.key.clone(), true);
    let expanded = build(&a, &a.theme, 0, 64);
    let turn = expanded
        .blocks
        .iter()
        .find(|b| b.kind == BlockKind::Turn)
        .unwrap();
    assert_eq!(turn.key, turn_key);
    assert!(turn.end - turn.start > 8);
    assert_eq!(
        a.messages()
            .iter()
            .find(|m| m.role == "user")
            .unwrap()
            .parts[0]["text"],
        original
    );
    rows(&mut a);
    let i = a
        .blocks()
        .iter()
        .position(|b| b.kind == BlockKind::Turn)
        .unwrap();
    a.ui.timeline_sel = Some(i);
    a.effects.clear();
    a.execute("copy.user_message");
    assert!(a
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Copy(text) if text == &original)));
}

#[test]
fn assistant_and_individual_fence_copy_keep_full_source() {
    use nexus_tui::app::Effect;
    use nexus_tui::timeline::BlockKind;
    let mut a = app();
    let event = serde_json::json!({"id": 90, "type": "message.added", "session_id": SESSION,
        "cursor": "log_c915bfc9b6d826c4:90",
        "data": {"id": "msg_code", "session_id": SESSION, "seq": 90, "created": 1793113412.0,
                 "role": "assistant", "parts": [{"type": "text", "text": "Example:\n\n```python\nprint(42)\n```\n\n```sh\necho second\n```"}]}});
    if let Ok(nexus_tui::api::Frame::Event(event)) =
        nexus_tui::api::Frame::from_value("message.added", None, &event)
    {
        a.apply_sync(nexus_tui::sync::SyncMsg::Event(event));
    }
    rows(&mut a);
    a.ui.focus = Focus::Timeline;
    a.ui.timeline_sel = a.blocks().iter().position(|b| {
        b.kind == BlockKind::Assistant
            && b.target.as_deref().is_some_and(|s| s.contains("print(42)"))
    });
    let assistant = a.ui.timeline_sel;
    a.ui.timeline_sel = a
        .blocks()
        .iter()
        .position(|b| b.kind == BlockKind::Code && b.target.as_deref() == Some("print(42)"));
    a.effects.clear();
    a.run_command("copy.code_block");
    assert!(a
        .effects
        .iter()
        .any(|effect| matches!(effect, Effect::Copy(text) if text == "print(42)")));
    a.ui.timeline_sel = a
        .blocks()
        .iter()
        .position(|b| b.kind == BlockKind::Code && b.target.as_deref() == Some("echo second"));
    assert!(a.ui.timeline_sel.is_some());
    let second_fence = a.ui.timeline_sel.unwrap();
    a.click(nexus_tui::ui::Target::Block(second_fence));
    rows(&mut a);
    assert!(a
        .hits
        .rect_of(&nexus_tui::ui::Target::CopyBlock(second_fence))
        .is_some());
    a.effects.clear();
    a.run_command("copy.code_block");
    assert!(a
        .effects
        .iter()
        .any(|effect| matches!(effect, Effect::Copy(text) if text == "echo second")));
    a.ui.timeline_sel = assistant;
    a.effects.clear();
    a.run_command("copy.assistant_block");
    assert!(a.effects.iter().any(|effect| matches!(effect, Effect::Copy(text) if text.contains("Example:") && text.contains("print(42)"))));
}

#[test]
fn bracketed_user_prompt_counts_as_a_turn_for_context_folding() {
    let mut a = app();
    let selected = a.state.model.selected.as_mut().unwrap();
    selected.messages.retain(|message| message.role == "user");
    selected.messages.truncate(1);
    selected.messages[0].parts = vec![serde_json::json!({"type":"text", "text":"[bug] fix this"})];
    selected.starting = Some(
        serde_json::json!({"system_prompt":"prompt", "tools": [], "agent": {}, "instruction_file": {}}),
    );
    rows(&mut a);
    let index = a
        .blocks()
        .iter()
        .position(|block| block.kind == nexus_tui::timeline::BlockKind::Context)
        .unwrap();
    let key = a.blocks()[index].key.clone();
    a.click(nexus_tui::ui::Target::Block(index));
    assert_eq!(a.ui.timeline_open.get(&key), Some(&true));
}

#[test]
fn reasoning_reports_its_measured_duration() {
    use nexus_tui::api::Event;
    use nexus_tui::sync::SyncMsg;
    let mut a = app();
    let fields = |id: i64, time: f64, kind: &str, data: serde_json::Value| Event {
        id,
        cursor: None,
        seq: None,
        time,
        kind: kind.into(),
        session_id: Some(SESSION.into()),
        data,
    };
    a.apply_sync(SyncMsg::Event(fields(
        500,
        20.0,
        "message.added",
        serde_json::json!({
            "id": "msg_reason", "session_id": SESSION, "seq": 20, "created": 1793113500.0,
            "role": "assistant",
            "parts": [{"type": "reasoning", "summary": "Let me think."}]
        }),
    )));
    a.apply_sync(SyncMsg::Event(fields(
        501,
        20.5,
        "reasoning_delta",
        serde_json::json!({"text": "Let me think."}),
    )));
    a.apply_sync(SyncMsg::Event(fields(
        503,
        24.0,
        "part_done",
        serde_json::json!({"part": {"type": "reasoning", "summary": "Let me think."}}),
    )));
    let text = rows(&mut a).join("\n");
    assert!(text.contains("Thought for 3.5s"), "{text}");
}

#[test]
fn a_retry_sequence_is_one_tight_stack_with_rounded_delays() {
    let mut a = app();
    let mk = |id: u64, kind: &str, data: serde_json::Value| {
        serde_json::json!({"id": id, "type": kind, "session_id": SESSION,
            "cursor": format!("log_c915bfc9b6d826c4:{id}"), "data": data})
    };
    for v in [
        mk(
            400,
            "retry_scheduled",
            serde_json::json!({"attempt": 1, "max_attempts": 3,
            "delay": 0.8421336129301662, "reason": "codex: HTTP 429"}),
        ),
        mk(401, "retry_started", serde_json::json!({"attempt": 2})),
        mk(
            402,
            "session.error",
            serde_json::json!({"kind": "provider",
            "message": "codex: HTTP 429", "status": 429}),
        ),
    ] {
        if let Ok(nexus_tui::api::Frame::Event(e)) =
            nexus_tui::api::Frame::from_value(v["type"].as_str().unwrap(), None, &v)
        {
            a.apply_sync(nexus_tui::sync::SyncMsg::Event(e));
        }
    }
    let drawn = rows(&mut a);
    let text = drawn.join("\n");
    assert!(text.contains("provider retry 1/3 in 0.8s"), "{text}");
    // One line each, detail inline, no gap between them.
    let first = drawn.iter().position(|r| r.contains("retry 1/3")).unwrap();
    assert!(drawn[first].contains("codex: HTTP 429"), "{text}");
    assert!(drawn[first + 1].contains("retry started"), "no gap: {text}");
    let err = drawn
        .iter()
        .position(|r| r.contains("provider: codex: HTTP 429"))
        .unwrap();
    assert_eq!(drawn[err].matches("HTTP 429").count(), 1, "{text}");
}

/// Replace the viewed session's messages with one assistant turn of tool calls,
/// each `(name, result output)`; a `None` output leaves the call running.
fn calls(a: &mut nexus_tui::app::App, tools: &[(&str, Option<&str>)]) {
    let parts: Vec<_> = tools
        .iter()
        .enumerate()
        .map(|(n, (name, _))| serde_json::json!({"type":"tool_call", "id":format!("c{n}"), "name":name, "arguments":"{}"}))
        .collect();
    let results: Vec<_> = tools
        .iter()
        .enumerate()
        .filter_map(|(n, (_, out))| {
            out.map(|o| serde_json::json!({"type":"tool_result", "call_id":format!("c{n}"), "output":o, "is_error":false}))
        })
        .collect();
    let view = a.state.model.selected.as_mut().unwrap();
    view.messages = serde_json::from_value(serde_json::json!([
        {"id":"u", "session_id":SESSION, "seq":1, "created":1.0, "role":"user", "parts":[{"type":"text","text":"go"}]},
        {"id":"a", "session_id":SESSION, "seq":2, "created":2.0, "role":"assistant", "parts":parts},
        {"id":"t", "session_id":SESSION, "seq":3, "created":3.0, "role":"tool", "parts":results}
    ]))
    .unwrap();
    view.events.clear();
}

#[test]
fn verb_groups_cover_every_tool_family_and_name_unknown_tools() {
    let mut a = app();
    let ok = Some("ok");
    for (tools, want) in [
        (
            vec![("web_search", ok), ("web_search", ok)],
            "Searched 2 times",
        ),
        (vec![("web_fetch", ok); 3], "Fetched 3 pages"),
        (vec![("ask", ok)], "Asked 1 question"),
        (vec![("code", ok), ("code", ok)], "Ran 2 cells"),
    ] {
        calls(&mut a, &tools);
        let text = rows(&mut a).join("\n");
        assert!(text.contains(want), "{want}: {text}");
    }
    calls(&mut a, &[("mcp_weather", ok), ("mcp_weather", ok)]);
    let text = rows(&mut a).join("\n");
    assert!(text.contains("Used mcp_weather"), "{text}");
    assert!(!text.contains("Used tool"), "{text}");
    calls(
        &mut a,
        &[("mcp_weather", ok), ("skill_x", ok), ("skill_x", ok)],
    );
    let text = rows(&mut a).join("\n");
    assert!(text.contains("Used 3 tools"), "{text}");
}

#[test]
fn working_shimmer_peaks_in_the_agent_colour() {
    use nexus_tui::timeline::{build, BlockKind};
    let mut a = app();
    calls(&mut a, &[("bash", None)]);
    busy(&mut a);
    let mut options = a.theme.opts;
    options.reduced_motion = false;
    a.theme = a.theme.with(options);
    let agent = a.viewed().map(|s| s.agent.clone()).unwrap_or_default();
    let peak = a.theme.agent_style(&agent).fg;
    assert_ne!(peak, a.theme.fg("text.primary").fg, "distinct peak colour");
    let seen = (0..54).any(|frame| {
        let t = build(&a, &a.theme, frame, 100);
        let group = t
            .blocks
            .iter()
            .find(|b| b.kind == BlockKind::Group)
            .unwrap();
        t.lines[group.start]
            .spans
            .iter()
            .any(|s| s.style.fg == peak)
    });
    assert!(seen, "the highlight reaches the agent colour");
    // Reduced motion stays static.
    options.reduced_motion = true;
    a.theme = a.theme.with(options);
    let t = build(&a, &a.theme, 10, 100);
    let group = t
        .blocks
        .iter()
        .find(|b| b.kind == BlockKind::Group)
        .unwrap();
    assert_eq!(t.lines[group.start].spans.len(), 2);
}

#[test]
fn collapsed_bash_rows_show_a_non_zero_exit_code() {
    let mut a = app();
    // The second call keeps the group live, so member rows are visible.
    calls(
        &mut a,
        &[
            ("bash", Some("[exit code 2]\nboom")),
            ("bash", Some("[cwd: /w]\n[exit code 0]\nfine")),
            ("read", None),
        ],
    );
    busy(&mut a);
    let text = rows(&mut a).join("\n");
    assert!(text.contains("exit 2"), "{text}");
    assert_eq!(
        text.matches("exit ").count(),
        1,
        "zero exit stays quiet: {text}"
    );
}

fn starting_context() -> serde_json::Value {
    serde_json::json!({
        "origin": "created",
        "system_prompt": format!("You are a coding agent.\n{}", "x".repeat(4000)),
        "tools": ["read", "bash", "edit"],
        "tool_definitions": {"read": {"name": "read", "parameters": {"type": "object"}}},
        "instruction_file": {"path": "/work/AGENTS.md", "status": "loaded", "bytes": 800},
        "agent": {"name": "build", "source": "builtin", "description": "Does the work."},
        "child_agents": [{"name": "scout"}, {"name": "plan"}],
        "workspace": {"active": false}
    })
}

#[test]
fn the_context_section_lists_items_with_estimates_and_the_window_share() {
    let mut a = empty(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    a.open_session(SESSION);
    a.state.model.selected.as_mut().unwrap().starting = Some(starting_context());
    a.state.models = vec![serde_json::json!({"ref": "codex/gpt-5", "context": 100000})];
    a.state.model.sessions.get_mut(SESSION).unwrap().model = Some("codex/gpt-5".into());
    let text = rows(&mut a).join("\n");
    for needle in [
        "Context",
        "System prompt",
        "You are a coding agent.",
        "Tools [3]",
        "read  bash  edit",
        "Instructions AGENTS.md loaded",
        "/work/AGENTS.md",
        "part of the system prompt",
        "Agent build",
        "Child agents [2]",
        "scout  plan",
        "of the window",
    ] {
        assert!(text.contains(needle), "missing {needle:?}:\n{text}");
    }
    // The system prompt is ~4,024 characters, so about a thousand tokens.
    assert!(text.contains("~1.0K"), "{text}");
}

#[test]
fn the_turn_footer_prices_a_turn_from_the_catalog() {
    use nexus_tui::api::Event;
    let mut a = app();
    a.state.model.sessions.get_mut(SESSION).unwrap().model = Some("codex/gpt-5".into());
    a.state.models = vec![serde_json::json!({
        "ref": "codex/gpt-5", "context": 400000,
        "input_cost": 1.0, "output_cost": 10.0, "cache_read_cost": 0.2
    })];
    let first = a.messages()[0].created;
    let events = &mut a.state.model.selected.as_mut().unwrap().events;
    events.push(Event {
        id: 900,
        cursor: None,
        seq: None,
        time: first + 0.1,
        kind: "wire_request".into(),
        session_id: Some(SESSION.into()),
        data: serde_json::json!({"body": {"model": "gpt-5"}}),
    });
    events.push(Event {
        id: 901,
        cursor: None,
        seq: None,
        time: first + 0.2,
        kind: "usage".into(),
        session_id: Some(SESSION.into()),
        data: serde_json::json!({
            "input_tokens": 100000, "cached_input_tokens": 50000, "output_tokens": 10000
        }),
    });
    let text = rows(&mut a).join("\n");
    // 50K fresh input at $1, 50K cached at $0.20, 10K output at $10 per million.
    assert!(text.contains("100K in 50.0K cached 10.0K out"), "{text}");
    assert!(text.contains("$0.16"), "{text}");
}
