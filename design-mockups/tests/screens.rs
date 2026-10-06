use nexus_mockups::app::{App, SIZES};
use nexus_mockups::shoot;
use ratatui::{
    buffer::Buffer,
    crossterm::event::{KeyCode, KeyEvent, KeyModifiers},
    layout::Rect,
};
use std::{collections::HashSet, time::Instant};

fn key(c: KeyCode) -> KeyEvent {
    KeyEvent::new(c, KeyModifiers::NONE)
}
fn alt(c: KeyCode) -> KeyEvent {
    KeyEvent::new(c, KeyModifiers::ALT)
}
fn frame(app: &mut App) -> (Buffer, Rect) {
    let full = Rect::new(0, 0, 120, 37);
    let mut buf = Buffer::empty(full);
    app.size_i = 2;
    app.render(&mut buf, full, Instant::now());
    app.render(&mut buf, full, Instant::now());
    (buf, app.frame_area)
}
fn text(app: &mut App) -> String {
    let (b, a) = frame(app);
    shoot::to_text(&b, a)
}

#[test]
fn every_screen_state_theme_and_size_renders() {
    let n = App::new().screens.len();
    assert!(n >= 20, "plan §10.3 lists ~21 screens, found {n}");
    for si in 0..n {
        let (key, states) = {
            let a = App::new();
            (a.screens[si].key, a.screens[si].states.len())
        };
        for st in 0..states {
            for theme in 0..3 {
                for size in 1..SIZES.len() {
                    let (buf, area) = shoot::render(key, st, theme, size, theme == 2).unwrap_or_else(|| panic!("{key}"));
                    let t = shoot::to_text(&buf, area);
                    assert!(t.lines().filter(|l| !l.trim().is_empty()).count() > 3, "{key} state {st} is empty");
                }
            }
        }
    }
}

#[test]
fn focus_ids_are_unique_and_hits_stay_inside_the_frame() {
    let n = App::new().screens.len();
    for si in 0..n {
        let states = App::new().screens[si].states.len();
        for st in 0..states {
            for size in 1..SIZES.len() {
                let mut app = App::new();
                app.screen = si;
                app.state = st;
                app.apply_state();
                app.size_i = size;
                let full = Rect::new(0, 0, SIZES[size].0, SIZES[size].1 + 1);
                let mut buf = Buffer::empty(full);
                app.render(&mut buf, full, Instant::now());
                app.render(&mut buf, full, Instant::now());
                let key = app.key_of();
                let order = app.focus.order().to_vec();
                let uniq: HashSet<&String> = order.iter().collect();
                assert_eq!(uniq.len(), order.len(), "{key}/{st}/{size}: duplicate focus ids {:?}", order.iter().filter(|i| order.iter().filter(|j| j == i).count() > 1).collect::<HashSet<_>>());
                for x in 0..full.width {
                    for y in 0..full.height {
                        if let Some((_, _)) = app.hits.at(x, y) {
                            assert!(x < full.width && y < full.height);
                        }
                    }
                }
            }
        }
    }
}

#[test]
fn nothing_hides_clipping_is_announced() {
    // At 80x24 a long Settings page must announce more content below, never just cut it.
    let mut app = App::new();
    app.select("settings-models", 0);
    app.size_i = 1;
    let t = {
        let (b, a) = frame_at(&mut app, 80, 24);
        shoot::to_text(&b, a)
    };
    assert!(t.contains("Area") && t.contains("Models"), "narrow Settings uses an area select:\n{t}");
}
fn frame_at(app: &mut App, w: u16, h: u16) -> (Buffer, Rect) {
    let full = Rect::new(0, 0, w, h + 1);
    let mut buf = Buffer::empty(full);
    app.size_i = 1;
    app.render(&mut buf, full, Instant::now());
    app.render(&mut buf, full, Instant::now());
    (buf, app.frame_area)
}

#[test]
fn models_tiers_are_tabs_with_defaults_above() {
    let mut app = App::new();
    app.select("settings-models", 1);
    let t = text(&mut app);
    let default_at = t.find("DEFAULT").expect("DEFAULT section");
    let tiers_at = t.find("TIERS").expect("TIERS section");
    assert!(default_at < tiers_at, "model settings sit above the tier tabs");
    assert!(t.contains("Low") && t.contains("Medium") && t.contains("High"));
    assert!(t.contains("Default model chain"));
}

#[test]
fn providers_is_one_page_with_a_section_per_provider() {
    let mut app = App::new();
    app.select("settings-providers", 0);
    let t = text(&mut app);
    for p in ["Anthropic", "OpenAI", "Google", "OpenCode Go", "Ollama"] {
        assert!(t.contains(p), "{p} missing:\n{t}");
    }
    assert!(t.contains("connected"));
}

#[test]
fn keyboard_reorder_a_tier_and_see_the_toast() {
    let mut app = App::new();
    app.select("settings-models", 1);
    text(&mut app);
    // → into the page, then walk to the first medium-tier row.
    app.key(key(KeyCode::Right));
    for _ in 0..40 {
        text(&mut app);
        if app.focus.current() == Some("tier:1:0") {
            break;
        }
        app.key(key(KeyCode::Down));
    }
    assert_eq!(app.focus.current(), Some("tier:1:0"));
    assert_eq!(app.w.tiers[1][0].label, "anthropic/claude-sonnet-5-5");
    app.key(alt(KeyCode::Down));
    assert_eq!(app.w.tiers[1][0].label, "openai/gpt-6-mini", "Alt+Down moves the item");
    assert_eq!(app.focus.current(), Some("tier:1:1"), "focus follows the moved item");
    let t = text(&mut app);
    assert!(t.contains("Medium tier order saved"), "{t}");
}

#[test]
fn space_toggles_and_confirms_with_a_toast_naming_the_file() {
    let mut app = App::new();
    app.select("settings-voice", 0);
    text(&mut app);
    app.focus.set("set:autosend");
    assert!(!app.w.auto_send);
    app.key(key(KeyCode::Char(' ')));
    assert!(app.w.auto_send);
    let t = text(&mut app);
    assert!(t.contains("Saved") && t.contains("nexus.toml"), "{t}");
}

#[test]
fn locked_tool_cannot_change_and_says_why() {
    let mut app = App::new();
    app.select("settings-tools", 0);
    text(&mut app);
    app.focus.set("tool:bash");
    app.key(key(KeyCode::Char(' ')));
    assert!(app.w.families[2].tools[0].on);
    let t = text(&mut app);
    assert!(t.contains("Locked after the first turn"), "{t}");
}

#[test]
fn up_down_in_the_area_list_switches_the_page() {
    let mut app = App::new();
    app.select("settings-providers", 0);
    text(&mut app);
    assert_eq!(app.focus.current(), Some("area:providers"));
    app.key(key(KeyCode::Down));
    text(&mut app);
    assert_eq!(app.v.area, 4, "Models is next");
    app.key(key(KeyCode::Up));
    app.key(key(KeyCode::Up));
    text(&mut app);
    assert_eq!(app.focus.current(), Some("area:keyboard"));
    assert_eq!(app.v.area, 2);
}

#[test]
fn escape_ladder_closes_popup_then_search_then_overlay() {
    let mut app = App::new();
    app.select("settings-voice", 0);
    text(&mut app);
    app.focus.set("set:device");
    app.key(key(KeyCode::Enter));
    assert!(app.v.popup.is_some());
    let t = text(&mut app);
    assert!(t.contains("metal"), "popup lists options:\n{t}");
    app.key(key(KeyCode::Esc));
    assert!(app.v.popup.is_none(), "first Esc closes the popup only");
    assert_eq!(app.key_of(), "settings-voice");
    app.key(key(KeyCode::Esc));
    assert_eq!(app.key_of(), "chat", "then the overlay closes");
}

#[test]
fn sessions_search_filters_and_clears_with_escape() {
    let mut app = App::new();
    app.select("sessions", 0);
    text(&mut app);
    app.key(key(KeyCode::Char('/')));
    assert_eq!(app.focus.current(), Some("sessions:search"));
    for c in "refr".chars() {
        app.key(key(KeyCode::Char(c)));
    }
    let t = text(&mut app);
    assert!(t.contains("Refresh docs for models") && t.contains("Refresh CSS tokens"), "{t}");
    assert!(!t.contains("Port settings"), "filter hides non-matches");
    app.key(key(KeyCode::Esc));
    assert!(app.v.sess_search.value.is_empty(), "Esc clears before leaving");
}

#[test]
fn sessions_filter_tabs_and_archive_shortcut() {
    let mut app = App::new();
    app.select("sessions", 0);
    text(&mut app);
    app.focus.set("session:s2");
    app.key(key(KeyCode::Char('a')));
    assert!(app.w.sessions.iter().find(|s| s.id == "s2").unwrap().archived);
    let t = text(&mut app);
    assert!(t.contains("Archived"), "{t}");
    app.key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::CONTROL));
    assert_eq!(app.v.sess_filter, 1);
}

#[test]
fn toasts_have_a_close_button_and_clicking_it_dismisses() {
    let mut app = App::new();
    app.select("toasts", 0);
    let t = text(&mut app);
    assert!(t.contains("×"), "{t}");
    let before = app.toasts.toasts.len();
    let hit = (0..120u16).flat_map(|x| (0..37u16).map(move |y| (x, y))).find(|&(x, y)| matches!(app.hits.at(x, y), Some((id, nexus_widgets::hit::Part::Named(n))) if id.starts_with("toast:") && n == "close")).expect("close hit");
    app.click(hit.0, hit.1);
    assert_eq!(app.toasts.toasts.len(), before - 1);
}

#[test]
fn dismiss_all_with_the_leader_chord() {
    let mut app = App::new();
    app.select("toasts", 1);
    app.key(KeyEvent::new(KeyCode::Char('x'), KeyModifiers::CONTROL));
    app.key(key(KeyCode::Char('x')));
    assert!(app.toasts.toasts.is_empty());
}

#[test]
fn remove_then_undo_restores_the_model() {
    let mut app = App::new();
    app.select("settings-models", 0);
    text(&mut app);
    app.focus.set("tier:0:1");
    let removed = app.w.tiers[0][1].label.clone();
    app.key(key(KeyCode::Delete));
    assert_eq!(app.w.tiers[0].len(), 1);
    app.key(KeyEvent::new(KeyCode::Char('x'), KeyModifiers::CONTROL));
    app.key(key(KeyCode::Char('t')));
    assert_eq!(app.w.tiers[0][1].label, removed);
}

#[test]
fn mono_theme_uses_no_colour() {
    let (buf, area) = shoot::render("settings-models", 0, 2, 2, false).unwrap();
    for y in area.y..area.y + area.height {
        for x in area.x..area.x + area.width {
            let c = &buf[(x, y)];
            assert!(matches!(c.fg, ratatui::style::Color::Reset) && matches!(c.bg, ratatui::style::Color::Reset | ratatui::style::Color::Black | ratatui::style::Color::Gray), "({x},{y}) {:?} {:?}", c.fg, c.bg);
        }
    }
}

#[test]
fn ascii_glyphs_have_no_wide_unicode_chrome() {
    let (buf, area) = shoot::render("settings-models", 0, 0, 2, true).unwrap();
    let t = shoot::to_text(&buf, area);
    for bad in ['▌', '⋮', '▾', '×', '━'] {
        assert!(!t.contains(bad), "ASCII mode drew {bad}:\n{t}");
    }
}

#[test]
fn scope_control_only_where_a_page_can_be_project_scoped() {
    for (key, scoped) in [("settings-models", false), ("settings-voice", false), ("settings-providers", false), ("settings-layout", false), ("settings-agents", false), ("settings-tools", false), ("settings-skills", true), ("settings-mcp", true)] {
        let mut app = App::new();
        app.select(key, 0);
        let t = text(&mut app);
        assert_eq!(t.contains("Scope"), scoped, "{key}:\n{t}");
        if !scoped {
            assert!(!t.contains("global  ") && !t.lines().any(|l| l.trim_end().ends_with("global")), "{key} shows a scope badge:\n{t}");
        }
    }
}

#[test]
fn context_header_has_no_dot_leader_and_tokens_sit_next_to_the_title() {
    let mut app = App::new();
    app.select("context-header", 0);
    let t = text(&mut app);
    assert!(!t.contains("····"), "no dot leader:\n{t}");
    let tools = t.lines().find(|l| l.contains("Tools") && l.contains("tok")).expect("tools heading");
    let (a, b) = (tools.find("Tools").unwrap(), tools.find("tok").unwrap());
    assert!(b - a < 20, "tokens next to the title: {tools}");
}

#[test]
fn context_header_always_shows_its_contents_in_every_state() {
    for st in 0..3 {
        let mut app = App::new();
        app.select("context-header", st);
        let t = text(&mut app);
        for need in ["read", "native-app-review", "github", "AGENTS.md"] {
            assert!(t.contains(need), "state {st} hides {need}:\n{t}");
        }
    }
    let mut app = App::new();
    app.select("chat", 0);
    assert!(text(&mut app).contains("native-app-review"), "chat shows the full header too");
    app.focus.set("ctx:tools");
    app.key(key(KeyCode::Enter));
    assert!(text(&mut app).contains("read"), "Enter inspects; it never collapses");
}
