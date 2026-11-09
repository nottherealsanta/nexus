mod common;
use common::*;
use nexus_tui::actions::{ActionDone, ApiCall, Done};
use nexus_tui::app::{App, Effect};
use nexus_tui::inspector::Inspector;
use nexus_tui::theme::{Mode, Tier};
use nexus_tui::ui::OverlayKind;
use serde_json::Value;
use std::time::{Duration, Instant};

fn file() -> Value {
    serde_json::from_str(include_str!("../../interface/scenarios/inspector.json")).unwrap()
}
fn scenario(name: &str) -> Value {
    file()[name].clone()
}
fn query(call: &ApiCall) -> Vec<(String, String)> {
    match call {
        ApiCall::Inspector { query, .. } => query.clone(),
        other => panic!("not an inspector call: {other:?}"),
    }
}
fn has(q: &[(String, String)], key: &str, value: &str) -> bool {
    q.iter().any(|(k, v)| k == key && v == value)
}
/// Every inspector request the app has queued since the last call.
fn calls(app: &mut App) -> Vec<ApiCall> {
    let out = app
        .effects
        .iter()
        .filter_map(|e| match e {
            Effect::Api(
                c @ (ApiCall::Inspector { .. }
                | ApiCall::InspectorHead { .. }
                | ApiCall::InspectorRecord { .. }),
            ) => Some(c.clone()),
            _ => None,
        })
        .collect();
    app.effects.clear();
    out
}
fn answer(app: &mut App, call: &ApiCall, body: Value) {
    app.apply_done(ActionDone {
        call: call.clone(),
        result: Ok(Done {
            inspection: Some(body),
            ..Done::default()
        }),
    });
}
fn fail(app: &mut App, call: &ApiCall) {
    app.apply_done(ActionDone {
        call: call.clone(),
        result: Err("offline".into()),
    });
}
fn open() -> App {
    let mut app = with_turn(opts(Mode::Dark, Tier::TrueColor), (160, 50));
    app.run_command("session.open_inspector");
    app
}
fn shown(app: &mut App) -> String {
    draw(app, 160, 50).1.join("\n")
}
fn inspector(app: &App) -> &Inspector {
    match app.ui.overlay.as_ref().map(|o| &o.kind) {
        Some(OverlayKind::Inspector(i)) => i,
        _ => panic!("inspector is not open"),
    }
}
/// Open on Timing with the scenario applied.
fn ready() -> App {
    let mut app = open();
    let call = calls(&mut app).remove(0);
    answer(&mut app, &call, scenario("timing"));
    app
}

#[test]
fn terminal_resource_lanes_share_axis_and_diagnostic_details() {
    let fixture: Value = serde_json::from_str(include_str!(
        "../../interface/scenarios/inspector-terminals.json"
    ))
    .unwrap();
    let mut data = scenario("timing");
    data["lanes"] = fixture["lanes"].clone();
    let mut app = open();
    let call = calls(&mut app).remove(0);
    answer(&mut app, &call, data);
    let text = shown(&mut app);
    for fact in fixture["expect"].as_array().unwrap() {
        assert!(text.contains(fact.as_str().unwrap()), "{text}");
    }
    assert!(text.contains("~ terminal"));
    press(&mut app, &["j", "enter"]);
    let expanded = shown(&mut app);
    assert!(expanded.contains("diagnostic_only"), "{expanded}");
    assert!(nexus_tui::inspector::kind_options(0).contains(&"terminal"));
    if let Ok(dir) = std::env::var("NEXUS_INSPECTOR_SHOTS") {
        let buffer = draw(&mut app, 160, 50).0;
        let (fg, bg) = nexus_tui::svg::page_colors(false);
        std::fs::write(
            format!("{dir}/tui-terminals.svg"),
            nexus_tui::svg::to_svg(&buffer, fg, bg),
        )
        .unwrap();
    }
}

#[test]
fn opens_with_v2_query_and_shows_cache_lifetime_and_page_separately() {
    let mut app = open();
    let first = calls(&mut app);
    assert!(!app.effects.iter().any(|e| matches!(e, Effect::OpenUrl(_))));
    let q = query(&first[0]);
    assert!(has(&q, "view", "Timing") && has(&q, "limit", "50"));
    assert!(!q.iter().any(|(k, _)| k == "page" || k == "before"));
    answer(&mut app, &first[0], scenario("timing"));
    let text = shown(&mut app);
    for fact in [
        "Session inspector",
        "Session lifetime · Input 12,000 · Cached 8,400 · Uncached 3,600",
        "Hit 70% of input tokens reported by provider · Writes unknown",
        "11 of 12 requests reported usage (partial)",
        "scripted/m · 11 requests",
        "Displayed page · Input 1,000 · Cached 750",
        "1 of 1 requests reported usage",
        "Scout",
        "bash",
        "Live",
    ] {
        assert!(text.contains(fact), "missing {fact}: {text}");
    }
    press(&mut app, &["esc"]);
    assert!(app.ui.overlay.is_none());
}

#[test]
fn late_responses_after_navigation_are_ignored() {
    let mut app = ready();
    press(&mut app, &["2"]);
    let stale_for_view = calls(&mut app).remove(0);
    press(&mut app, &["3"]);
    let latest = calls(&mut app).remove(0);
    answer(&mut app, &stale_for_view, scenario("conversation"));
    assert!(
        inspector(&app).loading,
        "a stale answer must not finish loading"
    );
    assert!(inspector(&app).data().is_null());
    answer(&mut app, &latest, scenario("steps"));
    assert!(!inspector(&app).loading);
    assert_eq!(inspector(&app).data()["view"], "Steps");
    // Closing drops a response that arrives later.
    press(&mut app, &["r"]);
    let pending = calls(&mut app).remove(0);
    press(&mut app, &["esc"]);
    answer(&mut app, &pending, scenario("steps"));
    assert!(app.ui.overlay.is_none());
}

#[test]
fn cursor_paging_keeps_selection_expansion_and_views() {
    let mut app = ready();
    press(&mut app, &["2"]);
    let call = calls(&mut app).remove(0);
    answer(&mut app, &call, scenario("conversation"));
    press(&mut app, &["j", "enter"]);
    let text = shown(&mut app);
    assert!(text.contains("\"role\": \"assistant\"") || text.contains("Reading the files first"));
    assert!(text.contains("Clipped · showing the first part of 30.5 KB"));
    assert!(text.contains("Omitted · detail ×1"));
    // Older uses the cursor, never a page number.
    press(&mut app, &["n"]);
    let older = calls(&mut app).remove(0);
    let q = query(&older);
    assert!(has(&q, "before", "WyJjIiwxXQ"), "{q:?}");
    assert!(!q.iter().any(|(k, _)| k == "page"));
    answer(&mut app, &older, scenario("conversation_older"));
    let text = shown(&mut app);
    assert!(text.contains("Older records") && text.contains("oldest retained"));
    assert!(text.contains("Paused · viewing older records"));
    press(&mut app, &["p"]);
    let newer = calls(&mut app).remove(0);
    assert!(!query(&newer).iter().any(|(k, _)| k == "before"));
    answer(&mut app, &newer, scenario("conversation"));
    // Selection and expansion survive paging back, a refresh and another view.
    press(&mut app, &["r"]);
    let refresh = calls(&mut app).remove(0);
    answer(&mut app, &refresh, scenario("conversation"));
    press(&mut app, &["1"]);
    let t = calls(&mut app).remove(0);
    answer(&mut app, &t, scenario("timing"));
    press(&mut app, &["2"]);
    let c = calls(&mut app).remove(0);
    answer(&mut app, &c, scenario("conversation"));
    let i = inspector(&app);
    assert_eq!(i.page().selected.as_deref(), Some("msg_2"));
    assert!(i.page().expanded.contains("msg_2"));
}

#[test]
fn filters_tree_selector_and_full_record() {
    let mut app = ready();
    press(&mut app, &["2"]);
    let call = calls(&mut app).remove(0);
    answer(&mut app, &call, scenario("conversation"));
    press(&mut app, &["/"]);
    typing(&mut app, "bash");
    assert!(shown(&mut app).contains("Search: bash"));
    press(&mut app, &["enter"]);
    let filtered = calls(&mut app).remove(0);
    assert!(has(&query(&filtered), "q", "bash"));
    answer(&mut app, &filtered, scenario("conversation_filtered"));
    assert!(shown(&mut app).contains("search \"bash\""));
    press(&mut app, &["f"]);
    let kind = calls(&mut app).remove(0);
    assert!(has(&query(&kind), "kind", "user") && has(&query(&kind), "q", "bash"));
    press(&mut app, &["x"]);
    let cleared = calls(&mut app).remove(0);
    assert!(!query(&cleared).iter().any(|(k, _)| k == "q" || k == "kind"));
    answer(&mut app, &cleared, scenario("conversation"));
    // Full record on demand.
    press(&mut app, &["j", "enter", "e"]);
    let record = calls(&mut app).remove(0);
    assert!(
        matches!(&record, ApiCall::InspectorRecord { kind, id, .. } if kind == "message" && id == "msg_2")
    );
    answer(&mut app, &record, scenario("record"));
    let text = shown(&mut app);
    assert!(
        text.contains("FULL RECORD TEXT") && text.contains("Full record loaded"),
        "{text}"
    );
    // Inspect the child without switching the chat session.
    press(&mut app, &["]"]);
    let child = calls(&mut app).remove(0);
    assert!(has(&query(&child), "session", "ses_child"));
    assert_eq!(inspector(&app).session, common::SESSION);
    press(&mut app, &["6"]);
    let q = calls(&mut app).remove(0);
    assert!(has(&query(&q), "view", "Questions"));
    answer(&mut app, &q, scenario("questions"));
    let text = shown(&mut app);
    assert!(text.contains("question pending") && text.contains("question replied"));
}

#[test]
fn live_state_checks_head_after_invalidation_and_refetches_only_on_change() {
    let mut app = ready();
    let t0 = Instant::now();
    let invalidate = |app: &mut App, at: Instant| {
        if let Some(OverlayKind::Inspector(inspector)) =
            app.ui.overlay.as_mut().map(|overlay| &mut overlay.kind)
        {
            inspector.invalidate(at);
        }
    };
    app.tick(t0 + Duration::from_secs(1));
    assert!(
        calls(&mut app).is_empty(),
        "no periodic head poll without a session change"
    );
    invalidate(&mut app, t0 + Duration::from_secs(2));
    app.tick(t0 + Duration::from_secs(3));
    let heads = calls(&mut app);
    assert!(matches!(heads[0], ApiCall::InspectorHead { .. }));
    // No second poll is queued while one is in flight.
    app.tick(t0 + Duration::from_secs(6));
    assert!(calls(&mut app).is_empty());
    // Unchanged head: no refetch.
    answer(&mut app, &heads[0], scenario("head"));
    assert!(calls(&mut app).is_empty());
    assert!(shown(&mut app).contains("Live · updated"));
    // Changed head: exactly one refetch.
    invalidate(&mut app, t0 + Duration::from_secs(11));
    app.tick(t0 + Duration::from_secs(12));
    let heads = calls(&mut app);
    answer(&mut app, &heads[0], scenario("head_changed"));
    let refetch = calls(&mut app);
    assert_eq!(refetch.len(), 1);
    assert!(matches!(refetch[0], ApiCall::Inspector { .. }));
    // A new log generation resets held cursors and reloads the newest page.
    answer(&mut app, &refetch[0], scenario("timing"));
    press(&mut app, &["n"]);
    let older = calls(&mut app).remove(0);
    answer(&mut app, &older, scenario("timing"));
    invalidate(&mut app, t0 + Duration::from_secs(19));
    app.tick(t0 + Duration::from_secs(20));
    let heads = calls(&mut app);
    let mut reset = scenario("head");
    reset["generation"] = "g_new".into();
    answer(&mut app, &heads[0], reset);
    let refetch = calls(&mut app).remove(0);
    assert!(!query(&refetch).iter().any(|(k, _)| k == "before"));
    // Disconnected never looks current; Refresh stays available.
    answer(&mut app, &refetch, scenario("timing"));
    invalidate(&mut app, t0 + Duration::from_secs(29));
    app.tick(t0 + Duration::from_secs(30));
    let heads = calls(&mut app);
    fail(&mut app, &heads[0]);
    let text = shown(&mut app);
    assert!(
        text.contains("Disconnected") && text.contains("not current"),
        "{text}"
    );
    assert!(!text.contains("● Live"));
    // Pausing stops polling; nothing is polled once closed.
    press(&mut app, &["r"]);
    let r = calls(&mut app).remove(0);
    answer(&mut app, &r, scenario("timing"));
    press(&mut app, &["w"]);
    assert!(shown(&mut app).contains("Paused"));
    app.tick(t0 + Duration::from_secs(60));
    assert!(calls(&mut app).is_empty());
    press(&mut app, &["w"]);
    calls(&mut app);
    press(&mut app, &["esc"]);
    app.tick(t0 + Duration::from_secs(90));
    assert!(calls(&mut app).is_empty());
}

#[test]
fn shared_gantt_axis_ruler_zoom_fit_ttfb_and_unknown_end() {
    let mut i = Inspector::new("ses_inspector".into());
    i.loading = false;
    i.set_data(scenario("timing"), 100.0);
    let text = i.text(110);
    assert!(text.contains("+0.00s") && text.contains("+5.00s") && text.contains("+10.00s"));
    assert!(text.contains("1× · view +0.00s to +10.00s of 10.00s"));
    assert!(text.contains("┼"));
    assert!(text.contains("Review · 3 operations") && text.contains("Scout · 2 operations"));
    assert!(text.contains("######"), "model bar: {text}");
    assert!(text.contains('|') && text.contains("first byte"));
    assert!(text.contains("end unknown") && text.contains("live"));
    // Zoom and pan keep one axis.
    i.zoom(true);
    i.pan(true);
    assert!(i.text(110).contains("2× · view +0.50s to +5.50s"));
    for _ in 0..10 {
        i.pan(true);
    }
    assert!(i.text(110).contains("view +5.00s to +10.00s"));
    // Fit selects the chosen operation, a second fit returns to the whole range.
    i.fit();
    assert!(i.text(110).contains("1× · view +0.00s"));
    i.select(1);
    i.fit();
    assert!(
        i.text(110).contains("5× ") || i.text(110).contains("4× "),
        "{}",
        i.text(110)
    );
    i.fit();
    assert!(i.text(110).contains("1× · view +0.00s"));
    // Selection is by id: rows arriving before it do not move it.
    let mut data = scenario("timing");
    let extra = data["lanes"][0].clone();
    let mut extra = extra;
    extra["id"] = "ses_inspector:turn:turn_0".into();
    data["lanes"].as_array_mut().unwrap().insert(0, extra);
    i.select(0);
    let selected = i.page().selected.clone();
    i.set_data(data, 101.0);
    assert_eq!(i.page().selected, selected);
    assert_eq!(i.items_len_for_test(), 6);
}

#[test]
fn unknown_cache_without_data_and_long_lists_are_windowed() {
    let mut i = Inspector::new("ses_inspector".into());
    i.loading = false;
    assert!(i.text(60).contains("Cached unknown"));
    let mut data = scenario("conversation");
    let rows: Vec<Value> = (0..300)
        .map(|n| serde_json::json!({"kind":"message","id":format!("m{n}"),"summary":format!("row {n}"),"detail":"{}","detail_truncated":false}))
        .collect();
    data["records"] = rows.into();
    i.view = 1;
    i.set_data(data, 1.0);
    let text = i.text(90);
    assert!(
        text.contains("row 0") && !text.contains("row 100"),
        "only a window is drawn"
    );
    assert!(text.contains("rows below"));
    for _ in 0..150 {
        i.select(1);
    }
    let text = i.text(90);
    assert!(text.contains("row 150") && text.contains("rows above") && !text.contains("row 0\n"));
}

#[test]
fn screenshots_when_requested() {
    let Ok(dir) = std::env::var("NEXUS_INSPECTOR_SHOTS") else {
        return;
    };
    let shot = |app: &mut App, name: &str, w: u16, h: u16| {
        let buffer = draw(app, w, h).0;
        let (fg, bg) = nexus_tui::svg::page_colors(false);
        std::fs::write(
            format!("{dir}/{name}.svg"),
            nexus_tui::svg::to_svg(&buffer, fg, bg),
        )
        .unwrap();
    };
    let mut app = ready();
    press(&mut app, &["j", "j", "enter"]);
    shot(&mut app, "tui-timing", 160, 50);
    press(&mut app, &["2"]);
    let call = calls(&mut app).remove(0);
    answer(&mut app, &call, scenario("conversation"));
    press(&mut app, &["j", "enter"]);
    shot(&mut app, "tui-conversation", 160, 50);
    press(&mut app, &["6"]);
    let call = calls(&mut app).remove(0);
    answer(&mut app, &call, scenario("questions"));
    press(&mut app, &["enter"]);
    shot(&mut app, "tui-questions", 160, 50);
    press(&mut app, &["1"]);
    let call = calls(&mut app).remove(0);
    answer(&mut app, &call, scenario("timing"));
    shot(&mut app, "tui-narrow", 80, 40);
}

/// Where `needle` is drawn: (column, row).
fn find(app: &mut App, w: u16, h: u16, needle: &str) -> (u16, u16) {
    let lines = draw(app, w, h).1;
    for (y, line) in lines.iter().enumerate() {
        if let Some(at) = line.find(needle) {
            return (line[..at].chars().count() as u16, y as u16);
        }
    }
    panic!("{needle} is not on screen");
}

#[test]
fn clicking_rows_expands_them_and_clicking_the_tree_picks_the_session() {
    let mut app = ready();
    press(&mut app, &["2"]);
    let call = calls(&mut app).remove(0);
    answer(&mut app, &call, scenario("conversation"));
    // A click selects the row and toggles its details.
    let (x, y) = find(&mut app, 160, 50, "assistant · 3 parts");
    click(&mut app, x + 2, y);
    let i = inspector(&app);
    assert_eq!(i.page().selected.as_deref(), Some("msg_2"));
    assert!(i.page().expanded.contains("msg_2"));
    click(&mut app, x + 2, y);
    assert!(!inspector(&app).page().expanded.contains("msg_2"));
    // A click on a session in the tree inspects it, as `]` does.
    let (x, y) = find(&mut app, 160, 50, "Scout running");
    click(&mut app, x + 1, y);
    let child = calls(&mut app).remove(0);
    assert!(has(&query(&child), "session", "ses_child"));
    // At a narrow width the entries wrap and are still clickable.
    let mut narrow = with_turn(opts(Mode::Dark, Tier::TrueColor), (60, 40));
    narrow.run_command("session.open_inspector");
    let call = calls(&mut narrow).remove(0);
    answer(&mut narrow, &call, scenario("timing"));
    let (x, y) = find(&mut narrow, 60, 40, "Scout running");
    click(&mut narrow, x + 1, y);
    assert!(has(
        &query(&calls(&mut narrow).remove(0)),
        "session",
        "ses_child"
    ));
}
