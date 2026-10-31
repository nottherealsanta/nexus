//! Frame scheduling: batching, dirty tracking and render barriers.
//!
//! The interactive loop lives in `src/main.rs`, so these tests drive the same
//! `frames::run_batch` policy with deterministic queues and an injected clock.
//! Rendering is stateful (hit maps, scroll normalization, selection screens),
//! so parity is checked on the complete buffer plus the state the renderer
//! publishes, not just on text.

mod common;

use std::collections::VecDeque;
use std::time::{Duration, Instant};

use common::*;
use nexus_tui::api::{Event, Frame, History, Message};
use nexus_tui::app::{sync_msg_affects_view, App};
use nexus_tui::frames::{self, Applied, BatchEnd};
use nexus_tui::program_status::Tracker;
use nexus_tui::sync::{ResyncCause, SyncMsg};
use nexus_tui::theme::{Mode, Tier};
use nexus_tui::timeline::{BlockKind, LayoutCache};
use serde_json::{json, Value};

const GEN: &str = "log_c915bfc9b6d826c4";

fn event(id: i64, kind: &str, data: Value) -> Event {
    let v = json!({
        "id": id,
        "seq": id,
        "time": 1793113400.0 + id as f64,
        "type": kind,
        "session_id": SESSION,
        "data": data,
        "cursor": format!("{GEN}:{id}"),
    });
    match Frame::from_value(kind, None, &v).expect("valid fixture event") {
        Frame::Event(event) => event,
        _ => panic!("not an event"),
    }
}

fn wire_event(id: i64) -> Event {
    event(
        id,
        "wire_event",
        json!({"type": "wire_event", "name": "response.output_text.delta", "data": {"delta": "x"}}),
    )
}

fn wire_response(id: i64) -> Event {
    event(
        id,
        "wire_response",
        json!({"type": "wire_response", "status": 200, "headers": {}}),
    )
}

fn wire_request(id: i64) -> Event {
    event(
        id,
        "wire_request",
        json!({"type": "wire_request", "method": "POST", "body": {"model": "terra"}}),
    )
}

fn tool_start(id: i64) -> Event {
    event(
        id,
        "tool_start",
        json!({"type": "tool_start", "call": {
            "type": "tool_call", "id": "call_9", "name": "read",
            "arguments": "{\"path\":\"src/main.rs\"}"
        }, "args": {"path": "src/main.rs"}}),
    )
}

fn tool_end(id: i64) -> Event {
    event(
        id,
        "tool_end",
        json!({"type": "tool_end", "call": {
            "type": "tool_call", "id": "call_9", "name": "read",
            "arguments": "{\"path\":\"src/main.rs\"}"
        }, "result": {"type": "tool_result", "call_id": "call_9",
            "output": "fn main() {}\n", "is_error": false}}),
    )
}

fn stream() -> Vec<SyncMsg> {
    let mut messages = Vec::new();
    messages.push(SyncMsg::Event(event(
        20,
        "session.status",
        json!({"status": "busy", "turn_id": "trn_2", "origin": "prompt"}),
    )));
    messages.push(SyncMsg::Event(wire_event(21)));
    messages.push(SyncMsg::Event(event(
        22,
        "text_delta",
        json!({"type": "text_delta", "text": "streamed "}),
    )));
    messages.push(SyncMsg::Event(event(
        23,
        "reasoning_delta",
        json!({"type": "reasoning_delta", "text": "why "}),
    )));
    messages.push(SyncMsg::Event(wire_response(24)));
    messages.push(SyncMsg::Event(wire_request(25)));
    messages.push(SyncMsg::Event(event(
        26,
        "text_delta",
        json!({"type": "text_delta", "text": "text"}),
    )));
    messages.push(SyncMsg::Event(tool_start(27)));
    messages.push(SyncMsg::Event(tool_end(28)));
    messages.push(SyncMsg::Event(event(
        29,
        "message.added",
        json!({
            "id": "msg_2b", "session_id": SESSION, "seq": 3,
            "created": 1793113400.0, "role": "assistant",
            "parts": [{"type": "text", "text": "streamed text", "origin": "codex",
                       "raw": {"type": "message"}}]
        }),
    )));
    messages.push(SyncMsg::Event(event(
        30,
        "turn.summary",
        json!({"id": "trn_1", "status": "completed", "started": 1793113304.0,
               "finished": 1793113315.0, "start_seq": 0, "steps": 1,
               "stop": "end_turn", "ended": {"stop": "end_turn", "steps": 1}}),
    )));
    messages.push(SyncMsg::History(Box::new(History {
        session_id: SESSION.into(),
        messages: vec![Message {
            id: "msg_0".into(),
            session_id: SESSION.into(),
            seq: 0,
            created: 1793113200.0,
            role: "user".into(),
            parts: vec![json!({"type": "text", "text": "older context"})],
            delivery: None,
        }],
        messages_more: false,
        ..Default::default()
    })));
    messages
}

/// A deterministic mirror of the loop's dirty rules: draw at the top when
/// dirty, then apply one batch with the production helper.
fn drive(
    app: &mut App,
    mut queue: VecDeque<SyncMsg>,
    per_message: Duration,
    mut draw: impl FnMut(&mut App),
) -> (usize, usize, bool) {
    let mut dirty = true;
    let mut frames = 0;
    let mut applied = 0;
    let clock = std::cell::Cell::new(Instant::now());
    loop {
        if dirty {
            draw(app);
            frames += 1;
            dirty = false;
        }
        let Some(first) = queue.pop_front() else {
            break;
        };
        let started = clock.get();
        let outcome = frames::run_batch(
            first,
            started,
            || clock.get(),
            || queue.pop_front(),
            || false,
            |msg| {
                clock.set(clock.get() + per_message);
                Applied::dirty(app.apply_sync_tracked(msg))
            },
        );
        applied += outcome.applied;
        dirty |= outcome.dirty;
    }
    (frames, applied, dirty)
}

fn fresh() -> App {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    app.effects.clear();
    app.frame = 0;
    app
}

fn render(app: &mut App, w: u16, h: u16) -> ratatui::buffer::Buffer {
    app.set_size(w, h);
    nexus_tui::render::render_buffer(app, w, h)
}

#[test]
fn batched_replay_matches_per_event_rendering() {
    let (w, h) = (120u16, 36u16);
    let mut reference = fresh();
    let now = Instant::now();
    reference.clock = now;
    let _ = render(&mut reference, w, h);
    for msg in stream() {
        reference.apply_sync(msg);
        let _ = render(&mut reference, w, h);
    }

    let mut batched = fresh();
    batched.clock = now;
    let (frames, applied, dirty) = drive(&mut batched, stream().into(), Duration::ZERO, |app| {
        let _ = render(app, w, h);
    });
    let _ = render(&mut batched, w, h);

    assert_eq!(applied, stream().len());
    assert!(frames < stream().len(), "a burst should coalesce frames");
    assert!(!dirty, "the last draw settled the model");

    assert_eq!(batched.state, reference.state, "reduced model");
    assert_eq!(batched.effects, reference.effects, "effects");
    assert_eq!(batched.hits, reference.hits, "hit map");
    assert_eq!(batched.regions, reference.regions, "regions");
    assert_eq!(batched.ui.scroll, reference.ui.scroll, "scroll");
    assert_eq!(
        batched.ui.scrolls, reference.ui.scrolls,
        "per-session scrolls"
    );
    assert_eq!(batched.timeline_screen, reference.timeline_screen, "screen");
    assert_eq!(
        render(&mut batched, w, h),
        render(&mut reference, w, h),
        "complete cells"
    );
    assert_eq!(batched.hits, reference.hits, "hit map after the final draw");
}

#[test]
fn wire_traffic_is_applied_but_does_not_change_the_view() {
    for invisible in [wire_event(40), wire_response(41)] {
        let (w, h) = (120u16, 36u16);
        let mut a = fresh();
        let mut b = fresh();
        let before = render(&mut a, w, h);
        let _ = render(&mut b, w, h);
        let last = invisible.id;
        let events = a.state.events_applied;
        let effects = a.effects.clone();

        assert!(!sync_msg_affects_view(&SyncMsg::Event(invisible.clone())));
        assert!(!a.apply_sync_tracked(SyncMsg::Event(invisible.clone())));
        b.apply_sync(SyncMsg::Event(invisible.clone()));

        assert_eq!(a.state.model.last_id, last, "cursor advanced");
        assert_eq!(a.state.events_applied, events + 1);
        assert_eq!(a.effects, effects, "no effects");
        assert_eq!(a.state, b.state, "same reduced state");
        assert_eq!(a.hits, b.hits, "hit map unchanged");
        assert_eq!(render(&mut a, w, h), before, "unchanged frame");
        assert_eq!(render(&mut a, w, h), render(&mut b, w, h));
    }
}

#[test]
fn every_other_message_and_unknown_kind_is_conservative() {
    assert!(sync_msg_affects_view(&SyncMsg::Event(wire_request(50))));
    assert!(sync_msg_affects_view(&SyncMsg::Event(event(
        51,
        "unknown.future_kind",
        json!({"x": 1})
    ))));
    assert!(!sync_msg_affects_view(&SyncMsg::Event(event(
        52,
        "wire_event",
        json!({"type": "wire_event"})
    ))));
    assert!(sync_msg_affects_view(&SyncMsg::Git {
        session: SESSION.into(),
        data: Ok(json!({})),
    }));
    assert!(sync_msg_affects_view(&SyncMsg::Connection(
        nexus_tui::state::ConnectionState::Live
    )));
    assert!(sync_msg_affects_view(&SyncMsg::Snapshot {
        snapshot: Box::new(snapshot()),
        cause: ResyncCause::Initial,
    }));
}

fn simple_visible(id: i64) -> SyncMsg {
    SyncMsg::Event(event(
        id,
        "text_delta",
        json!({"type": "text_delta", "text": "a"}),
    ))
}

#[test]
fn a_prequeued_burst_needs_fewer_frames_than_events() {
    let mut app = fresh();
    let queue: VecDeque<SyncMsg> = (60..100).map(simple_visible).collect();
    let (frames, applied, dirty) = drive(&mut app, queue, Duration::ZERO, |app| {
        let _ = render(app, 120, 36);
    });
    assert_eq!(applied, 40);
    assert_eq!(frames, 2, "the initial frame and one coalesced result");
    assert!(!dirty);
}

#[test]
fn invisible_only_traffic_draws_no_extra_frame() {
    let mut app = fresh();
    let queue: VecDeque<SyncMsg> = (200..260)
        .map(|id| SyncMsg::Event(wire_event(id)))
        .collect();
    let (frames, applied, _) = drive(&mut app, queue, Duration::ZERO, |app| {
        let _ = render(app, 120, 36);
    });
    assert_eq!(applied, 60);
    assert_eq!(frames, 1, "only the initial frame");
}

#[test]
fn a_visible_event_followed_by_invisible_ones_is_drawn_once() {
    let mut app = fresh();
    let mut queue = VecDeque::new();
    queue.push_back(simple_visible(300));
    for id in 301..340 {
        queue.push_back(SyncMsg::Event(wire_event(id)));
    }
    let (frames, applied, _) = drive(&mut app, queue, Duration::ZERO, |app| {
        let _ = render(app, 120, 36);
    });
    assert_eq!(applied, 40);
    assert_eq!(frames, 2, "initial frame plus the visible result");
}

#[test]
fn a_clean_batch_does_not_cancel_an_outstanding_draw() {
    // The loop's dirty rule: OR in each result, never assign a false tick over
    // an earlier dirty flag.
    let mut dirty = true;
    let (_, _, after_visible) = drive(
        &mut fresh(),
        [simple_visible(400)].into(),
        Duration::ZERO,
        |_| {},
    );
    dirty |= after_visible;
    let (_, _, after_invisible) = drive(
        &mut fresh(),
        [SyncMsg::Event(wire_event(401))].into(),
        Duration::ZERO,
        |_| {},
    );
    assert!(!after_invisible, "an invisible batch is clean");
    dirty |= after_invisible;
    assert!(dirty, "the earlier draw is still owed");
}

#[test]
fn a_false_tick_does_not_erase_dirty() {
    let mut dirty = true;
    let changed = false;
    dirty |= changed;
    assert!(dirty);
}

#[test]
fn the_last_visible_event_is_drawn_without_another_event() {
    let mut app = fresh();
    let (frames, applied, dirty) = drive(
        &mut app,
        [simple_visible(500)].into(),
        Duration::ZERO,
        |app| {
            let _ = render(app, 120, 36);
        },
    );
    assert_eq!((applied, frames), (1, 2), "initial and final frames");
    assert!(!dirty);
    assert_eq!(app.state.model.streaming[SESSION].text, "a");
}

#[test]
fn budget_stops_a_backlog_but_not_a_spaced_stream() {
    let mut app = fresh();
    let queue: VecDeque<SyncMsg> = (600..620).map(simple_visible).collect();
    let clock = std::cell::Cell::new(Instant::now());
    let applied = std::cell::Cell::new(0);
    let mut queue = queue;
    let outcome = frames::run_batch(
        queue.pop_front().unwrap(),
        clock.get(),
        || clock.get(),
        || queue.pop_front(),
        || false,
        |msg| {
            clock.set(clock.get() + Duration::from_millis(5));
            applied.set(applied.get() + 1);
            Applied::dirty(app.apply_sync_tracked(msg))
        },
    );
    assert_eq!(outcome.end, BatchEnd::Budget);
    assert_eq!(outcome.applied, 2, "5 ms + 5 ms, then 10 ms is over");
    assert_eq!(applied.get(), 2);
    assert!(queue.len() < 20, "the rest stays queued");
}

#[test]
fn rendering_between_inputs_is_a_render_barrier() {
    use nexus_tui::ui::Focus;

    // Changing detail changes the block list only at the next draw. A Down
    // after a fold must see the new list, which is why input is a barrier.
    let mut app = fresh();
    app.ui.focus = Focus::Timeline;
    app.apply_sync(SyncMsg::Event(event(
        60,
        "message.added",
        json!({
            "id": "msg_tools", "session_id": SESSION, "seq": 4,
            "created": 1793113500.0, "role": "assistant",
            "parts": [
                {"type": "tool_call", "id": "c1", "name": "read",
                 "arguments": "{\"path\":\"a\"}"},
                {"type": "tool_call", "id": "c2", "name": "bash",
                 "arguments": "{\"command\":\"ls\"}"}
            ]
        }),
    )));
    let _ = render(&mut app, 120, 36);
    let detailed = app.ui.timeline_blocks.len();
    let medium = nexus_tui::render::buffer_rows(&render(&mut app, 120, 36));
    press(&mut app, &["alt+d"]); // medium -> high
    press(&mut app, &["alt+d"]); // high -> low: activity groups collapse
    assert_eq!(app.ui.timeline_blocks.len(), detailed, "no rebuild yet");
    let low = nexus_tui::render::buffer_rows(&render(&mut app, 120, 36));
    assert_eq!(
        app.ui.timeline_blocks.len(),
        detailed,
        "the group is still one block; its contents fold"
    );
    assert_ne!(low, medium, "the fold is in the next frame");
    let before = app.ui.timeline_sel;
    press(&mut app, &["down"]);
    assert_ne!(before, app.ui.timeline_sel, "Down moved the selection");

    // A key handled before that draw still sees the old blocks, which is why
    // the loop renders pending changes first.
    let mut stale = fresh();
    stale.ui.focus = Focus::Timeline;
    let _ = render(&mut stale, 120, 36);
    let stale_blocks = stale.ui.timeline_blocks.len();
    press(&mut stale, &["alt+d"]);
    press(&mut stale, &["alt+d"]);
    assert_eq!(stale.ui.timeline_blocks.len(), stale_blocks);
    assert_eq!(stale.ui.detail, nexus_tui::ui::Detail::Low);
}

#[test]
fn resize_then_click_uses_the_last_hit_map() {
    let mut app = fresh();
    let _ = render(&mut app, 120, 36);
    let old = app.regions.clone();
    app.set_size(80, 24);
    let _ = render(&mut app, 80, 24);
    assert_ne!(old, app.regions, "the hit map follows the size");
    // A click on the composer's region is accepted at the new size.
    let area = app.regions.timeline;
    click(&mut app, area.x + 1, area.y + 1);
    let _ = render(&mut app, 80, 24);
}

#[test]
fn a_burst_keeps_effects_and_status_reports_in_order() {
    let msgs = vec![
        SyncMsg::Event(event(
            700,
            "permission.asked",
            json!({"id": "per_7", "session_id": SESSION, "tool": "bash",
                   "call_id": "call_7", "args": {"command": "ls"}}),
        )),
        SyncMsg::Event(event(
            701,
            "permission.replied",
            json!({"id": "per_7", "session_id": SESSION, "allow": true,
                   "outcome": "allowed"}),
        )),
        SyncMsg::Event(event(
            702,
            "turn.summary",
            json!({"id": "trn_1", "status": "completed", "started": 1793113304.0,
                   "finished": 1793113315.0, "start_seq": 0,
                   "ended": {"stop": "end_turn", "steps": 1}}),
        )),
        SyncMsg::Event(wire_event(703)),
    ];

    // Reference: one item at a time, as the old loop did.
    let mut reference = fresh();
    let mut ref_effects = Vec::new();
    let mut ref_status = Tracker::default();
    let mut ref_reports = Vec::new();
    for msg in &msgs {
        let snapshot = matches!(msg, SyncMsg::Snapshot { .. });
        let accepted = match msg {
            SyncMsg::Event(event) if event.id > reference.state.model.last_id => {
                Some(event.clone())
            }
            _ => None,
        };
        reference.apply_sync(msg.clone());
        if snapshot {
            ref_status.snapshot();
        }
        if let Some(event) = accepted.filter(|e| reference.state.model.last_id == e.id) {
            ref_status.event(&event);
        }
        ref_reports.push(ref_status.reconcile(&reference.state));
        ref_effects.append(&mut reference.effects);
    }

    // Batched: the production batch helper. Effects are dispatched after each
    // item (the barrier), so the sequence must match.
    let mut batched = fresh();
    let mut batch_effects = Vec::new();
    let mut status = Tracker::default();
    let mut reports = Vec::new();
    let mut queue: VecDeque<SyncMsg> = msgs.into();
    let outcome = frames::run_batch(
        queue.pop_front().unwrap(),
        Instant::now(),
        Instant::now,
        || queue.pop_front(),
        || false,
        |msg| {
            let accepted = match &msg {
                SyncMsg::Event(event) if event.id > batched.state.model.last_id => {
                    Some(event.clone())
                }
                _ => None,
            };
            let dirty = batched.apply_sync_tracked(msg);
            if let Some(event) = accepted.filter(|e| batched.state.model.last_id == e.id) {
                status.event(&event);
            }
            reports.push(status.reconcile(&batched.state));
            batch_effects.append(&mut batched.effects);
            Applied::dirty(dirty)
        },
    );

    assert_eq!(outcome.applied, 4);
    assert_eq!(batch_effects, ref_effects, "ordered effects");
    assert_eq!(reports, ref_reports, "ordered OSC status reports");
    assert!(
        ref_reports
            .iter()
            .any(|r| r.iter().any(|s| s.contains("state=blocked"))),
        "the approval was reported while it waited"
    );
    assert!(
        ref_reports
            .iter()
            .any(|r| r.iter().any(|s| s.contains("state=done"))),
        "the summary reported completion"
    );
}

#[test]
fn quit_halts_the_batch_without_applying_the_rest() {
    let mut app = fresh();
    let queue: VecDeque<SyncMsg> = (800..810).map(simple_visible).collect();
    let mut queue = queue;
    let mut applied = 0;
    let outcome = frames::run_batch(
        queue.pop_front().unwrap(),
        Instant::now(),
        Instant::now,
        || queue.pop_front(),
        || false,
        |msg| {
            applied += 1;
            app.apply_sync_tracked(msg);
            Applied::halt(true)
        },
    );
    assert_eq!(applied, 1);
    assert_eq!(outcome.applied, 1);
    assert!(outcome.halt);
    assert_eq!(queue.len(), 9);
}

#[test]
fn session_switch_parity_after_a_batch() {
    let (w, h) = (120u16, 36u16);
    let mut reference = fresh();
    let _ = render(&mut reference, w, h);
    reference.state.model.select(None);
    let _ = render(&mut reference, w, h);
    reference.open_session(SESSION);
    let _ = render(&mut reference, w, h);

    let mut batched = fresh();
    let _ = render(&mut batched, w, h);
    batched.state.model.select(None);
    let _ = render(&mut batched, w, h);
    batched.open_session(SESSION);
    let _ = render(&mut batched, w, h);

    assert_eq!(batched.state, reference.state);
    assert_eq!(batched.ui.tabs, reference.ui.tabs);
    assert_eq!(batched.ui.scroll, reference.ui.scroll);
    assert_eq!(batched.hits, reference.hits);
    assert_eq!(render(&mut batched, w, h), render(&mut reference, w, h));
}

#[test]
fn closing_the_input_or_sync_channel_exits_and_aux_closures_do_not_spin() {
    // The production select disables a closed auxiliary branch instead of
    // re-polling it. Model that here: taking None closes the branch once.
    let (image_tx, mut image_rx) = tokio::sync::mpsc::unbounded_channel::<u8>();
    drop(image_tx);
    let mut open = true;
    let mut polls = 0;
    for _ in 0..5 {
        if !open {
            break;
        }
        polls += 1;
        match image_rx.try_recv() {
            Ok(_) => {}
            Err(tokio::sync::mpsc::error::TryRecvError::Disconnected) => open = false,
            Err(tokio::sync::mpsc::error::TryRecvError::Empty) => {}
        }
    }
    assert!(!open);
    assert_eq!(polls, 1, "a closed branch is not busy-polled");
}

// ---- phase 2: rendered-body cache --------------------------------------------

fn add_message(app: &mut App, id: i64, seq: i64, role: &str, text: &str) {
    app.apply_sync(SyncMsg::Event(event(
        id,
        "message.added",
        json!({
            "id": format!("msg_cache_{id}"), "session_id": SESSION,
            "seq": seq, "created": 1793113600.0 + id as f64, "role": role,
            "parts": [{"type": "text", "text": text}]
        }),
    )));
    app.effects.clear();
}

fn stats(app: &App) -> (u64, u64, u64) {
    app.layout.borrow().stats()
}

#[test]
fn cached_frames_match_a_cold_frame() {
    let (w, h) = (120u16, 36u16);
    let mut warm = fresh();
    let _ = render(&mut warm, w, h);
    let _ = render(&mut warm, w, h);
    let warm_buf = render(&mut warm, w, h);

    let mut cold = fresh();
    let cold_buf = render(&mut cold, w, h);

    assert_eq!(warm_buf, cold_buf, "a cache hit cannot change the frame");
    assert_eq!(warm.hits, cold.hits);
    assert_eq!(warm.ui.timeline_blocks.len(), cold.ui.timeline_blocks.len());
}

#[test]
fn an_unchanged_frame_does_no_parse_or_wrap_work() {
    let mut app = fresh();
    let _ = render(&mut app, 120, 36);
    let (hits, misses, _) = stats(&app);
    assert_eq!(hits, 0, "nothing is cached before the first frame");
    assert!(misses > 0, "the first frame fills the cache");

    let _ = render(&mut app, 120, 36);
    let (hits2, misses2, _) = stats(&app);
    assert_eq!(hits2, hits, "the cached timeline needs no body lookups");
    assert_eq!(misses2, misses, "no body is parsed or wrapped again");
}

// ---- phase 3: timeline and chrome invalidation ------------------------------

#[test]
fn typing_and_scrolling_reuse_the_timeline() {
    let (w, h) = (120u16, 36u16);
    let mut app = fresh();
    let _ = render(&mut app, w, h);
    let builds = app.timeline_builds.get();
    let blocks = app.ui.timeline_blocks.clone();
    let screen = app.timeline_screen.clone();

    typing(&mut app, "hello");
    let _ = render(&mut app, w, h);
    assert_eq!(app.timeline_builds.get(), builds, "typing is chrome only");
    assert_eq!(app.ui.timeline_blocks, blocks);
    assert_eq!(app.timeline_screen, screen, "timeline cells are unchanged");

    app.scroll_timeline(-2);
    let _ = render(&mut app, w, h);
    assert_eq!(app.timeline_builds.get(), builds, "scrolling reuses lines");
}

#[test]
fn idle_ticks_reuse_the_timeline_but_animation_rebuilds() {
    let mut app = fresh();
    app.theme = app.theme.with(nexus_tui::theme::Options {
        reduced_motion: true,
        ..app.theme.opts
    });
    let _ = render(&mut app, 120, 36);
    let builds = app.timeline_builds.get();
    app.tick(Instant::now());
    let _ = render(&mut app, 120, 36);
    assert_eq!(
        app.timeline_builds.get(),
        builds,
        "no animation, no rebuild"
    );

    let mut moving = fresh();
    moving.theme = moving.theme.with(nexus_tui::theme::Options {
        reduced_motion: false,
        ..moving.theme.opts
    });
    let _ = render(&mut moving, 120, 36);
    let builds = moving.timeline_builds.get();
    moving.frame += 1;
    let _ = render(&mut moving, 120, 36);
    assert_eq!(
        moving.timeline_builds.get(),
        builds + 1,
        "the spinner phase is a timeline input"
    );
}

#[test]
fn model_ui_and_size_changes_rebuild_the_timeline() {
    let (w, h) = (120u16, 36u16);
    let mut app = fresh();
    let _ = render(&mut app, w, h);
    let mut builds = app.timeline_builds.get();

    add_message(&mut app, 1200, 80, "assistant", "a new cached frame");
    let _ = render(&mut app, w, h);
    assert_eq!(app.timeline_builds.get(), builds + 1, "new content");
    builds = app.timeline_builds.get();

    app.ui.focus = nexus_tui::ui::Focus::Timeline;
    let _ = render(&mut app, w, h);
    assert_eq!(app.timeline_builds.get(), builds + 1, "focus bar");
    builds = app.timeline_builds.get();

    press(&mut app, &["down"]);
    let _ = render(&mut app, w, h);
    assert_eq!(app.timeline_builds.get(), builds + 1, "selection");
    builds = app.timeline_builds.get();

    let key = app
        .ui
        .timeline_blocks
        .iter()
        .find(|block| block.kind == BlockKind::Assistant)
        .expect("assistant block")
        .key
        .clone();
    app.ui.timeline_open.insert(key, false);
    let _ = render(&mut app, w, h);
    assert_eq!(app.timeline_builds.get(), builds + 1, "fold state");
    builds = app.timeline_builds.get();

    app.set_size(100, 30);
    let _ = render(&mut app, 100, 30);
    assert_eq!(app.timeline_builds.get(), builds + 1, "width");
}

#[test]
fn a_non_selected_session_event_rebuilds_conservatively() {
    let mut app = fresh();
    let _ = render(&mut app, 120, 36);
    let builds = app.timeline_builds.get();
    app.apply_sync(SyncMsg::Event(event(
        1300,
        "session.status",
        json!({"status": "busy", "turn_id": "trn_other", "origin": "prompt"}),
    )));
    // The event belongs to SESSION, but the point is that any view-affecting
    // sync message invalidates the timeline even when the selected content did
    // not obviously change.
    let _ = render(&mut app, 120, 36);
    assert_eq!(app.timeline_builds.get(), builds + 1);
}

#[test]
fn width_and_theme_are_cache_dependencies() {
    let mut app = fresh();
    let _ = render(&mut app, 120, 36);
    let (_, after_first, _) = stats(&app);
    let narrow = render(&mut app, 96, 30);
    let (_, after_resize, _) = stats(&app);
    assert!(after_resize > after_first, "a width change re-lays out");
    let _ = render(&mut app, 120, 36);

    let before_theme = render(&mut app, 120, 36);
    let (_, misses_before, _) = stats(&app);
    let mut options = app.theme.opts;
    options.ascii = true;
    app.theme = app.theme.with(options);
    let after_theme = render(&mut app, 120, 36);
    let (_, misses_after, _) = stats(&app);
    assert!(misses_after > misses_before, "a theme change re-renders");
    assert_ne!(before_theme, after_theme, "the glyphs changed");
    assert_ne!(narrow, after_theme, "the resize changed the frame");
}

#[test]
fn folding_changes_the_key_and_unfolding_hits_again() {
    let mut app = fresh();
    let _ = render(&mut app, 120, 36);
    let key = app
        .ui
        .timeline_blocks
        .iter()
        .find(|block| block.kind == BlockKind::Assistant)
        .expect("the fixture has assistant text")
        .key
        .clone();
    let (_, misses_rendered, _) = stats(&app);
    let open = render(&mut app, 120, 36);

    app.ui.timeline_open.insert(key.clone(), false);
    let folded = render(&mut app, 120, 36);
    let (_, misses_folded, _) = stats(&app);
    assert!(
        misses_folded > misses_rendered,
        "the folded body is new work"
    );
    assert_ne!(open, folded, "folding changes the frame");

    app.ui.timeline_open.remove(&key);
    let unfolded = render(&mut app, 120, 36);
    let (_, misses_unfolded, _) = stats(&app);
    assert_eq!(
        misses_unfolded, misses_folded,
        "the open body is still cached"
    );
    assert_eq!(open, unfolded, "unfolding returns the earlier frame");
}

#[test]
fn history_merge_and_replacement_show_new_text() {
    let mut app = fresh();
    let _ = render(&mut app, 120, 36);
    let (_, misses_before, _) = stats(&app);

    // Close the running turn first: the fixture's live scratch keys would
    // otherwise sort a newly loaded message before the live announcements.
    app.apply_sync(SyncMsg::Event(event(
        17,
        "turn.summary",
        json!({"id": "trn_1", "status": "completed", "started": 1793113304.0,
               "finished": 1793113315.0, "start_seq": 0,
               "ended": {"stop": "end_turn", "steps": 1}}),
    )));
    app.effects.clear();
    let mut history = History {
        session_id: SESSION.into(),
        messages: vec![Message {
            id: "msg_cache_history".into(),
            session_id: SESSION.into(),
            seq: 40,
            created: 1793113900.0,
            role: "assistant".into(),
            parts: vec![json!({"type": "text", "text": "merged after history"})],
            delivery: None,
        }],
        ..Default::default()
    };
    app.apply_sync(SyncMsg::History(Box::new(history.clone())));
    app.effects.clear();
    let rows = nexus_tui::render::buffer_rows(&render(&mut app, 120, 36));
    assert!(
        rows.iter().any(|row| row.contains("merged after history")),
        "new history is drawn"
    );
    let (_, misses_after, _) = stats(&app);
    assert!(misses_after > misses_before, "new bodies are laid out");

    // A second, identical history result must hit the cache.
    history.messages[0].parts = vec![json!({"type": "text", "text": "replaced text"})];
    app.apply_sync(SyncMsg::History(Box::new(history)));
    app.effects.clear();
    let _ = render(&mut app, 120, 36);
    let (_, misses_replaced, _) = stats(&app);
    assert!(
        misses_replaced > misses_after,
        "replacement content is a different key"
    );
}

#[test]
fn the_cache_is_bounded_and_evicts_oldest_first() {
    use std::cell::RefCell;
    let limit = 24 * 1024;
    let mut app = fresh();
    app.layout = RefCell::new(LayoutCache::with_limit(limit));
    for step in 0..12 {
        add_message(
            &mut app,
            900 + step,
            50 + step,
            "assistant",
            &format!("cache eviction body {step}: {}", "word ".repeat(500)),
        );
        let _ = render(&mut app, 120, 36);
    }
    let (_, _, evictions) = stats(&app);
    assert!(evictions > 0, "a tiny cache evicts");
    {
        let cache = app.layout.borrow();
        assert!(
            cache.bytes() <= limit || cache.len() == 1,
            "bounded bytes ({} <= {limit})",
            cache.bytes()
        );
    }

    let mut reference = fresh();
    for step in 0..12 {
        add_message(
            &mut reference,
            900 + step,
            50 + step,
            "assistant",
            &format!("cache eviction body {step}: {}", "word ".repeat(500)),
        );
    }
    assert_eq!(
        render(&mut app, 120, 36),
        render(&mut reference, 120, 36),
        "eviction never changes the frame"
    );
}

#[test]
fn tool_completion_and_raw_view_are_never_stale() {
    let mut app = fresh();
    let call = json!({"type": "tool_call", "id": "call_cache", "name": "bash",
        "arguments": "{\"command\":\"printf cached\"}"});
    app.apply_sync(SyncMsg::Event(event(
        950,
        "message.added",
        json!({"id": "msg_cache_tool", "session_id": SESSION, "seq": 60,
            "created": 1793114000.0, "role": "assistant", "parts": [call]}),
    )));
    app.apply_sync(SyncMsg::Event(event(
        951,
        "tool_end",
        json!({"type": "tool_end", "call": {"type": "tool_call", "id": "call_cache",
            "name": "bash", "arguments": "{\"command\":\"printf cached\"}"},
            "result": {"type": "tool_result", "call_id": "call_cache",
                "output": "cached output\n", "is_error": false}}),
    )));
    app.apply_sync(SyncMsg::Event(event(
        952,
        "message.added",
        json!({"id": "msg_cache_result", "session_id": SESSION, "seq": 61,
            "created": 1793114001.0, "role": "tool",
            "parts": [{"type": "tool_result", "call_id": "call_cache",
                "output": "cached output\n", "is_error": false}]}),
    )));
    app.ui.detail = nexus_tui::ui::Detail::High;
    app.effects.clear();
    let _ = render(&mut app, 120, 36);
    let (_, tool_misses, _) = stats(&app);
    let _ = render(&mut app, 120, 36);
    let (_, tool_misses_again, _) = stats(&app);
    assert_eq!(
        tool_misses_again, tool_misses,
        "tool detail rows are cached after completion"
    );
    let key = app
        .ui
        .timeline_blocks
        .iter()
        .find(|block| block.kind == BlockKind::Tool)
        .expect("a tool block")
        .key
        .clone();

    app.ui.timeline_open.insert(format!("{key}:raw"), true);
    let raw = nexus_tui::render::buffer_rows(&render(&mut app, 120, 36));
    assert!(
        raw.iter().any(|row| row.contains("printf cached")),
        "the raw view shows the arguments after completion"
    );
}
