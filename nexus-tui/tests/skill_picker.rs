mod common;
use common::*;
use nexus_tui::sync::SyncMsg;
use nexus_tui::theme::{Mode, Tier};
use nexus_tui::ui::OverlayKind;
use serde_json::{json, Value};

fn scenario() -> Value {
    serde_json::from_str(include_str!("../../interface/scenarios/skill-picker.json")).unwrap()
}

fn setup(count: usize) -> nexus_tui::app::App {
    let data = scenario();
    let mut app = empty(opts(Mode::Dark, Tier::TrueColor), (170, 50));
    app.ui.right_open = false;
    app.state.agents =
        vec![json!({"name": "build", "prompt": "Build carefully.", "tools": ["read"]})];
    let skills: Vec<_> = data["names"]
        .as_array()
        .unwrap()
        .iter()
        .take(count)
        .map(|name| {
            json!({
                "name": name, "description": data["description"], "state": "on", "locked": false
            })
        })
        .collect();
    app.apply_sync(SyncMsg::DraftContext {
        request: app.draft_request().unwrap(),
        data: Ok(json!({"used": 1200, "parts": {"instructions": 1200}, "tools": [], "skills": skills, "mcp": []})),
    });
    app
}

fn open_picker(app: &mut nexus_tui::app::App) {
    let (_, rows) = draw(app, 170, 50);
    let y = rows
        .iter()
        .position(|row| row.contains("Skills ["))
        .unwrap();
    let x = rows[y].find("Skills [").unwrap();
    click(app, x as u16, y as u16);
    assert!(
        matches!(app.ui.overlay.as_ref().map(|o| &o.kind), Some(OverlayKind::ContextEntries { title, .. }) if title == "Skills")
    );
}

fn screenshot(app: &mut nexus_tui::app::App, name: &str) {
    if let Ok(dir) = std::env::var("NEXUS_SKILL_SHOTS") {
        let (buffer, _) = draw(app, 170, 50);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            format!("{dir}/{name}.svg"),
            nexus_tui::svg::to_svg(&buffer, "#ededed", "#0b0b0b"),
        )
        .unwrap();
    }
}

#[test]
fn shared_skill_boundary_picker_and_nested_details() {
    for count in [12, 13] {
        let mut app = setup(count);
        let (_, rows) = draw(&mut app, 170, 50);
        let shown = rows.join("\n");
        assert_eq!(shown.contains("accessibility"), count == 12, "{shown}");
        screenshot(&mut app, &format!("inline-{count}"));
        open_picker(&mut app);
        let (buffer, rows) = draw(&mut app, 170, 50);
        let y = rows
            .iter()
            .position(|row| row.contains("accessibility"))
            .unwrap();
        let x = rows[y][..rows[y].find("accessibility").unwrap()]
            .chars()
            .count();
        let top = (x as u16 - 1, y as u16 - 1);
        assert_eq!(buffer[top].symbol(), " ", "cards must not have borders");
        assert_eq!(
            buffer[top].bg,
            app.theme.role_style("skill_card_focus").bg.unwrap()
        );
        let shown = rows.join("\n");
        assert!(shown.contains("Enabled"), "{shown}");
        assert!(shown.contains("Build accessible"), "{shown}");
        screenshot(&mut app, &format!("picker-{count}"));
        press(&mut app, &["space"]);
        assert!(app
            .ui
            .draft
            .disabled_tools
            .contains(&"skill:accessibility".into()));
        assert!(matches!(
            app.ui.overlay.as_ref().map(|o| &o.kind),
            Some(OverlayKind::ContextEntries { open: None, .. })
        ));
        press(&mut app, &["space"]);
        assert!(!app
            .ui
            .draft
            .disabled_tools
            .contains(&"skill:accessibility".into()));
        press(&mut app, &["down", "down", "down", "down", "down"]);
        screenshot(&mut app, &format!("scrolled-{count}"));
        press(&mut app, &["enter"]);
        if let Some(overlay) = app.ui.overlay.as_mut() {
            if let OverlayKind::ContextEntries {
                entries,
                open: Some(index),
                ..
            } = &mut overlay.kind
            {
                let data = scenario();
                entries[*index].full = Some(nexus_tui::actions::skill_detail(&data));
            }
        }
        screenshot(&mut app, &format!("detail-{count}"));
        let (_, rows) = draw(&mut app, 170, 50);
        let mut shown = rows.join("\n");
        press(&mut app, &["pagedown"]);
        screenshot(&mut app, &format!("detail-markdown-{count}"));
        shown.push_str(&draw(&mut app, 170, 50).1.join("\n"));
        assert!(
            shown.contains("detail view."),
            "the full description must wrap: {shown}"
        );
        for expected in scenario()["detail_expect"].as_array().unwrap() {
            assert!(shown.contains(expected.as_str().unwrap()), "{shown}");
        }
        press(&mut app, &["pagedown", "esc"]);
        assert!(matches!(
            app.ui.overlay.as_ref().map(|o| &o.kind),
            Some(OverlayKind::ContextEntries {
                cursor: 5,
                open: None,
                ..
            })
        ));
        screenshot(&mut app, &format!("return-{count}"));
        press(&mut app, &["esc"]);
        assert!(app.ui.overlay.is_none());
    }
}

#[test]
fn inline_skill_name_opens_details_below_overflow_limit() {
    let mut app = setup(12);
    let (_, rows) = draw(&mut app, 170, 50);
    let y = rows
        .iter()
        .position(|row| row.contains("accessibility"))
        .unwrap();
    let x = rows[y].find("accessibility").unwrap();
    click(&mut app, x as u16, y as u16);
    assert!(matches!(
        app.ui.overlay.as_ref().map(|o| &o.kind),
        Some(OverlayKind::ContextEntries { open: Some(0), .. })
    ));
    press(&mut app, &["esc"]);
    assert!(matches!(
        app.ui.overlay.as_ref().map(|o| &o.kind),
        Some(OverlayKind::ContextEntries { open: None, .. })
    ));
}
