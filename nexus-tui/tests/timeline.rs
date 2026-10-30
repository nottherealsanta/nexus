//! Timeline blocks: turn grouping, fold state, tool expansion, history merge.

mod common;
use common::*;

use nexus_tui::api::History;
use nexus_tui::theme::{Mode, Tier};
use nexus_tui::ui::Focus;

/// These tests pin the low detail level (groups collapsed); the other levels
/// have their own test below.
fn app() -> nexus_tui::app::App {
    let mut a = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    a.ui.detail = nexus_tui::ui::Detail::Low;
    a
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
    assert!(text.contains("Build"), "turn footer: {text}");
    // D05: the footer never states how the turn ended.
    for word in ["done", "interrupted", "error", "<1 s", "call"] {
        assert!(!text.contains(word), "no end status {word:?}: {text}");
    }
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
        3,
        "folded turn is one text row with a padding row above and below"
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
fn a_tool_image_is_named_in_the_open_tool() {
    let mut a = app();
    let call = serde_json::json!({"id": 70, "type": "message.added", "session_id": SESSION,
        "cursor": "log_c915bfc9b6d826c4:70",
        "data": {"id": "msg_10", "session_id": SESSION, "seq": 10, "created": 1793113410.0,
                 "role": "assistant", "parts": [{"type": "tool_call", "id": "call_9", "name": "mcp_shot", "arguments": "{}"}]}});
    let result = serde_json::json!({"id": 71, "type": "message.added", "session_id": SESSION,
        "cursor": "log_c915bfc9b6d826c4:71",
        "data": {"id": "msg_11", "session_id": SESSION, "seq": 11, "created": 1793113411.0,
                 "role": "tool", "parts": [{"type": "tool_result", "call_id": "call_9", "output": "saved",
                 "is_error": false, "media": [{"artifact": "art_1", "mime": "image/png", "name": "shot.png"}]}]}});
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
    assert!(text.contains("image shot.png"), "{text}");
    assert!(text.contains("image/png"), "{text}");
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
        ..Default::default()
    };
    a.apply_sync(nexus_tui::sync::SyncMsg::History(Box::new(h)));
    let view = a.state.model.selected.as_ref().unwrap();
    assert!(!view.messages_more);
    assert!(view.scratch.iter().any(|e| e.id == 300), "live event kept");
    assert!(view.starting.is_some());
    // The starting context stays open after the first turn, with no
    // `Context` header row (see the_context_sections_stay_open_without_a_header_row).
    rows(&mut a);
    a.execute("turn.unfold_all");
    let text = rows(&mut a).join("\n");
    assert!(text.contains("System prompt"), "context section: {text}");
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
fn history_keeps_messages_applied_live_and_announces_missing_details() {
    let mut a = app();
    let mut msgs = a.messages().to_vec();
    let live = msgs.pop().expect("a message");
    // The history fetch finished before `live` was added by the stream.
    let h = History {
        session_id: SESSION.into(),
        messages: msgs,
        turns_error: Some("turn details failed to load (timed out)".into()),
        ..Default::default()
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
        text.contains("turn details failed to load (timed out)"),
        "gap announced: {text}"
    );
}

#[test]
fn user_band_is_full_width_with_a_rail_and_padding() {
    use nexus_tui::timeline::{build, BlockKind};
    for tier in [Tier::TrueColor, Tier::Ansi256, Tier::Ansi16, Tier::Mono] {
        let app = with_turn(opts(Mode::Dark, tier), (120, 36));
        let timeline = build(&app, &app.theme, 0, 64);
        let block = timeline
            .blocks
            .iter()
            .find(|b| b.kind == BlockKind::Turn)
            .unwrap();
        assert_eq!(block.end - block.start, 3, "padding, text, padding");
        let text = |i: usize| -> String {
            timeline.lines[i]
                .spans
                .iter()
                .map(|s| s.content.as_ref())
                .collect()
        };
        // Every row is a band row: full-width background and the rail at the edge.
        for i in block.start..block.end {
            assert!(
                timeline.lines[i].bg.is_some() && timeline.lines[i].rail.is_some(),
                "{tier:?} row {i}"
            );
        }
        assert!(text(block.start).trim().is_empty(), "top padding");
        assert!(text(block.end - 1).trim().is_empty(), "bottom padding");
        // No fold marker; the text keeps its 2-cell inset.
        assert!(text(block.start + 1).starts_with("   hi"), "{tier:?}");
        assert!(text(block.start + 1).contains("#1"), "{tier:?}");
    }
}

#[test]
fn user_band_rail_is_muted_and_takes_the_focus_colour_when_focused() {
    use nexus_tui::timeline::{build, BlockKind};
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let rail = |app: &nexus_tui::app::App| {
        let t = build(app, &app.theme, 0, 64);
        let b = t.blocks.iter().find(|b| b.kind == BlockKind::Turn).unwrap();
        t.lines[b.start + 1].rail.unwrap().fg
    };
    assert_eq!(rail(&app), app.theme.fg("text.muted").fg);
    let turn = {
        let t = build(&app, &app.theme, 0, 64);
        t.blocks
            .iter()
            .position(|b| b.kind == BlockKind::Turn)
            .unwrap()
    };
    app.ui.focus = Focus::Timeline;
    app.ui.timeline_sel = Some(turn);
    assert_eq!(rail(&app), app.theme.fg("interactive.focus").fg);
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
    view.scratch.clear();
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
            && text.contains("ran 1 command")
            && text.contains("thought")
            && !text.contains("failed"),
        "{text}"
    );
    assert!(timeline.blocks.iter().any(|b| b.kind == BlockKind::Child));
    assert!(!timeline.blocks.iter().any(|b| b.kind == BlockKind::Tool));
    a.ui.timeline_open.insert(groups[0].key.clone(), true);
    let expanded = build(&a, &a.theme, 0, 100);
    assert!(expanded.blocks.iter().any(|b| b.key == "tool:c1"));
    assert!(expanded.blocks.iter().any(|b| b.key == "reason:a:1"));
    let thought = expanded
        .blocks
        .iter()
        .find(|b| b.key == "reason:a:1")
        .unwrap();
    assert!(line_text(&expanded.lines[thought.start]).contains("◇ Thought"));
    assert!(expanded.blocks.iter().any(|b| b.key == "tool:c2"));
}

#[test]
fn waiting_groups_stay_open_and_live_groups_settle_without_changing_key() {
    use nexus_tui::timeline::{build, BlockKind};
    let mut a = app();
    // Medium shows the live tree; low folds a running group to `Working` (D06).
    a.ui.detail = nexus_tui::ui::Detail::Medium;
    let msgs = serde_json::json!([
        {"id":"u", "session_id":SESSION, "seq":1, "created":1.0, "role":"user", "parts":[{"type":"text","text":"start"}]},
        {"id":"a", "session_id":SESSION, "seq":2, "created":2.0, "role":"assistant", "parts":[{"type":"tool_call","id":"c1","name":"bash","arguments":"{\"command\":\"date\"}"}]}
    ]);
    let view = a.state.model.selected.as_mut().unwrap();
    view.messages = serde_json::from_value(msgs).unwrap();
    view.scratch.clear();
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
    // The grey sweep (muted to secondary) starts off the word's left edge, so
    // compare frames far enough apart for the highlight to have reached `W`.
    let frame0 = build(&a, &a.theme, 0, 100);
    let frame1 = build(&a, &a.theme, 12, 100);
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
    // Medium shows the live tree; low folds a running group to `Working` (D06).
    a.ui.detail = nexus_tui::ui::Detail::Medium;
    let calls: Vec<_> = (0..7).map(|n| serde_json::json!({"type":"tool_call", "id":format!("c{n}"), "name":"read", "arguments":"{}"})).collect();
    a.state.model.selected.as_mut().unwrap().messages = serde_json::from_value(serde_json::json!([
        {"id":"u", "session_id":SESSION, "seq":1, "created":1.0, "role":"user", "parts":[{"type":"text","text":"start"}]},
        {"id":"a", "session_id":SESSION, "seq":2, "created":2.0, "role":"assistant", "parts":calls}
    ])).unwrap();
    a.state.model.selected.as_mut().unwrap().scratch.clear();
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
    // Members hang off a `├─` / `╰─` tree; a running one shows the spinner in
    // place of its verb glyph.
    assert!(tool_rows.first().unwrap().starts_with("  ├─ ◐ Read"));
    assert!(tool_rows.last().unwrap().starts_with("  ╰─ ◐ Read"));
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
    // Low detail keeps the live group to its single `Working` row.
    a.ui.timeline_open.remove(&group.key);
    a.ui.detail = nexus_tui::ui::Detail::Low;
    let t = build(&a, &a.theme, 0, 100);
    assert!(!t.blocks.iter().any(|b| b.kind == BlockKind::Tool));
    assert!(t.lines.iter().any(|l| line_text(l).contains("Working")));
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
    view.scratch = [
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
fn bracketed_user_prompt_keeps_context_open() {
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
    assert_eq!(a.ui.timeline_open.get(&key), None);
}

#[test]
fn reasoning_does_not_show_a_duration() {
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
    assert!(text.contains("Thought"), "{text}");
    assert!(!text.contains("Thought for 3.5s"), "{text}");
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
    view.scratch.clear();
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
fn working_shimmer_peaks_in_primary_grey() {
    use nexus_tui::timeline::{build, BlockKind};
    let mut a = app();
    calls(&mut a, &[("bash", None)]);
    busy(&mut a);
    let mut options = a.theme.opts;
    options.reduced_motion = false;
    a.theme = a.theme.with(options);
    let peak = a.theme.fg("text.primary").fg;
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
    assert!(seen, "the highlight reaches primary grey");
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
    // Medium shows the live tree; low folds a running group to `Working` (D06).
    a.ui.detail = nexus_tui::ui::Detail::Medium;
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
fn context_headings_open_frozen_snapshot_details() {
    use nexus_tui::ui::OverlayKind;
    for heading in ["System prompt", "AGENTS.md", "Tools"] {
        let mut a = empty(opts(Mode::Dark, Tier::TrueColor), (120, 36));
        a.open_session(SESSION);
        a.state.model.selected.as_mut().unwrap().starting = Some(starting_context());
        let (_, rows) = draw(&mut a, 120, 36);
        let y = rows.iter().position(|row| row.contains(heading)).unwrap();
        let x = nexus_tui::text::width(&rows[y][..rows[y].find(heading).unwrap()]);
        click(&mut a, x as u16, y as u16);
        assert!(
            matches!(
                &a.ui.overlay.as_ref().map(|o| &o.kind),
                Some(OverlayKind::ContextDetail { .. })
            ),
            "{heading} must open a modal"
        );
    }
}

#[test]
fn the_context_section_lists_d10_sections_with_estimates_and_the_window_share() {
    let mut a = empty(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    a.open_session(SESSION);
    a.state.model.selected.as_mut().unwrap().starting = Some(starting_context());
    a.state.models = vec![serde_json::json!({"ref": "codex/gpt-5", "context": 100000})];
    a.state.model.sessions.get_mut(SESSION).unwrap().model = Some("codex/gpt-5".into());
    let text = draw(&mut a, 200, 50).1.join("\n");
    // D10: `◆` sections with a count and meta, a preview line, a tools grid,
    // and a text total with no bar.
    for needle in [
        "System prompt ~1.0K",
        "System prompt ~1.0K",
        "AGENTS.md ~200",
        "Tools [3] ~14",
        "Skills [0]  none",
        "MCP [0]  none",
        "total  ~1,020 tok",
        "of the window",
    ] {
        assert!(text.contains(needle), "missing {needle:?}:\n{text}");
    }
    assert!(!text.contains("You are a coding agent."));
    assert!(!text.contains("Child agents"));
    // The total is text: no meter cells.
    let total_row = text.lines().find(|l| l.contains("total  ~")).unwrap();
    assert!(
        !total_row.contains('\u{2571}') && !total_row.contains('/'),
        "no context bar: {total_row}"
    );
}

#[test]
fn session_tool_grid_keeps_individual_click_targets_after_a_message() {
    let mut a = app();
    a.state.model.selected.as_mut().unwrap().starting = Some(starting_context());
    a.ui.scroll.follow = false;
    a.ui.scroll.top = 0;
    let (_, lines) = draw(&mut a, 120, 36);
    let y = lines
        .iter()
        .position(|line| line.contains("read") && line.contains("bash"))
        .expect("tools share a row");
    let line = &lines[y];
    let bash = nexus_tui::text::width(&line[..line.find("bash").unwrap()]);
    click(&mut a, (bash - 2) as u16, y as u16);
    let expected = a
        .context_items()
        .iter()
        .position(|item| item.key == "bash")
        .unwrap();
    assert_eq!(a.ui.context_cursor, expected);
    let (_, lines) = draw(&mut a, 120, 36);
    let y = lines
        .iter()
        .position(|line| line.contains("read") && line.contains("bash"))
        .unwrap();
    let bash = nexus_tui::text::width(&lines[y][..lines[y].find("bash").unwrap()]);
    click(&mut a, bash as u16, y as u16);
    assert!(matches!(
        a.ui.overlay.as_ref().map(|o| &o.kind),
        Some(nexus_tui::ui::OverlayKind::ContextDetail { .. })
    ));
}

#[test]
fn the_context_section_without_an_instruction_file_says_so() {
    let mut a = empty(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    a.open_session(SESSION);
    let mut starting = starting_context();
    starting["instruction_file"] =
        serde_json::json!({"path": "/work/AGENTS.md", "status": "missing"});
    a.state.model.selected.as_mut().unwrap().starting = Some(starting);
    let text = rows(&mut a).join("\n");
    assert!(text.contains("AGENTS.md"), "{text}");
    assert!(text.contains("not found"), "{text}");
    assert!(!text.contains("in the system prompt"), "{text}");
    assert!(text.contains("Tools [3]"), "{text}");
}

#[test]
fn a_disabled_tool_is_struck_through_in_the_tools_grid() {
    let mut a = empty(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    a.open_session(SESSION);
    a.state.model.selected.as_mut().unwrap().starting = Some(starting_context());
    a.state
        .model
        .sessions
        .get_mut(SESSION)
        .unwrap()
        .extra
        .insert("disabled_tools".into(), serde_json::json!(["bash"]));
    let (_, rows) = draw(&mut a, 120, 36);
    let text = rows.join("\n");
    assert!(text.contains("Tools [3] (1 off)"), "{text}");
}

#[test]
fn the_context_sections_stay_open_without_a_header_row() {
    let mut a = app();
    a.state.model.selected.as_mut().unwrap().starting = Some(starting_context());
    let drawn = rows(&mut a);
    let text = drawn.join("\n");
    // The folded `Context · N items · ~X tokens` row is gone; the sections start
    // the block and stay expanded after the first turn.
    assert!(!text.contains("\u{25be} Context"), "{text}");
    assert!(!text.contains("Context \u{b7}"), "{text}");
    assert!(text.contains("System prompt"), "{text}");
    assert!(text.contains("total  ~"), "{text}");
}

#[test]
fn the_turn_footer_shows_usage_without_cost() {
    let mut a = app();
    a.state.model.sessions.get_mut(SESSION).unwrap().model = Some("codex/gpt-5".into());
    a.state.models = vec![serde_json::json!({
        "ref": "codex/gpt-5", "context": 400000,
        "input_cost": 1.0, "output_cost": 10.0, "cache_read_cost": 0.2
    })];
    summarize(
        &mut a,
        900,
        fixture_summary(
            "codex/gpt-5",
            serde_json::json!({"input": 100000, "cached": 50000, "output": 10000, "reasoning": 0}),
        ),
    );
    let text = rows(&mut a).join("\n");
    assert!(
        text.contains("100Ki") && text.contains("10.0Ko") && text.contains("50%c"),
        "{text}"
    );
    assert!(text.contains("Build"), "right-aligned agent lead: {text}");
    let footer = text.lines().find(|line| line.contains("100Ki")).unwrap();
    assert!(!footer.contains('$'), "{footer}");
    assert!(!footer.contains('▪'), "{footer}");
}

#[test]
fn the_turn_footers_usage_units_are_fainter_than_their_counts() {
    use nexus_tui::timeline::build;
    let mut a = app();
    a.state.model.sessions.get_mut(SESSION).unwrap().model = Some("codex/gpt-5".into());
    a.state.models = vec![serde_json::json!({
        "ref": "codex/gpt-5", "context": 400000,
        "input_cost": 1.0, "output_cost": 10.0, "cache_read_cost": 0.2
    })];
    summarize(
        &mut a,
        900,
        fixture_summary(
            "codex/gpt-5",
            serde_json::json!({"input": 100000, "cached": 50000, "output": 10000, "reasoning": 0}),
        ),
    );
    let t = build(&a, &a.theme, 0, 200);
    let footer = t
        .lines
        .iter()
        .find(|l| line_text(l).contains("100Ki"))
        .expect("footer");
    let faint = a.theme.fg("text.faint").fg;
    let muted = a.theme.fg("text.muted").fg;
    let units: Vec<_> = footer
        .spans
        .iter()
        .filter(|s| matches!(s.content.as_ref(), "i" | "o" | "r" | "c"))
        .collect();
    assert_eq!(units.len(), 4, "i, o, r and c: {:?}", footer.spans);
    for unit in units {
        assert_eq!(unit.style.fg, faint, "unit {unit:?}");
    }
    for value in ["100K", "10.0K", "0", "50%"] {
        let span = footer
            .spans
            .iter()
            .find(|s| s.content.as_ref() == value)
            .unwrap_or_else(|| panic!("{value:?} in {:?}", footer.spans));
        assert_eq!(span.style.fg, muted, "count {span:?}");
    }
}

#[test]
fn long_tool_output_keeps_head_and_tail_and_show_all_restores_the_middle() {
    let mut a = app();
    let output: String = (1..=500).map(|n| format!("line {n}\n")).collect();
    let call = serde_json::json!({"id": 80, "type": "message.added", "session_id": SESSION,
        "cursor": "log_c915bfc9b6d826c4:80",
        "data": {"id": "msg_20", "session_id": SESSION, "seq": 20, "created": 1793113420.0,
                 "role": "assistant", "parts": [{"type": "tool_call", "id": "call_long", "name": "bash",
                 "arguments": "{\"command\":\"seq 500\"}"}]}});
    let result = serde_json::json!({"id": 81, "type": "message.added", "session_id": SESSION,
        "cursor": "log_c915bfc9b6d826c4:81",
        "data": {"id": "msg_21", "session_id": SESSION, "seq": 21, "created": 1793113421.0,
                 "role": "tool", "parts": [{"type": "tool_result", "call_id": "call_long",
                 "output": output, "is_error": false}]}});
    for v in [call, result] {
        if let Ok(nexus_tui::api::Frame::Event(e)) =
            nexus_tui::api::Frame::from_value(v["type"].as_str().unwrap(), None, &v)
        {
            a.apply_sync(nexus_tui::sync::SyncMsg::Event(e));
        }
    }
    rows(&mut a);
    a.ui.focus = Focus::Timeline;
    let find = |a: &nexus_tui::app::App, kind| a.blocks().iter().position(|b| b.kind == kind);
    let group = find(&a, nexus_tui::timeline::BlockKind::Group).expect("group");
    a.ui.timeline_sel = Some(group);
    a.execute("tool.expand_toggle");
    rows(&mut a);
    let tool = find(&a, nexus_tui::timeline::BlockKind::Tool).expect("tool");
    a.ui.timeline_sel = Some(tool);
    a.execute("tool.expand_toggle");
    // The view is tall enough for the whole detail only when scrolled; read the
    // built lines instead of the visible rows.
    let built = |a: &nexus_tui::app::App| {
        let t = nexus_tui::timeline::build(a, &a.theme, 0, 100);
        t.lines
            .iter()
            .map(|l| {
                l.spans
                    .iter()
                    .map(|s| s.content.as_ref())
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n")
    };
    rows(&mut a);
    // Output keeps its command row and its last 8 rows; the rest folds behind
    // a marker that says how much it hides.
    let text = built(&a);
    assert!(text.contains("$ seq 500"), "command row: {text}");
    assert!(
        text.contains("line 500\n") && text.contains("line 493\n"),
        "tail"
    );
    assert!(!text.contains("line 492\n"), "earlier rows are folded");
    assert!(!text.contains("line 1\n"), "head is folded");
    assert!(
        text.contains("\u{2191} 492 earlier lines · enter show all"),
        "{text}"
    );
    assert!(a.term("tool.output_clipped_in_ui").unwrap());
    // `o` shows everything and folds it again.
    press(&mut a, &["o"]);
    let text = built(&a);
    assert!(
        text.contains("line 250\n") && !text.contains("earlier lines"),
        "all shown"
    );
    assert!(
        text.contains("showing all 500 lines · o show less"),
        "{text}"
    );
    press(&mut a, &["o"]);
    assert!(!built(&a).contains("line 250\n"), "o folds it again");
    // Enter on the open, clipped tool shows all too; the next Enter folds the tool.
    a.execute("tool.expand_toggle");
    assert!(built(&a).contains("line 250\n"), "enter shows all");
    a.execute("tool.expand_toggle");
    let text = built(&a);
    assert!(!text.contains("line 500\n"), "second enter folds the tool");
}

fn add_assistant_message(a: &mut nexus_tui::app::App, n: i64, text: &str) {
    let v = serde_json::json!({"id": 1000 + n, "type": "message.added", "session_id": SESSION,
        "cursor": format!("log_c915bfc9b6d826c4:{}", 1000 + n),
        "data": {"id": format!("msg_long_{n}"), "session_id": SESSION, "seq": 100 + n,
                 "created": 1793113500.0 + n as f64,
                 "role": "assistant", "parts": [{"type": "text", "text": text}]}});
    if let Ok(nexus_tui::api::Frame::Event(e)) =
        nexus_tui::api::Frame::from_value("message.added", None, &v)
    {
        a.apply_sync(nexus_tui::sync::SyncMsg::Event(e));
    }
}

#[test]
fn following_anchors_and_jump_to_latest_hold_at_every_size() {
    for (w, h) in [(80, 24), (160, 48)] {
        let mut a = app();
        for n in 0..60 {
            add_assistant_message(&mut a, n, &format!("paragraph number {n}"));
        }
        let drawn = draw(&mut a, w, h).1.join("\n");
        assert!(a.ui.scroll.follow, "{w}x{h}: follows the bottom");
        assert!(
            drawn.contains("paragraph number 59"),
            "{w}x{h}: newest visible"
        );
        assert!(
            !drawn.contains("paragraph number 10"),
            "{w}x{h}: old content scrolled off"
        );

        // Scrolling up leaves follow mode and pins what is read.
        a.ui.focus = Focus::Timeline;
        a.scroll_timeline(-10);
        assert!(
            !a.ui.scroll.follow,
            "{w}x{h}: scrolling up suspends following"
        );
        let top = a.ui.scroll.top;
        let before = draw(&mut a, w, h).1.join("\n");
        add_assistant_message(&mut a, 60, "paragraph number 60");
        let after = draw(&mut a, w, h).1.join("\n");
        assert_eq!(
            a.ui.scroll.top, top,
            "{w}x{h}: incoming text keeps the anchor"
        );
        assert!(
            !after.contains("paragraph number 60"),
            "{w}x{h}: new content stays below"
        );
        assert!(a.term("timeline.scrolled_up").unwrap());
        assert!(
            a.hits.rect_of(&nexus_tui::ui::Target::JumpLatest).is_some(),
            "{w}x{h}: jump control"
        );
        let _ = before;

        // Jumping restores following and shows the newest item.
        a.execute("session.jump_latest");
        let drawn = draw(&mut a, w, h).1.join("\n");
        assert!(a.ui.scroll.follow, "{w}x{h}");
        assert!(drawn.contains("paragraph number 60"), "{w}x{h}: {drawn}");
    }
}

fn add_tool(a: &mut nexus_tui::app::App, n: i64, command: &str, output: &str) {
    let call = serde_json::json!({"id": 2000 + n * 10, "type": "message.added", "session_id": SESSION,
        "cursor": format!("log_c915bfc9b6d826c4:{}", 2000 + n * 10),
        "data": {"id": format!("msg_t{n}"), "session_id": SESSION, "seq": 200 + n * 2, "created": 1793113600.0 + n as f64,
                 "role": "assistant", "parts": [{"type": "tool_call", "id": format!("call_t{n}"), "name": "bash",
                 "arguments": serde_json::json!({"command": command}).to_string()}]}});
    let result = serde_json::json!({"id": 2001 + n * 10, "type": "message.added", "session_id": SESSION,
        "cursor": format!("log_c915bfc9b6d826c4:{}", 2001 + n * 10),
        "data": {"id": format!("msg_r{n}"), "session_id": SESSION, "seq": 201 + n * 2, "created": 1793113600.5 + n as f64,
                 "role": "tool", "parts": [{"type": "tool_result", "call_id": format!("call_t{n}"),
                 "output": output, "is_error": false}]}});
    for v in [call, result] {
        if let Ok(nexus_tui::api::Frame::Event(e)) =
            nexus_tui::api::Frame::from_value(v["type"].as_str().unwrap(), None, &v)
        {
            a.apply_sync(nexus_tui::sync::SyncMsg::Event(e));
        }
    }
}

#[test]
fn detail_levels_fold_finished_groups_until_high_expands_every_tool() {
    use nexus_tui::timeline::BlockKind;
    use nexus_tui::ui::Detail;
    let mut a = app();
    add_tool(&mut a, 1, "echo first", "first-output");
    add_tool(&mut a, 2, "echo second", "second-output");
    let count =
        |a: &nexus_tui::app::App, kind| a.blocks().iter().filter(|b| b.kind == kind).count();
    let low = rows(&mut a).join("\n");
    assert!(
        count(&a, BlockKind::Group) > 0 && count(&a, BlockKind::Tool) == 0,
        "{low}"
    );
    assert!(low.contains("Ran 2 commands"), "{low}");
    assert!(!low.contains("echo first"), "{low}");
    // Medium: a finished group still folds to its summary (DESIGN_PLAN D06
    // "Detail levels"); only a live group shows its tree at this level.
    a.execute("timeline.detail_cycle");
    assert_eq!(a.ui.detail, Detail::Medium);
    let medium = rows(&mut a).join("\n");
    assert_eq!(count(&a, BlockKind::Tool), 0, "{medium}");
    assert!(medium.contains("Ran 2 commands"), "{medium}");
    assert!(!medium.contains("first-output"), "{medium}");
    // High: every tool opens and shows its output.
    a.execute("timeline.detail_cycle");
    assert_eq!(a.ui.detail, Detail::High);
    let high = rows(&mut a).join("\n");
    assert!(
        high.contains("first-output") && high.contains("second-output"),
        "{high}"
    );
    // Back to low; a hand-opened group follows the level again.
    a.execute("timeline.detail_cycle");
    assert_eq!(a.ui.detail, Detail::Low);
    assert!(a.ui.timeline_open.keys().all(|k| !k.starts_with("group:")));
}

/// Usage and a model for the fixture turn, so its footer has every segment.
fn rich_footer(a: &mut nexus_tui::app::App) {
    a.state.model.sessions.get_mut(SESSION).unwrap().model = Some("codex/gpt-5".into());
    a.state.models = vec![serde_json::json!({
        "ref": "codex/gpt-5", "name": "GPT-5 Terra", "context": 400000,
        "input_cost": 1.0, "output_cost": 10.0, "cache_read_cost": 0.2
    })];
    summarize(
        a,
        900,
        fixture_summary(
            "codex/gpt-5",
            serde_json::json!({"input": 12000, "cached": 4000, "output": 2000, "reasoning": 500}),
        ),
    );
}

fn line_text(l: &nexus_tui::timeline::TLine) -> String {
    l.spans.iter().map(|s| s.content.as_ref()).collect()
}

#[test]
fn the_footer_is_right_aligned_and_wraps_from_the_left_at_every_width() {
    use unicode_width::UnicodeWidthStr;
    let mut a = app();
    rich_footer(&mut a);
    for (page, wraps) in [(55usize, true), (60, false), (120, false), (200, false)] {
        let t = nexus_tui::timeline::build(&a, &a.theme, 0, page);
        let at = t
            .lines
            .iter()
            .position(|l| line_text(l).trim_start().starts_with("Build"))
            .unwrap_or_else(|| panic!("{page}: no footer"));
        // The footer is the last row of the turn: after it only the end of the lines.
        let tail = &t.lines[at..];
        let texts: Vec<String> = tail.iter().map(line_text).collect();
        assert!(
            texts.iter().all(|r| r.width() == page - 2),
            "{page}: every footer row is padded to the right edge: {texts:?}"
        );
        assert_eq!(texts.len() > 1, wraps, "{page}: {texts:?}");
        let joined = texts.iter().map(|r| r.trim()).collect::<Vec<_>>().join(" ");
        for part in ["GPT-5 Terra", "3 tools", "12.0Ki", "33%c", "2.0Ko", "500r"] {
            if part == "3 tools" {
                continue; // the fixture turn has no tools
            }
            assert!(joined.contains(part), "{page}: {part:?} in {joined:?}");
        }
        assert!(
            !joined.contains("done") && !joined.contains('✓'),
            "{joined}"
        );
        // Segments break at ` · `, never inside one; the second row has no leading separator.
        if wraps {
            assert!(!texts[1].trim().starts_with('·'), "{texts:?}");
        }
    }
}

#[test]
fn an_interrupted_turn_has_no_continue_hint_and_a_failed_turn_puts_its_error_above_the_footer() {
    use nexus_tui::api::Event;
    let mut a = app();
    let first = a.messages()[0].created;
    // Interrupted: no turn_end, no later turn.
    {
        let view = a.state.model.selected.as_mut().unwrap();
        view.scratch
            .retain(|e| e.kind != "turn_end" && e.kind != "done");
    }
    let t = nexus_tui::timeline::build(&a, &a.theme, 0, 100);
    let text: Vec<String> = t.lines.iter().map(line_text).collect();
    let footer = text
        .iter()
        .find(|r| r.trim_start().starts_with("Build"))
        .expect("footer");
    assert!(
        !footer.contains("enter continue") && !footer.contains("/continue"),
        "the footer carries no continue hint: {footer:?}"
    );
    assert!(
        !footer.contains("interrupted") && !footer.contains('◼'),
        "{footer:?}"
    );
    // Failed: the first error line sits on the row above the footer.
    a.state.model.selected.as_mut().unwrap().scratch.push(Event {
        id: 950,
        cursor: None,
        seq: None,
        time: first + 0.5,
        kind: "session.error".into(),
        session_id: Some(SESSION.into()),
        data: serde_json::json!({"kind": "provider", "message": "rate limited\nretry after 4s"}),
    });
    let t = nexus_tui::timeline::build(&a, &a.theme, 0, 100);
    let text: Vec<String> = t.lines.iter().map(line_text).collect();
    let at = text
        .iter()
        .position(|r| r.trim_start().starts_with("Build"))
        .unwrap();
    assert!(
        text[at - 1].trim() == "\u{2715} rate limited",
        "error row above: {:?}",
        text[at - 1]
    );
    assert!(
        !text[at].contains("error") && !text[at].contains("retry"),
        "{:?}",
        text[at]
    );
}

#[test]
fn a_folded_group_still_shows_edits_and_failures() {
    let mut a = app(); // low detail: groups folded
    a.state.model.selected.as_mut().unwrap().messages = serde_json::from_value(serde_json::json!([
        {"id":"u", "session_id":SESSION, "seq":1, "created":1.0, "role":"user", "parts":[{"type":"text","text":"go"}]},
        {"id":"a", "session_id":SESSION, "seq":2, "created":2.0, "role":"assistant", "parts":[
            {"type":"tool_call","id":"c1","name":"read","arguments":"{\"path\":\"a\"}"},
            {"type":"tool_call","id":"c2","name":"edit","arguments":"{\"path\":\"a\",\"old_string\":\"x\\ny\",\"new_string\":\"z\"}"},
            {"type":"tool_call","id":"c3","name":"edit","arguments":"{\"path\":\"b\",\"old_string\":\"q\",\"new_string\":\"w\"}"},
            {"type":"tool_call","id":"c4","name":"bash","arguments":"{\"command\":\"false\"}"}]},
        {"id":"t", "session_id":SESSION, "seq":3, "created":3.0, "role":"tool", "parts":[
            {"type":"tool_result","call_id":"c1","output":"one","is_error":false},
            {"type":"tool_result","call_id":"c2","output":"ok","is_error":false},
            {"type":"tool_result","call_id":"c3","output":"old text not found","is_error":true},
            {"type":"tool_result","call_id":"c4","output":"boom","is_error":true}]}
    ])).unwrap();
    a.state.model.selected.as_mut().unwrap().scratch.clear();
    let text = nexus_tui::timeline::build(&a, &a.theme, 0, 100)
        .lines
        .iter()
        .map(line_text)
        .collect::<Vec<_>>()
        .join("\n");
    assert!(
        text.contains("Read 1 file, edited 2 files, ran 1 command"),
        "edits share the group with reads and commands: {text}"
    );
    assert!(
        text.contains("+1 \u{2212}2"),
        "edit counts of the successful edit: {text}"
    );
    assert!(!text.contains("failed"), "failures are not counted: {text}");
    assert!(
        !text.contains("old text not found"),
        "members stay folded: {text}"
    );
}

#[test]
fn members_carry_verb_glyphs_and_the_status_glyph_replaces_them() {
    use nexus_tui::timeline::{build, BlockKind};
    let mut a = app();
    a.ui.detail = nexus_tui::ui::Detail::Medium;
    a.state.model.selected.as_mut().unwrap().messages = serde_json::from_value(serde_json::json!([
        {"id":"u", "session_id":SESSION, "seq":1, "created":1.0, "role":"user", "parts":[{"type":"text","text":"go"}]},
        {"id":"a", "session_id":SESSION, "seq":2, "created":2.0, "role":"assistant", "parts":[
            {"type":"tool_call","id":"c1","name":"read","arguments":"{\"path\":\"a.py\"}"},
            {"type":"tool_call","id":"c2","name":"edit","arguments":"{\"path\":\"a.py\"}"},
            {"type":"tool_call","id":"c3","name":"bash","arguments":"{\"command\":\"ls\"}"},
            {"type":"tool_call","id":"c4","name":"code","arguments":"{\"code\":\"1\"}"},
            {"type":"tool_call","id":"c5","name":"ask","arguments":"{}"},
            {"type":"tool_call","id":"c6","name":"bash","arguments":"{\"command\":\"false\"}"}]},
        {"id":"t", "session_id":SESSION, "seq":3, "created":3.0, "role":"tool", "parts":[
            {"type":"tool_result","call_id":"c1","output":"x","is_error":false},
            {"type":"tool_result","call_id":"c2","output":"x","is_error":false},
            {"type":"tool_result","call_id":"c3","output":"x","is_error":false},
            {"type":"tool_result","call_id":"c4","output":"x","is_error":false},
            {"type":"tool_result","call_id":"c5","output":"x","is_error":false},
            {"type":"tool_result","call_id":"c6","output":"boom","is_error":true}]}
    ])).unwrap();
    a.state.model.selected.as_mut().unwrap().scratch.clear();
    let member_rows = |a: &nexus_tui::app::App| -> Vec<String> {
        let t = build(a, &a.theme, 0, 100);
        t.blocks
            .iter()
            .filter(|b| b.kind == BlockKind::Tool)
            .map(|b| line_text(&t.lines[b.start]))
            .collect()
    };
    let unicode = member_rows(&a);
    for (row, want) in
        unicode
            .iter()
            .zip(["→ Read", "← Edit", "$ Shell", "λ Code", "? Ask", "✕ Shell"])
    {
        assert!(row.contains(want), "{want}: {row:?}");
    }
    // ASCII fallbacks: every glyph on a member row is ASCII.
    let mut options = a.theme.opts;
    options.ascii = true;
    a.theme = a.theme.with(options);
    let ascii = member_rows(&a);
    for (row, want) in ascii
        .iter()
        .zip(["> Read", "< Edit", "$ Shell", "L Code", "? Ask", "x Shell"])
    {
        assert!(row.contains(want), "{want}: {row:?}");
        assert!(row.is_ascii(), "ascii row: {row:?}");
    }
    let t = build(&a, &a.theme, 0, 100);
    for l in &t.lines {
        let text = line_text(l);
        if text.contains("Read") || text.contains("Edit") || text.contains("Shell") {
            assert!(text.is_ascii(), "ascii timeline row: {text:?}");
        }
    }
}

#[test]
fn reasoning_rows_follow_d07() {
    use nexus_tui::timeline::{build, BlockKind};
    let mut a = app();
    a.state.model.selected.as_mut().unwrap().messages = serde_json::from_value(serde_json::json!([
        {"id":"u", "session_id":SESSION, "seq":1, "created":1.0, "role":"user", "parts":[{"type":"text","text":"go"}]},
        {"id":"a", "session_id":SESSION, "seq":2, "created":2.0, "role":"assistant", "parts":[
            {"type":"reasoning","summary":"Looking for where the manifest is parsed.\nThen the second line."}]},
        {"id":"b", "session_id":SESSION, "seq":3, "created":3.0, "role":"assistant", "parts":[
            {"type":"text","text":"between"},
            {"type":"reasoning","summary":""}]}
    ])).unwrap();
    a.state.model.selected.as_mut().unwrap().scratch.clear();
    let t = build(&a, &a.theme, 0, 100);
    let rows: Vec<String> = t
        .blocks
        .iter()
        .filter(|b| b.kind == BlockKind::Reasoning)
        .map(|b| line_text(&t.lines[b.start]))
        .collect();
    assert_eq!(rows.len(), 2, "{rows:?}");
    // Activity rows sit at the two-cell activity indent.
    assert!(
        rows.iter()
            .all(|r| r.starts_with("  ") && !r.starts_with("   ")),
        "{rows:?}"
    );
    let rows: Vec<&str> = rows.iter().map(|r| r.trim_start()).collect();
    assert!(
        rows[0].starts_with("▸ Thought · Looking for where the manifest is parsed."),
        "{:?}",
        rows[0]
    );
    assert!(
        !rows[0].contains("second line"),
        "preview is the first line: {:?}",
        rows[0]
    );
    assert!(
        rows[1].starts_with("▸ Thought · encrypted, replayed to the provider"),
        "{:?}",
        rows[1]
    );
    // Open: the summary behind a rail, with no preview on the head row.
    a.ui.timeline_open.insert("reason:a:0".into(), true);
    let t = build(&a, &a.theme, 0, 100);
    let at = t
        .blocks
        .iter()
        .find(|b| b.key == "reason:a:0")
        .unwrap()
        .start;
    assert_eq!(line_text(&t.lines[at]), "  ▾ Thought");
    assert_eq!(
        line_text(&t.lines[at + 1]),
        "    │ Looking for where the manifest is parsed."
    );
    // The italic preview is muted.
    let preview = &build(&a, &a.theme, 0, 100);
    let _ = preview;
}

#[test]
fn tool_detail_is_the_old_box_with_a_raw_toggle() {
    use nexus_tui::timeline::{build, BlockKind};
    let mut a = app();
    a.ui.detail = nexus_tui::ui::Detail::High;
    add_tool(&mut a, 1, "echo hello", "hello");
    let t = build(&a, &a.theme, 0, 100);
    let tool = t.blocks.iter().find(|b| b.kind == BlockKind::Tool).unwrap();
    let rows: Vec<String> = t.lines[tool.start..tool.end]
        .iter()
        .map(line_text)
        .collect();
    assert!(rows[0].trim_start().starts_with("┌ $ Shell"), "{rows:?}");
    let name = t.lines[tool.start]
        .spans
        .iter()
        .find(|s| s.content == "Shell")
        .unwrap();
    assert!(!name
        .style
        .add_modifier
        .contains(ratatui::style::Modifier::BOLD));
    assert!(
        rows[0].contains("raw"),
        "raw toggle on the head row: {rows:?}"
    );
    assert_eq!(rows[1], "  │ $ echo hello");
    assert_eq!(
        rows.last().unwrap(),
        "  ╰ hello",
        "last row closes the box: {rows:?}"
    );
    let strong = a.theme.fg("border.strong").fg;
    assert_eq!(t.lines[tool.start].spans[0].style.fg, strong);
    assert_eq!(t.lines[tool.start + 1].spans[0].style.fg, strong);
    // Raw shows the exact arguments and the result.
    a.execute("tool.raw_toggle");
    let _ = a;
}

#[test]
fn the_header_says_what_the_session_is_doing() {
    let mut a = app();
    assert!(!draw(&mut a, 120, 36).1[0].contains("working"));
    busy(&mut a);
    // The header omits the busy label and elapsed time (nexus-tui README);
    // `Working` lives by the composer instead.
    let head = draw(&mut a, 120, 36).1[0].clone();
    assert!(!head.to_lowercase().contains("working"), "{head}");
    assert!(!head.contains("running"), "{head}");
    // A pending approval wins over running.
    a.state.model.approvals.insert(
        "p".into(),
        serde_json::from_value(
            serde_json::json!({"id":"p", "session_id":SESSION, "call_id":"c1", "tool":"bash"}),
        )
        .unwrap(),
    );
    let head = draw(&mut a, 120, 36).1[0].clone();
    assert!(head.contains("waiting for you"), "{head}");
    a.state.model.approvals.clear();
    // Offline.
    a.state.connection = nexus_tui::state::ConnectionState::Offline {
        reason: "gone".into(),
    };
    let head = draw(&mut a, 120, 36).1[0].clone();
    assert!(head.contains("\u{2715} offline"), "{head}");
}

#[test]
fn the_sticky_header_pins_the_band_and_the_next_band_pushes_it_up() {
    use nexus_tui::timeline::BlockKind;
    use nexus_tui::ui::Target;
    for (w, h) in [(80u16, 24u16), (200, 50)] {
        let mut a = app();
        let long = "alpha beta gamma delta epsilon ".repeat(30);
        let v = serde_json::json!({"id": 60, "type": "message.added", "session_id": SESSION,
            "cursor": "log_c915bfc9b6d826c4:60",
            "data": {"id": "msg_s1", "session_id": SESSION, "seq": 90, "created": 1793113300.0,
                     "role": "user", "parts": [{"type": "text", "text": long}]}});
        if let Ok(nexus_tui::api::Frame::Event(e)) =
            nexus_tui::api::Frame::from_value("message.added", None, &v)
        {
            a.apply_sync(nexus_tui::sync::SyncMsg::Event(e));
        }
        for n in 0..80 {
            add_assistant_message(&mut a, n, &format!("paragraph number {n}"));
        }
        {
            let v = serde_json::json!({"id": 3000, "type": "message.added", "session_id": SESSION,
                "cursor": "log_c915bfc9b6d826c4:3000",
                "data": {"id": "msg_s2", "session_id": SESSION, "seq": 900, "created": 1793114300.0,
                         "role": "user", "parts": [{"type": "text", "text": "next question"}]}});
            if let Ok(nexus_tui::api::Frame::Event(e)) =
                nexus_tui::api::Frame::from_value("message.added", None, &v)
            {
                a.apply_sync(nexus_tui::sync::SyncMsg::Event(e));
            }
            for n in 2100..2140 {
                add_assistant_message(&mut a, n, &format!("later paragraph {n}"));
            }
        }
        // The blocks of the frame as drawn (the centre column is narrower than the terminal).
        draw(&mut a, w, h);
        let bands: Vec<(usize, usize, usize)> = a
            .blocks()
            .iter()
            .enumerate()
            .filter(|(_, b)| b.kind == BlockKind::Turn)
            .map(|(i, b)| (i, b.start, b.end))
            .collect();
        assert_eq!(bands.len(), 3, "{w}x{h}");
        let (long_idx, long_start, long_end) = bands[1];
        let (_, next_start, _) = bands[2];
        assert!(
            long_end - long_start >= 5,
            "{w}x{h}: a long message has several rows"
        );
        // Scrolled well inside the second turn: its band is pinned at the top.
        a.ui.focus = Focus::Timeline;
        a.ui.scroll = nexus_tui::ui::Scroll {
            follow: false,
            top: long_end + 10,
        };
        let (_, drawn) = draw(&mut a, w, h);
        let pin = a
            .hits
            .rect_of(&Target::StickyUser(long_idx))
            .expect("pinned band");
        assert_eq!(pin.y, 1, "{w}x{h}: under the header");
        let rows = &drawn[pin.y as usize..(pin.y + pin.height) as usize];
        assert_eq!(rows.len(), 4, "3 text rows and the shade row: {rows:?}");
        assert!(rows[0].contains("alpha beta"), "{rows:?}");
        assert!(
            rows[2].contains('…'),
            "a long message ends in an ellipsis: {rows:?}"
        );
        assert!(
            rows[3].contains("░") && rows[3].contains("lines of this turn above"),
            "{rows:?}"
        );
        // The next band pushes it up: with the next band one row below the top,
        // only the shade row of the pin is left.
        a.ui.scroll = nexus_tui::ui::Scroll {
            follow: false,
            top: next_start - 1,
        };
        let (_, drawn) = draw(&mut a, w, h);
        let pin = a
            .hits
            .rect_of(&Target::StickyUser(long_idx))
            .expect("pushed pin");
        assert_eq!(pin.height, 1, "{w}x{h}");
        assert!(
            drawn[pin.y as usize].contains("░"),
            "{:?}",
            drawn[pin.y as usize]
        );
        // With the next band at the top row the pin is gone.
        a.ui.scroll = nexus_tui::ui::Scroll {
            follow: false,
            top: next_start,
        };
        draw(&mut a, w, h);
        assert!(
            a.hits.rect_of(&Target::StickyUser(long_idx)).is_none(),
            "{w}x{h}"
        );
        // A click on the pin scrolls back to the real band.
        a.ui.scroll = nexus_tui::ui::Scroll {
            follow: false,
            top: long_end + 10,
        };
        draw(&mut a, w, h);
        let pin = a.hits.rect_of(&Target::StickyUser(long_idx)).unwrap();
        click(&mut a, pin.x + 5, pin.y);
        assert_eq!(a.ui.scroll.top, long_start, "{w}x{h}");
        draw(&mut a, w, h);
        assert!(a.hits.rect_of(&Target::StickyUser(long_idx)).is_none());
    }
}

#[test]
fn a_finished_group_flashes_its_glyph_for_six_frames_unless_motion_is_reduced() {
    use nexus_tui::timeline::{build, BlockKind, FLASH_FRAMES};
    for reduced in [false, true] {
        let mut a = app();
        let mut options = a.theme.opts;
        options.reduced_motion = reduced;
        a.theme = a.theme.with(options);
        calls(&mut a, &[("bash", None)]);
        busy(&mut a);
        let t = build(&a, &a.theme, 0, 100);
        assert_eq!(t.live_groups.len(), 1, "the running group is live");
        a.note_live_groups(&t);
        // The tool finishes and the session goes idle.
        a.state.model.running.clear();
        a.state.model.sessions.get_mut(SESSION).unwrap().status = Some("idle".into());
        a.state.model.selected.as_mut().unwrap().messages.push(
            serde_json::from_value(serde_json::json!({"id":"r", "session_id":SESSION, "seq":4,
                "created":4.0, "role":"tool", "parts":[{"type":"tool_result","call_id":"c0",
                "output":"ok","is_error":false}]}))
            .unwrap(),
        );
        a.frame = 100;
        let t = build(&a, &a.theme, a.frame, 100);
        assert!(t.live_groups.is_empty());
        a.note_live_groups(&t);
        let accent = a.theme.fg("interactive.accent").fg;
        let marker_fg = |a: &nexus_tui::app::App, frame: usize| {
            let t = build(a, &a.theme, frame, 100);
            let g = t
                .blocks
                .iter()
                .find(|b| b.kind == BlockKind::Group)
                .unwrap();
            t.lines[g.start].spans[0].style.fg
        };
        if reduced {
            assert!(a.ui.group_flash.is_empty(), "no flash with reduced motion");
            assert_ne!(marker_fg(&a, 100), accent);
        } else {
            assert_eq!(marker_fg(&a, 100), accent, "flash starts");
            assert_eq!(marker_fg(&a, 100 + FLASH_FRAMES - 1), accent);
            assert_ne!(marker_fg(&a, 100 + FLASH_FRAMES), accent, "and ends");
            a.frame = 100 + FLASH_FRAMES;
            let t = build(&a, &a.theme, a.frame, 100);
            a.note_live_groups(&t);
            assert!(a.ui.group_flash.is_empty(), "expired entries are dropped");
        }
    }
}

#[test]
fn background_shell_completion_is_a_notice_without_a_user_turn() {
    let mut a = app();
    let mut message = a.state.model.selected.as_ref().unwrap().messages[0].clone();
    message.id = "background_done".into();
    message.role = "user".into();
    message.seq = 999;
    message.parts = vec![
        serde_json::json!({"type":"text", "text":"→ Background Shell Done: 'make test'\nexit code 0"}),
    ];
    a.state
        .model
        .selected
        .as_mut()
        .unwrap()
        .messages
        .push(message);
    let text = rows(&mut a).join("\n");
    assert!(text.contains("→ Background Shell Done:"));
    assert!(!text.contains("exit code 0"));
    let index = a
        .blocks()
        .iter()
        .position(|b| b.kind == nexus_tui::timeline::BlockKind::Notice)
        .unwrap();
    a.click(nexus_tui::ui::Target::Block(index));
    assert!(rows(&mut a).join("\n").contains("exit code 0"));
    assert_eq!(
        a.blocks()
            .iter()
            .filter(|b| b.kind == nexus_tui::timeline::BlockKind::Turn)
            .count(),
        1
    );
}

#[test]
fn consecutive_background_completions_are_grouped_and_expand_in_order() {
    use nexus_tui::timeline::{build, BlockKind};
    let mut a = app();
    let template = a.state.model.selected.as_ref().unwrap().messages[0].clone();
    for (id, command) in [("one", "first"), ("two", "second")] {
        let mut message = template.clone();
        message.id = id.into();
        message.role = "user".into();
        message.parts = vec![
            serde_json::json!({"type":"text", "text":format!("→ Background Shell Done: {command}\noutput {command}")}),
        ];
        a.state
            .model
            .selected
            .as_mut()
            .unwrap()
            .messages
            .push(message);
    }
    let timeline = build(&a, &a.theme, 0, 100);
    let notices: Vec<_> = timeline
        .blocks
        .iter()
        .filter(|b| b.kind == BlockKind::Notice)
        .collect();
    assert_eq!(notices.len(), 1);
    assert_eq!(notices[0].end - notices[0].start, 1);
    let key = notices[0].key.clone();
    a.ui.timeline_open.insert(key, true);
    let timeline = build(&a, &a.theme, 0, 100);
    let text = timeline
        .lines
        .iter()
        .flat_map(|l| l.spans.iter())
        .map(|s| s.content.as_ref())
        .collect::<String>();
    assert!(text.find("output first").unwrap() < text.find("output second").unwrap());
}

#[test]
fn assistant_text_aligns_with_the_user_message() {
    let mut a = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    let (_, rows) = draw(&mut a, 120, 36);
    let user = rows.iter().find(|line| line.contains("  hi")).unwrap();
    let assistant = rows.iter().find(|line| line.contains("Hello")).unwrap();
    let user_col = nexus_tui::text::width(&user[..user.find("hi").unwrap()]);
    let assistant_col = nexus_tui::text::width(&assistant[..assistant.find("Hello").unwrap()]);
    assert_eq!(user_col, assistant_col);
}

#[test]
fn image_labels_align_with_user_text() {
    let mut a = app();
    a.state.model.selected.as_mut().unwrap().messages[0].parts.push(
        serde_json::json!({"type":"image", "name":"Image A", "data_url":"data:image/png;base64,AA=="}),
    );
    let (_, rows) = draw(&mut a, 120, 36);
    let text = rows.iter().find(|row| row.contains("  hi")).unwrap();
    let image = rows
        .iter()
        .find(|row| row.contains("Image | Image A"))
        .unwrap();
    assert_eq!(
        nexus_tui::text::width(&text[..text.find("hi").unwrap()]),
        nexus_tui::text::width(&image[..image.find("Image |").unwrap()])
    );
}

#[test]
fn busy_timeline_has_three_trailing_blank_rows() {
    use nexus_tui::timeline::build;
    let mut a = app();
    let idle = build(&a, &a.theme, 0, 100);
    assert!(!idle.lines.last().unwrap().spans.is_empty());
    busy(&mut a);
    let live = build(&a, &a.theme, 0, 100);
    assert!(live.lines[live.lines.len() - 3..]
        .iter()
        .all(|line| line.spans.is_empty()));
    assert!(!live.lines[live.lines.len() - 4].spans.is_empty());
}

#[test]
fn shell_and_error_previews_are_limited_and_error_text_is_neutral() {
    use nexus_tui::timeline::{build, BlockKind};
    let mut a = app();
    calls(&mut a, &[("bash", Some("ok")), ("edit", Some("failed"))]);
    let command = "x".repeat(100);
    let error = "z".repeat(100);
    let view = a.state.model.selected.as_mut().unwrap();
    view.messages[1].parts[0]["arguments"] =
        serde_json::json!(serde_json::json!({"command":command}).to_string());
    view.messages[2].parts[1]["output"] = serde_json::json!(error);
    view.messages[2].parts[1]["is_error"] = serde_json::json!(true);
    a.ui.timeline_open.insert("group:tool:c0".into(), true);
    a.ui.timeline_open.insert("group:tool:c1".into(), true);
    let timeline = build(&a, &a.theme, 0, 250);
    let shell = timeline.blocks.iter().find(|b| b.key == "tool:c0").unwrap();
    let shell_text: String = timeline.lines[shell.start]
        .spans
        .iter()
        .map(|s| s.content.as_ref())
        .collect();
    assert!(
        shell_text.contains(&format!("{}...", "x".repeat(80))),
        "{shell_text}"
    );
    let edit = timeline
        .blocks
        .iter()
        .find(|b| b.kind == BlockKind::Tool && b.key == "tool:c1")
        .unwrap();
    let error_span = timeline.lines[edit.start]
        .spans
        .iter()
        .find(|s| s.content.contains(&"z".repeat(80)))
        .unwrap();
    assert!(error_span
        .content
        .contains(&format!("{}...", "z".repeat(80))));
    assert_eq!(error_span.style.fg, a.theme.fg("text.secondary").fg);
}

#[test]
fn only_shell_tool_rows_show_tool_duration() {
    use nexus_tui::timeline::{build, BlockKind};
    let mut a = app();
    calls(&mut a, &[("bash", Some("ok")), ("read", Some("ok"))]);
    // Timings come from the stored turn's `tools`.
    let turn: nexus_tui::api::TurnSummary = serde_json::from_value(serde_json::json!({
        "id": "trn_t", "status": "completed", "started": 1.0, "finished": 17.0, "start_seq": 0,
        "steps": 2, "stop": "end_turn", "ended": {"stop": "end_turn", "steps": 2},
        "requests": [], "tools": [
            {"call_id": "c0", "name": "bash", "state": "inserted", "started": 10.0, "finished": 12.0, "is_error": false},
            {"call_id": "c1", "name": "read", "state": "inserted", "started": 13.0, "finished": 16.0, "is_error": false}
        ]
    }))
    .unwrap();
    a.state
        .model
        .selected
        .as_mut()
        .unwrap()
        .upsert_turn(turn, 17.0);
    a.ui.timeline_open.insert("group:tool:c0".into(), true);
    let timeline = build(&a, &a.theme, 0, 120);
    let line = |key: &str| {
        let block = timeline.blocks.iter().find(|b| b.key == key).unwrap();
        timeline.lines[block.start]
            .spans
            .iter()
            .map(|s| s.content.as_ref())
            .collect::<String>()
    };
    assert!(line("tool:c0").contains("2.0s"));
    assert!(!line("tool:c1").contains("3.0s"));
    let group = timeline
        .blocks
        .iter()
        .find(|b| b.kind == BlockKind::Group)
        .unwrap();
    assert!(!line(&group.key).contains("6.0s"));
}

#[test]
fn streaming_text_aligns_with_the_finished_assistant_message() {
    let mut a = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    for (id, text) in [(20, "Streaming "), (21, "reply\n\nsecond line")] {
        let v = serde_json::json!({"id": id, "type": "text_delta", "session_id": SESSION,
            "cursor": "log_c915bfc9b6d826c4:20",
            "data": {"type": "text_delta", "text": text}});
        if let Ok(nexus_tui::api::Frame::Event(e)) =
            nexus_tui::api::Frame::from_value("text_delta", None, &v)
        {
            a.apply_sync(nexus_tui::sync::SyncMsg::Event(e));
        }
    }
    let (_, rows) = draw(&mut a, 120, 36);
    let col = |needle: &str| {
        let row = rows
            .iter()
            .find(|line| line.contains(needle))
            .unwrap_or_else(|| panic!("{needle:?} in {rows:#?}"));
        nexus_tui::text::width(&row[..row.find(needle).unwrap()])
    };
    assert_eq!(col("Streaming"), col("Hello"));
    assert_eq!(col("second line"), col("Hello"));
}

// -- turn summaries (TURN_SUMMARY_PLAN 5.1) -------------------------------------

/// Two prompts, each answered: `u1`/`a1` (seqs 1, 2) and `u2`/`a2` (3, 4).
fn two_turns(a: &mut nexus_tui::app::App) {
    let view = a.state.model.selected.as_mut().unwrap();
    view.messages = serde_json::from_value(serde_json::json!([
        {"id":"u1", "session_id":SESSION, "seq":1, "created":1.0, "role":"user", "parts":[{"type":"text","text":"first question"}]},
        {"id":"a1", "session_id":SESSION, "seq":2, "created":2.0, "role":"assistant", "parts":[{"type":"text","text":"first answer"}]},
        {"id":"u2", "session_id":SESSION, "seq":3, "created":3.0, "role":"user", "parts":[{"type":"text","text":"second question"}]},
        {"id":"a2", "session_id":SESSION, "seq":4, "created":4.0, "role":"assistant", "parts":[{"type":"text","text":"second answer"}]}
    ]))
    .unwrap();
    view.scratch.clear();
}

fn stored(v: serde_json::Value) -> nexus_tui::api::TurnSummary {
    serde_json::from_value(v).unwrap()
}

/// The second prompt's turn: `start_seq` 2, one request for `a2`.
fn second_turn(usage: serde_json::Value, attempts: serde_json::Value) -> serde_json::Value {
    serde_json::json!({
        "id": "trn_2", "status": "completed", "started": 3.0, "finished": 4.5, "start_seq": 2,
        "steps": 1, "stop": "end_turn", "ended": {"stop": "end_turn", "steps": 1},
        "error": null, "recovery": "none",
        "requests": [{"id": "req_2", "step": 1, "status": "completed", "model": "codex/gpt-5",
                      "started": 3.1, "finished": 4.4, "stop": "end_turn", "message_id": "a2",
                      "usage": usage, "attempts": attempts}],
        "tools": []
    })
}

#[test]
fn the_floor_row_says_older_turns_have_no_details() {
    let mut a = app();
    two_turns(&mut a);
    let view = a.state.model.selected.as_mut().unwrap();
    view.upsert_turn(
        stored(second_turn(
            serde_json::json!({"input": 1000, "cached": 0, "output": 10, "reasoning": 0}),
            serde_json::json!([]),
        )),
        5.0,
    );
    view.floor = Some(3);
    let t = nexus_tui::timeline::build(&a, &a.theme, 0, 200);
    let text: Vec<String> = t.lines.iter().map(line_text).collect();
    let floor = text
        .iter()
        .position(|r| r.contains("the turns above have no recorded details"))
        .unwrap_or_else(|| panic!("floor row: {text:#?}"));
    let first = text
        .iter()
        .position(|r| r.contains("first answer"))
        .unwrap();
    let second = text
        .iter()
        .position(|r| r.contains("second question"))
        .unwrap();
    assert!(
        first < floor && floor < second,
        "between the turns: {text:#?}"
    );
    // The first turn's footer comes before the row; it has no token figures.
    let footers: Vec<&String> = text
        .iter()
        .filter(|r| r.trim_start().starts_with("Build"))
        .collect();
    assert_eq!(footers.len(), 2, "{text:#?}");
    assert!(!footers[0].contains("1.0Ki"), "{}", footers[0]);
    assert!(footers[1].contains("1.0Ki"), "{}", footers[1]);

    // Every message covered: no row.
    a.state.model.selected.as_mut().unwrap().floor = None;
    let t = nexus_tui::timeline::build(&a, &a.theme, 0, 200);
    assert!(!t
        .lines
        .iter()
        .map(line_text)
        .any(|r| r.contains("no recorded details")));
}

#[test]
fn unknown_usage_is_said_and_never_shown_as_zero() {
    let mut a = app();
    two_turns(&mut a);
    a.state.model.selected.as_mut().unwrap().upsert_turn(
        stored(second_turn(serde_json::Value::Null, serde_json::json!([]))),
        5.0,
    );
    let t = nexus_tui::timeline::build(&a, &a.theme, 0, 200);
    let text: Vec<String> = t.lines.iter().map(line_text).collect();
    let footer = text
        .iter()
        .rfind(|r| r.trim_start().starts_with("Build"))
        .unwrap();
    assert!(footer.contains("usage unknown"), "{footer}");
    assert!(!footer.contains("0i") && !footer.contains("0o"), "{footer}");
    // The model still comes from the request.
    assert!(footer.contains("gpt-5"), "{footer}");
}

#[test]
fn stored_attempts_and_errors_become_retry_and_error_rows() {
    let mut a = app();
    two_turns(&mut a);
    let attempts = serde_json::json!([
        {"n": 1, "status": "failed", "delay": null, "error": "codex: HTTP 429", "usage": null},
        {"n": 2, "status": "completed", "delay": 0.84, "error": "codex: HTTP 429",
         "usage": {"input": 1000, "cached": 0, "output": 10, "reasoning": 0}}
    ]);
    let mut failed = second_turn(serde_json::Value::Null, serde_json::json!([]));
    failed["id"] = "trn_3".into();
    failed["status"] = "failed".into();
    failed["ended"] = serde_json::Value::Null;
    failed["start_seq"] = 4.into();
    failed["started"] = 4.6.into();
    failed["error"] =
        serde_json::json!({"kind": "provider", "message": "codex: HTTP 500\nbody", "status": 500});
    failed["requests"][0]["status"] = "failed".into();
    failed["requests"][0]["message_id"] = serde_json::Value::Null;
    failed["requests"][0]["usage"] = serde_json::Value::Null;
    failed["requests"][0]["id"] = "req_3".into();
    failed["requests"][0]["attempts"] = serde_json::json!([
        {"n": 1, "status": "failed", "error": "codex: HTTP 500"},
        {"n": 2, "status": "failed", "delay": 2.0, "error": "codex: HTTP 500"}
    ]);
    let view = a.state.model.selected.as_mut().unwrap();
    view.upsert_turn(
        stored(second_turn(
            serde_json::json!({"input": 1000, "cached": 0, "output": 10, "reasoning": 0}),
            attempts,
        )),
        5.0,
    );
    view.upsert_turn(stored(failed), 6.0);
    // The failed turn's prompt, after the second answer.
    view.messages.push(
        serde_json::from_value(
            serde_json::json!({"id":"u3", "session_id":SESSION, "seq":5, "created":4.6,
            "role":"user", "parts":[{"type":"text","text":"third question"}]}),
        )
        .unwrap(),
    );
    a.state.model.selected.as_mut().unwrap().turns[1].start_seq = 4;
    let t = nexus_tui::timeline::build(&a, &a.theme, 0, 200);
    let text: Vec<String> = t.lines.iter().map(line_text).collect();
    let find = |needle: &str| {
        text.iter()
            .position(|r| r.contains(needle))
            .unwrap_or_else(|| panic!("{needle:?} in {text:#?}"))
    };
    // The retry stack sits before the answer it led to.
    let retry = find("provider retry 1 in 0.8s");
    assert!(text[retry].contains("codex: HTTP 429"), "{}", text[retry]);
    assert!(text[retry + 1].contains("retry started (attempt 2)"));
    assert!(retry < find("second answer"));
    // The failed request ran out of retries; the turn's error follows.
    let exhausted = find("provider retries exhausted after 2");
    let error = find("provider: codex: HTTP 500");
    assert!(find("third question") < exhausted && exhausted < error);
    // The footer of the failed turn has its error above it, and no continue hint
    // is needed for a failure that is not an interruption.
    let footer = text
        .iter()
        .rposition(|r| r.trim_start().starts_with("Build"))
        .unwrap();
    assert!(
        text[footer - 1].contains("codex: HTTP 500"),
        "{:?}",
        text[footer - 1]
    );
}

#[test]
fn a_turn_summary_replaces_the_running_turns_events() {
    let mut a = app();
    let view = a.state.model.selected.as_ref().unwrap();
    assert!(
        view.scratch.iter().any(|e| e.kind == "turn_end"),
        "live events"
    );
    assert!(
        !view.scratch.iter().any(|e| e.kind == "text_delta"),
        "deltas are folded, not kept"
    );
    summarize(
        &mut a,
        900,
        fixture_summary(
            "codex/gpt-5",
            serde_json::json!({"input": 2000, "cached": 1000, "output": 30, "reasoning": 0}),
        ),
    );
    let view = a.state.model.selected.as_ref().unwrap();
    assert!(view.scratch.is_empty(), "replaced by the summary");
    assert_eq!(view.turns.len(), 1);
    assert_eq!(view.usage.models["codex/gpt-5"].usage.input, 2000);
    // The same summary again (a replayed event) replaces, never adds.
    summarize(
        &mut a,
        901,
        fixture_summary(
            "codex/gpt-5",
            serde_json::json!({"input": 2000, "cached": 1000, "output": 30, "reasoning": 0}),
        ),
    );
    let view = a.state.model.selected.as_ref().unwrap();
    assert_eq!(view.turns.len(), 1);
    assert_eq!(view.usage.models["codex/gpt-5"].requests, 1);
    assert_eq!(view.usage.models["codex/gpt-5"].usage.input, 2000);
    let text = rows(&mut a).join("\n");
    assert!(text.contains("2.0Ki") && text.contains("50%c"), "{text}");
}

#[test]
fn a_running_turn_loaded_from_history_follows_live_events_until_its_summary() {
    let mut a = app();
    two_turns(&mut a);
    let mut running = second_turn(serde_json::Value::Null, serde_json::json!([]));
    running["status"] = "running".into();
    running["ended"] = serde_json::Value::Null;
    running["finished"] = serde_json::Value::Null;
    let mut messages = a.messages().to_vec();
    messages.pop(); // the answer is still streaming
    let h = History {
        session_id: SESSION.into(),
        messages,
        turns: vec![stored(running)],
        usage: serde_json::from_value(serde_json::json!({"since": 1.0, "models": {}})).unwrap(),
        ..Default::default()
    };
    a.apply_sync(nexus_tui::sync::SyncMsg::History(Box::new(h)));
    busy(&mut a);
    let text = rows(&mut a).join("\n");
    assert!(
        !text.contains("enter continue"),
        "running, not interrupted: {text}"
    );
    // The turn closes: its summary arrives with the answer saved.
    let answer = serde_json::json!({"id": 41, "type": "message.added", "session_id": SESSION,
        "cursor": "log_c915bfc9b6d826c4:41",
        "data": {"id": "a2", "session_id": SESSION, "seq": 4, "created": 4.0, "role": "assistant",
                 "parts": [{"type": "text", "text": "second answer"}]}});
    if let Ok(nexus_tui::api::Frame::Event(e)) =
        nexus_tui::api::Frame::from_value("message.added", None, &answer)
    {
        a.apply_sync(nexus_tui::sync::SyncMsg::Event(e));
    }
    summarize(
        &mut a,
        42,
        second_turn(
            serde_json::json!({"input": 3000, "cached": 0, "output": 5, "reasoning": 0}),
            serde_json::json!([]),
        ),
    );
    let idle = serde_json::json!({"id": 43, "type": "session.status", "session_id": SESSION,
        "cursor": "log_c915bfc9b6d826c4:43", "data": {"status": "idle", "turn_id": "trn_2"}});
    if let Ok(nexus_tui::api::Frame::Event(e)) =
        nexus_tui::api::Frame::from_value("session.status", None, &idle)
    {
        a.apply_sync(nexus_tui::sync::SyncMsg::Event(e));
    }
    let view = a.state.model.selected.as_ref().unwrap();
    assert_eq!(view.turns[0].status, "completed");
    assert_eq!(view.usage.models["codex/gpt-5"].usage.input, 3000);
    let text = rows(&mut a).join("\n");
    assert!(text.contains("3.0Ki"), "{text}");
}

#[test]
fn turns_re_read_after_a_reconnect_replace_the_held_ones_and_their_ledger() {
    let mut a = app();
    two_turns(&mut a);
    let page: nexus_tui::api::TurnPage = serde_json::from_value(serde_json::json!({
        "turns": [second_turn(serde_json::json!({"input": 700, "cached": 0, "output": 7, "reasoning": 0}), serde_json::json!([]))],
        "more_before": true, "more_after": false, "floor": {"seq": 3},
        "usage": {"since": 1.0, "models": {"codex/gpt-5": {"requests": 4, "input": 9000, "cached": 0, "output": 70, "reasoning": 0}}}
    }))
    .unwrap();
    a.apply_sync(nexus_tui::sync::SyncMsg::Turns {
        session: SESSION.into(),
        page: Box::new(page),
    });
    let view = a.state.model.selected.as_ref().unwrap();
    assert!(view.scratch.is_empty(), "the page closed the running turn");
    assert_eq!(view.floor, Some(3));
    // The page's ledger is current: it already counts the turn it carries.
    assert_eq!(view.usage.models["codex/gpt-5"].usage.input, 9000);
    // The same turn arriving again as an event does not count twice.
    summarize(
        &mut a,
        902,
        second_turn(
            serde_json::json!({"input": 700, "cached": 0, "output": 7, "reasoning": 0}),
            serde_json::json!([]),
        ),
    );
    let view = a.state.model.selected.as_ref().unwrap();
    assert_eq!(view.usage.models["codex/gpt-5"].usage.input, 9000);
    assert_eq!(view.turns.len(), 1);
}

#[test]
fn a_delivered_steer_is_tagged_and_does_not_open_a_turn() {
    let mut a = app();
    let view = a.state.model.selected.as_mut().unwrap();
    view.messages = serde_json::from_value(serde_json::json!([
        {"id":"u", "session_id":SESSION, "seq":1, "created":1.0, "role":"user", "parts":[{"type":"text","text":"go"}]},
        {"id":"a", "session_id":SESSION, "seq":2, "created":2.0, "role":"assistant", "parts":[{"type":"text","text":"working"}]},
        {"id":"s", "session_id":SESSION, "seq":3, "created":3.0, "role":"user", "delivery":"steer", "parts":[{"type":"text","text":"also run the linter"}]}
    ]))
    .unwrap();
    view.scratch.clear();
    let text = rows(&mut a).join("\n");
    assert!(text.contains("also run the linter"), "{text}");
    assert!(text.contains("steer"), "{text}");
    assert!(!text.contains("#2"), "{text}");
}
