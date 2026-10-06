use nexus_widgets::components::*;
use nexus_widgets::focus::{escape, Escape, EscapeState, Typeahead};
use nexus_widgets::theme::Level;
use nexus_widgets::*;
use ratatui::{buffer::Buffer, layout::Rect};
use std::time::{Duration, Instant};
use unicode_width::UnicodeWidthStr;

fn text(buf: &Buffer, y: u16) -> String {
    (0..buf.area.width).map(|x| buf[(x, y)].symbol().to_string()).collect::<String>()
}

#[test]
fn glyphs_are_single_width() {
    for g in [Glyphs::unicode(), Glyphs::ascii()] {
        for s in g.singles() {
            assert!(UnicodeWidthStr::width(s) <= 3, "{s}");
        }
        for s in [g.focus_bar, g.caret, g.close, g.dot_ok, g.dot_work, g.dot_idle, g.dot_err, g.open, g.closed, g.search, g.bar_full, g.bar_empty, g.rule, g.tab_rule] {
            assert_eq!(UnicodeWidthStr::width(s), 1, "{s:?} must be one cell");
        }
    }
}

#[test]
fn truncate_announces_clipping() {
    assert_eq!(truncate("hello", 10, "…"), "hello");
    assert_eq!(truncate("hello world", 6, "…"), "hello…");
    assert!(width(&truncate("東京東京東京", 7, "…")) <= 7);
}

#[test]
fn toggle_shows_state_in_text_not_only_colour() {
    let (th, g) = (Theme::mono(), Glyphs::unicode());
    for (on, locked, want) in [(true, false, "ON"), (false, false, "OFF"), (true, true, "LOCKED")] {
        let mut ui = Ui::new(&th, &g);
        let mut buf = Buffer::empty(Rect::new(0, 0, 40, 1));
        ui.begin_frame();
        toggle(&mut buf, &mut ui, Rect::new(0, 0, 40, 1), &Toggle { id: "t", label: "Voice input", on, locked: locked.then_some("why") });
        let row = text(&buf, 0);
        assert!(row.contains(want), "{row}");
        assert!(row.contains("Voice input"));
    }
}

#[test]
fn focus_moves_clamps_and_repairs() {
    let mut f = FocusRing::new();
    f.begin_frame();
    for id in ["a", "b", "c"] {
        f.register(id);
    }
    f.end_frame();
    assert_eq!(f.current(), Some("a"));
    f.move_by(1);
    f.move_by(1);
    f.move_by(1);
    assert_eq!(f.current(), Some("c"), "arrow movement clamps");
    f.cycle(1);
    assert_eq!(f.current(), Some("a"), "tab wraps");
    f.set("b");
    // `b` disappears: focus lands on its nearest surviving neighbour, not index 0.
    f.begin_frame();
    for id in ["a", "c"] {
        f.register(id);
    }
    f.end_frame();
    assert_eq!(f.current(), Some("c"));
}

#[test]
fn overlay_restores_opener_and_regions_remember() {
    let mut f = FocusRing::new();
    f.begin_frame();
    f.register("composer");
    f.register("row");
    f.end_frame();
    f.set("row");
    f.push_overlay();
    f.set("inside");
    assert_eq!(f.overlay_depth(), 1);
    f.pop_overlay();
    assert_eq!(f.current(), Some("row"));
    f.enter_region(Region::Sessions);
    f.set("session:2");
    f.set("x");
    f.enter_region(Region::Composer);
    f.enter_region(Region::Sessions);
    assert_eq!(f.current(), Some("x"), "region memory");
}

#[test]
fn escape_ladder_is_least_destructive_first() {
    let all = EscapeState { popup_open: true, search_nonempty: true, search_focused: true, inner_focused: true, overlay_open: true, in_sidebar: true };
    let mut s = all;
    let mut seen = vec![];
    loop {
        let e = escape(s);
        seen.push(e);
        match e {
            Escape::ClosePopup => s.popup_open = false,
            Escape::ClearSearch => s.search_nonempty = false,
            Escape::LeaveSearch => s.search_focused = false,
            Escape::LeaveInner => s.inner_focused = false,
            Escape::CloseOverlay => s.overlay_open = false,
            Escape::SidebarToComposer => s.in_sidebar = false,
            Escape::Composer => break,
        }
    }
    assert_eq!(seen, vec![Escape::ClosePopup, Escape::ClearSearch, Escape::LeaveSearch, Escape::LeaveInner, Escape::CloseOverlay, Escape::SidebarToComposer, Escape::Composer]);
}

#[test]
fn typeahead_resets_after_800ms() {
    let mut t = Typeahead::default();
    let t0 = Instant::now();
    t.push('o', t0);
    assert_eq!(t.find(&["anthropic", "openai", "opencode"], 0), Some(1));
    t.push('p', t0 + Duration::from_millis(300));
    assert_eq!(t.find(&["anthropic", "openai", "opencode"], 0), Some(1));
    t.push('e', t0 + Duration::from_millis(2000));
    assert_eq!(t.find(&["anthropic", "openai", "opencode"], 0), None, "buffer restarted at 'e'");
}

#[test]
fn toasts_expire_pause_dedup_and_cap() {
    let mut s = ToastStack::default();
    let t0 = Instant::now();
    s.tick(t0, None);
    let a = s.push(Level::Success, "Saved", "", "", "");
    let b = s.push(Level::Error, "Boom", "", "", "");
    s.tick(t0 + Duration::from_secs(4), Some(a));
    assert_eq!(s.toasts.len(), 2, "hovered success is paused; error lives 12 s");
    s.tick(t0 + Duration::from_secs(8), None);
    assert_eq!(s.toasts.len(), 1, "success expired after 3 s unpaused");
    assert_eq!(s.toasts[0].id, b);
    s.dismiss_all();
    s.push(Level::Info, "Copied", "", "copy", "");
    s.push(Level::Info, "Copied", "", "copy", "");
    assert_eq!(s.toasts.len(), 1);
    assert_eq!(s.toasts[0].count, 2);
    for i in 0..6 {
        s.push(Level::Info, &format!("n{i}"), "", "", "");
    }
    assert_eq!(s.visible().count(), MAX_VISIBLE);
    assert_eq!(s.hidden(), s.toasts.len() - MAX_VISIBLE);
}

#[test]
fn toast_close_button_is_a_hit_target() {
    let (th, g) = (Theme::dark(), Glyphs::unicode());
    let mut ui = Ui::new(&th, &g);
    let mut s = ToastStack::default();
    let id = s.push(Level::Error, "Could not reach OpenAI", "HTTP 503", "", "Details");
    let area = Rect::new(0, 1, 120, 30);
    let mut buf = Buffer::empty(Rect::new(0, 0, 120, 32));
    ui.begin_frame();
    let rects = toast_stack(&mut buf, &mut ui, area, &s);
    assert_eq!(rects.len(), 1);
    let r = rects[0].1;
    let x = r.x + r.width - 3;
    let (hit, part) = ui.hits.at(x, r.y).unwrap();
    assert_eq!(hit, format!("toast:{id}"));
    assert_eq!(*part, nexus_widgets::hit::Part::Named("close".into()));
    assert!(text(&buf, r.y).contains("×"));
}

#[test]
fn ordered_list_marks_in_use_and_registers_every_stop() {
    let (th, g) = (Theme::dark(), Glyphs::unicode());
    let mut ui = Ui::new(&th, &g);
    let mut buf = Buffer::empty(Rect::new(0, 0, 90, 6));
    ui.begin_frame();
    let items = [OrderedItem { label: "anthropic/claude-haiku-4-5", note: "" }, OrderedItem { label: "google/gemini-3-flash", note: "not connected" }];
    let h = ordered_list(&mut buf, &mut ui, Rect::new(0, 0, 90, 6), "tier:low", &items, "Add model…");
    assert_eq!(h, 3);
    assert!(text(&buf, 0).contains("in use"));
    assert!(text(&buf, 1).contains("fallback") && text(&buf, 1).contains("not connected"));
    ui.end_frame();
    assert_eq!(ui.focus.order(), ["tier:low:0", "tier:low:1", "tier:low:add"]);
}

#[test]
fn setting_row_shows_label_control_scope_and_description() {
    let (th, g) = (Theme::dark(), Glyphs::unicode());
    let mut ui = Ui::new(&th, &g);
    let mut buf = Buffer::empty(Rect::new(0, 0, 100, 2));
    ui.begin_frame();
    let h = setting_row(&mut buf, &mut ui, Rect::new(0, 0, 100, 2), "setting:voice:auto", "Send transcript automatically", "Off lets you review it.", "global", TOGGLE_W, |b, ui, r, f| toggle_view(b, ui, r.x, r.y, false, false, f));
    assert_eq!(h, 2);
    let row = text(&buf, 0);
    assert!(row.contains("Send transcript automatically") && row.contains("OFF") && row.contains("global"), "{row}");
    assert!(text(&buf, 1).contains("Off lets you review it."));
}

#[test]
fn nothing_is_drawn_outside_the_buffer() {
    // Every component clipped into a tiny buffer must not panic.
    let (th, g) = (Theme::light(), Glyphs::ascii());
    let mut ui = Ui::new(&th, &g);
    let mut buf = Buffer::empty(Rect::new(0, 0, 12, 3));
    ui.begin_frame();
    button(&mut buf, &mut ui, 8, 2, &Button::new("b", "A very long label", ButtonKind::Primary));
    segmented(&mut buf, &mut ui, 0, 0, "s", &["Dark", "Light", "System"], 1);
    select(&mut buf, &mut ui, 0, 1, 12, "sel", "a-very-long-selected-value");
    toast_stack(&mut buf, &mut ui, Rect::new(0, 0, 12, 3), &ToastStack::default());
    key_hints(&mut buf, &ui, Rect::new(0, 2, 12, 1), &[("Enter", "open"), ("Esc", "close")]);
}

#[test]
fn scroll_container_keeps_focus_visible_and_translates_hits() {
    let (th, g) = (Theme::dark(), Glyphs::unicode());
    let mut ui = Ui::new(&th, &g);
    let mut buf = Buffer::empty(Rect::new(0, 0, 40, 12));
    let mut sc = Scroll::default();
    ui.focus.set("row:30");
    for _ in 0..2 {
        ui.begin_frame();
        scrolled(&mut buf, &mut ui, Rect::new(2, 1, 38, 8), &mut sc, |b, ui, r| {
            for i in 0..40u16 {
                let id = format!("row:{i}");
                ui.stop(&id, Rect::new(0, i, r.width, 1));
                put(b, 0, i, &format!("line {i}"), ui.theme.text_style(), 20);
            }
            40
        });
        ui.end_frame();
    }
    assert!(sc.offset >= 23 && sc.offset <= 30, "focused row scrolled into view, offset {}", sc.offset);
    let rect = ui.hits.rect_of("row:30").expect("focused row has a hit rect inside the viewport");
    assert!(rect.y >= 1 && rect.y < 9, "hit translated to screen coordinates: {rect:?}");
    assert!(ui.hits.rect_of("row:0").is_none(), "hits outside the viewport are dropped");
    let drawn: String = (1..9).map(|y| text(&buf, y)).collect();
    assert!(drawn.contains("line 30"));
}

#[test]
fn toast_layout_matches_what_is_drawn_and_ingest_ignores_seen_ids() {
    let (th, g) = (Theme::dark(), Glyphs::unicode());
    for width in [80u16, 120, 200] {
        let mut ui = Ui::new(&th, &g);
        let mut s = ToastStack::default();
        assert!(s.ingest(10, Level::Info, "a", "", "", ""));
        assert!(s.ingest(11, Level::Error, "b", "body", "", "Details"));
        assert!(!s.ingest(11, Level::Error, "again", "", "", ""), "already shown");
        assert!(!s.ingest(3, Level::Info, "old", "", "", ""), "older than the last id");
        let area = Rect::new(0, 2, width, 30);
        let mut buf = Buffer::empty(Rect::new(0, 0, width, 34));
        ui.begin_frame();
        let drawn = toast_stack(&mut buf, &mut ui, area, &s);
        let laid = toast_layout(area, &s);
        assert_eq!(drawn.len(), laid.len());
        for (d, l) in drawn.iter().zip(&laid) {
            assert_eq!((d.0, d.1), (l.id, l.rect), "width {width}");
            assert!(l.close.x >= l.rect.x && l.close.x + l.close.width <= l.rect.x + l.rect.width);
        }
        assert!(laid[0].action.is_some(), "newest toast carries the action");
    }
}
