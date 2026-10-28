//! Focus model, the Esc precedence chain, the palette and command availability.

mod common;
use common::*;
use nexus_tui::app::{Effect, WIRED};
use nexus_tui::theme::{Mode, Tier};
use nexus_tui::ui::{Focus, PaletteKind, Tab, View};

fn app() -> nexus_tui::app::App {
    with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36))
}

fn rule(a: &nexus_tui::app::App) -> String {
    a.escape_rule().unwrap()
}

#[test]
fn model_agent_and_effort_pickers_use_core_catalog_data() {
    let mut a = app();
    a.execute("agent.change");
    assert!(a
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Sync(nexus_tui::sync::Command::Catalog(_)))));
    a.apply_sync(nexus_tui::sync::SyncMsg::Catalog {
        models: vec![serde_json::json!({
            "ref": "codex/gpt-6.1-terra", "provider": "codex", "id": "gpt-6.1-terra",
            "name": "GPT", "context": 400000, "reasoning": true,
            "efforts": ["low", "medium", "high"]
        })],
        agents: vec![
            serde_json::json!({"name": "build", "source": "builtin", "mode": "all"}),
            serde_json::json!({"name": "scout", "source": "builtin", "mode": "subagent"}),
        ],
        source: "models.dev".into(),
    });
    let items = a.palette_items();
    assert!(items.iter().any(|i| i.label == "scout"), "agents listed");
    a.execute("model.change");
    let items = a.palette_items();
    assert!(
        items
            .iter()
            .any(|i| i.label.contains("codex/gpt-6.1-terra")),
        "models listed: {items:?}"
    );
}

#[test]
fn escape_chain_follows_the_manifest_order() {
    let mut a = app();
    draw(&mut a, 120, 36);
    // nothing special: last resort refocuses the composer
    assert_eq!(rule(&a), "refocus");
    // settings view
    press(&mut a, &["ctrl+,"]);
    assert_eq!(a.ui.view, View::Settings);
    assert_eq!(rule(&a), "settings");
    // a running turn beats the settings view
    busy(&mut a);
    assert_eq!(rule(&a), "interrupt");
    // an alert with focus beats interrupt
    a.toast(nexus_tui::ui::Level::Info, "hello");
    a.ui.focus = Focus::Alert;
    assert_eq!(rule(&a), "alert");
    // a narrow drawer beats the alert
    a.set_size(80, 24);
    press(&mut a, &["ctrl+b"]);
    assert_eq!(rule(&a), "drawer");
    // an inline edit beats the drawer
    a.ui.drawer = None;
    a.ui.view = View::Session;
    a.execute("session.rename");
    assert_eq!(rule(&a), "text_edit");
    // a dialog beats the edit
    a.ui.edit = None;
    a.execute("session.delete");
    assert!(
        a.ui.dialog.is_some()
            || a.availability(a.manifest.command("session.delete").unwrap())
                .is_err()
    );
}

#[test]
fn overlay_and_dialog_outrank_everything() {
    let mut a = app();
    busy(&mut a);
    a.execute("session.rename");
    a.execute("palette.open");
    assert_eq!(rule(&a), "overlay");
    press(&mut a, &["esc"]);
    assert!(a.ui.overlay.is_none());
    // the rename edit is still open and now wins over interrupt
    assert_eq!(rule(&a), "text_edit");
    press(&mut a, &["esc"]);
    assert!(a.ui.edit.is_none());
    assert_eq!(rule(&a), "interrupt");
    // a dialog (idle session) is above the rest and cancels
    let mut a = app();
    a.execute("session.delete");
    assert_eq!(rule(&a), "dialog");
    press(&mut a, &["esc"]);
    assert!(
        a.ui.dialog.is_none() && a.effects.is_empty(),
        "cancel deletes nothing"
    );
}

#[test]
fn esc_interrupts_without_clearing_text_or_navigating_back() {
    let mut a = app();
    busy(&mut a);
    typing(&mut a, "half a thought");
    press(&mut a, &["esc"]);
    assert_eq!(
        a.composer_ref().text(),
        "half a thought",
        "Esc never clears the composer"
    );
    assert!(matches!(
        a.effects.as_slice(),
        [Effect::Api(nexus_tui::actions::ApiCall::Interrupt { .. })]
    ));
    assert_eq!(a.viewed_id(), Some(SESSION), "one press interrupts only");
}

#[test]
fn back_navigates_a_child_to_its_parent_after_interrupt_is_not_needed() {
    let mut a = app();
    // make the viewed session a child of a second one
    let mut parent = a.state.model.sessions[SESSION].clone();
    parent.id = "ses_parent".into();
    parent.title = "Parent".into();
    parent.parent_id = None;
    a.state.model.sessions.insert(parent.id.clone(), parent);
    a.state.model.sessions.get_mut(SESSION).unwrap().parent_id = Some("ses_parent".into());
    assert_eq!(rule(&a), "back");
    press(&mut a, &["esc"]);
    assert_eq!(a.viewed_id(), Some("ses_parent"));
    assert!(a.effects.iter().any(|e| matches!(e, Effect::Sync(_))));
}

#[test]
fn overlays_restore_the_trigger_focus_and_scroll() {
    let mut a = app();
    draw(&mut a, 120, 36);
    a.ui.focus = Focus::Timeline;
    a.ui.scroll = nexus_tui::ui::Scroll {
        follow: false,
        top: 3,
    };
    press(&mut a, &["ctrl+p"]);
    a.ui.scroll = nexus_tui::ui::Scroll::default();
    a.ui.focus = Focus::Composer;
    press(&mut a, &["esc"]);
    assert_eq!(a.ui.focus, Focus::Timeline);
    assert_eq!(a.ui.scroll.top, 3);
    // a palette opened from the notifications overlay still gives back the first trigger
    a.ui.focus = Focus::Sidebar;
    press(&mut a, &["ctrl+n"]);
    press(&mut a, &["ctrl+p"]);
    press(&mut a, &["esc"]);
    assert_eq!(a.ui.focus, Focus::Sidebar);
}

#[test]
fn f6_cycles_focus_and_toggles_are_focusable() {
    let mut a = app();
    a.ui.focus = Focus::ToggleLeft;
    let mut seen = vec![a.ui.focus];
    for _ in 0..a.focus_order().len() {
        press(&mut a, &["f6"]);
        seen.push(a.ui.focus);
    }
    assert_eq!(seen.first(), seen.last());
    assert!(
        seen.contains(&Focus::ToggleRight)
            && seen.contains(&Focus::Sidebar)
            && seen.contains(&Focus::Composer)
    );
    a.ui.focus = Focus::ToggleRight;
    let open = a.ui.right_open;
    press(&mut a, &["enter"]);
    assert_ne!(a.ui.right_open, open);
}

fn palette_labels(a: &nexus_tui::app::App) -> Vec<(String, Option<String>)> {
    a.palette_items()
        .into_iter()
        .map(|i| (i.label, i.reason))
        .collect()
}

#[test]
fn palette_filters_fuzzily_and_shows_keys() {
    let mut a = app();
    press(&mut a, &["ctrl+p"]);
    let all = a.palette_items();
    assert!(all.len() > 30, "the manifest drives the list");
    // enabled commands come first when nothing is typed
    let first_disabled = all.iter().position(|i| i.reason.is_some()).unwrap();
    assert!(all[first_disabled..].iter().all(|i| i.reason.is_some()));
    typing(&mut a, "tgl left");
    let items = a.palette_items();
    assert_eq!(
        items[0].kind,
        PaletteKind::Command("sidebar.left.toggle".into())
    );
    assert_eq!(items[0].hint, "ctrl+b", "the key is shown");
    // subsequence match on the title, case-insensitive
    for _ in 0..8 {
        press(&mut a, &["backspace"]);
    }
    typing(&mut a, "ARCH");
    let labels = palette_labels(&a);
    assert!(labels.iter().any(|(l, _)| l == "Archive session"));
    assert!(labels.iter().any(|(l, _)| l == "Unarchive session"));
    typing(&mut a, "zzzzq");
    assert!(a.palette_items().is_empty());
}

#[test]
fn disabled_commands_are_listed_with_their_reason_and_never_run() {
    let mut a = app();
    press(&mut a, &["ctrl+p"]);
    typing(&mut a, "steer");
    let items = a.palette_items();
    let steer = items
        .iter()
        .find(|i| i.kind == PaletteKind::Command("session.steer".into()))
        .expect("blocked_core commands are shown, not hidden");
    assert_eq!(
        steer.reason.as_deref(),
        Some("Steering needs a running turn.")
    );
    // Enter explains and keeps the palette open
    press(&mut a, &["enter"]);
    assert!(a.ui.overlay.is_some());
    assert!(a
        .ui
        .alerts
        .toasts
        .iter()
        .any(|t| t.text.contains("Steering needs")));
    assert!(a.effects.is_empty());
    // a core-blocked command with no condition of its own: handoff while idle
    let handoff = a.manifest.command("session.handoff").unwrap().clone();
    assert!(a.availability(&handoff).is_err());
    // commands that exist in the manifest but are not built yet say so
    let unbuilt = a.manifest.command("session.worktrees").unwrap().clone();
    assert!(a
        .availability(&unbuilt)
        .unwrap_err()
        .contains("Not available yet"));
}

#[test]
fn wired_commands_exist_and_blocked_ones_are_never_wired_open() {
    let mut a = app();
    for id in WIRED {
        let c = a
            .manifest
            .command(id)
            .unwrap_or_else(|| panic!("{id} not in manifest"));
        assert!(
            !matches!(c.status.as_deref(), Some("blocked_core" | "deferred")),
            "{id} is blocked in the manifest"
        );
    }
    // every blocked/deferred command is disabled no matter what
    let blocked: Vec<_> = a
        .manifest
        .command
        .iter()
        .filter(|c| matches!(c.status.as_deref(), Some("blocked_core" | "deferred")))
        .cloned()
        .collect();
    assert!(blocked.len() > 8);
    for c in &blocked {
        assert!(a.availability(c).is_err(), "{}", c.id);
        a.run_command(&c.id);
    }
    assert!(a.effects.is_empty(), "disabled commands do nothing");
}

#[test]
fn every_vocabulary_term_is_known_to_this_client() {
    let a = app();
    for t in &a.manifest.vocabulary.terms {
        assert!(a.term(t).is_some(), "term {t} not implemented");
    }
}

#[test]
fn settings_navigation_follows_the_manifest_order() {
    let mut a = app();
    press(&mut a, &["ctrl+,"]);
    let (_, rows) = draw(&mut a, 120, 36);
    let text = rows.join("\n");
    let order: Vec<usize> = a
        .manifest
        .settings
        .section
        .iter()
        .map(|s| {
            text.find(&format!(" {}", s.label))
                .unwrap_or_else(|| panic!("{} missing", s.label))
        })
        .collect();
    assert!(order.windows(2).all(|p| p[0] < p[1]), "{order:?}");
    // down moves through sections in order, enter goes into the rows
    press(&mut a, &["down", "down"]);
    assert_eq!(
        a.manifest.settings.section[a.ui.settings_section].id,
        "models"
    );
    press(&mut a, &["esc"]);
    assert_eq!(a.ui.view, View::Session);
    assert_eq!(a.ui.tab, Tab::Sessions);
}

#[test]
fn session_actions_are_wired_to_the_api() {
    use nexus_tui::actions::ApiCall;
    let mut a = app();
    typing(&mut a, "hello there");
    press(&mut a, &["enter"]);
    assert!(
        matches!(a.effects.pop(), Some(Effect::Api(ApiCall::Prompt { text, .. })) if text == "hello there")
    );
    assert!(a.composer_ref().is_empty());
    // failed send puts the draft back
    a.apply_done(nexus_tui::actions::ActionDone {
        call: ApiCall::Prompt {
            session: SESSION.into(),
            text: "hello there".into(),
        },
        result: Err("busy".into()),
    });
    assert_eq!(a.composer_ref().text(), "hello there");
    assert!(a
        .ui
        .alerts
        .toasts
        .last()
        .unwrap()
        .text
        .contains("Send failed"));
    // rename, archive
    a.execute("session.archive");
    assert!(matches!(
        a.effects.pop(),
        Some(Effect::Api(ApiCall::Archive { archived: true, .. }))
    ));
    // delete asks first and names the damage
    a.execute("session.delete");
    assert!(a.effects.is_empty());
    assert_eq!(
        a.ui.dialog.as_ref().unwrap().button,
        0,
        "the safe choice is focused"
    );
    press(&mut a, &["right", "enter"]);
    assert!(matches!(
        a.effects.pop(),
        Some(Effect::Api(ApiCall::Delete { .. }))
    ));
    // continue after a cut-short turn: the last message is not an assistant's
    // slash command runs the same command
    a.composer().set_text("/rename New title");
    press(&mut a, &["enter"]);
    assert!(
        matches!(a.effects.pop(), Some(Effect::Api(ApiCall::Rename { title, .. })) if title == "New title")
    );
}

#[test]
fn composer_paste_selection_undo_and_send_preserve_full_text() {
    use nexus_tui::actions::ApiCall;
    let mut a = app();
    a.effects.clear();
    let pasted = "first line\nsecond line\nthird line";
    a.handle_paste(pasted);
    assert!(a.composer_ref().text().contains("Pasted ~"));
    assert_eq!(a.composer_ref().expanded_text(), pasted);
    press(&mut a, &["ctrl+z"]);
    assert!(a.composer_ref().is_empty());
    press(&mut a, &["ctrl+y"]);
    assert_eq!(a.composer_ref().expanded_text(), pasted);
    press(&mut a, &["enter"]);
    assert!(a
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Api(ApiCall::Prompt { text, .. }) if text == pasted)));

    let mut a = app();
    typing(&mut a, "hello");
    press(&mut a, &["shift+left", "shift+left"]);
    assert!(a.composer_ref().selection().is_some());
    typing(&mut a, "!");
    assert_eq!(a.composer_ref().text(), "hel!");
    press(&mut a, &["ctrl+z"]);
    assert_eq!(a.composer_ref().text(), "hello");
    press(&mut a, &["end", "shift+left", "shift+left", "ctrl+c"]);
    assert!(a
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Copy(text) if text == "lo")));
}

#[test]
fn failed_send_restores_expanded_pending_paste() {
    use nexus_tui::actions::{ActionDone, ApiCall};
    let mut a = app();
    typing(&mut a, "sent");
    press(&mut a, &["enter"]);
    let pending = "first\nsecond\nthird";
    a.handle_paste(pending);
    a.apply_done(ActionDone {
        call: ApiCall::Prompt {
            session: SESSION.into(),
            text: "sent".into(),
        },
        result: Err("offline".into()),
    });
    assert_eq!(a.composer_ref().expanded_text(), format!("sent\n{pending}"));
}

#[test]
fn sent_draft_history_restores_the_unsent_draft() {
    let mut a = app();
    typing(&mut a, "first prompt");
    press(&mut a, &["enter"]);
    typing(&mut a, "unsent");
    press(&mut a, &["up"]);
    assert_eq!(a.composer_ref().text(), "first prompt");
    press(&mut a, &["down"]);
    assert_eq!(a.composer_ref().text(), "unsent");
}

#[test]
fn mouse_drag_selects_composer_text_for_copy() {
    use crossterm::event::{KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
    let mut a = app();
    typing(&mut a, "hello world");
    let (_, _) = draw(&mut a, 120, 36);
    let x = a.regions.composer.x + 3;
    let y = a.regions.composer.y + 1;
    for (kind, column) in [
        (MouseEventKind::Down(MouseButton::Left), x + 6),
        (MouseEventKind::Drag(MouseButton::Left), x + 10),
        (MouseEventKind::Up(MouseButton::Left), x + 10),
    ] {
        a.handle_mouse(MouseEvent {
            kind,
            column,
            row: y,
            modifiers: KeyModifiers::NONE,
        });
    }
    a.effects.clear();
    press(&mut a, &["ctrl+c"]);
    assert!(a
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Copy(text) if text == "worl")));
}

#[test]
fn history_keeps_expanded_pastes_and_is_scoped_to_each_session() {
    let mut a = app();
    let first = "one\ntwo\nthree";
    a.handle_paste(first);
    press(&mut a, &["enter"]);
    let scratch = "new\npending\npaste";
    a.handle_paste(scratch);
    press(&mut a, &["up"]);
    assert_eq!(a.composer_ref().expanded_text(), first);
    press(&mut a, &["down"]);
    assert_eq!(a.composer_ref().expanded_text(), scratch);
    a.state.model.select(None);
    assert!(!a.composer_ref().has_history());
    a.open_session(SESSION);
    assert_eq!(a.composer_ref().expanded_text(), scratch);
}

#[test]
fn mouse_drag_uses_display_cells_and_keeps_wide_graphemes_whole() {
    use crossterm::event::{KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
    let mut a = app();
    typing(&mut a, "a日本b");
    let (_, _) = draw(&mut a, 120, 36);
    let x = a.regions.composer.x + 3;
    let y = a.regions.composer.y + 1;
    for (kind, column) in [
        (MouseEventKind::Down(MouseButton::Left), x + 1),
        (MouseEventKind::Drag(MouseButton::Left), x + 5),
        (MouseEventKind::Up(MouseButton::Left), x + 5),
    ] {
        a.handle_mouse(MouseEvent {
            kind,
            column,
            row: y,
            modifiers: KeyModifiers::NONE,
        });
    }
    assert_eq!(a.composer_ref().selected_text().as_deref(), Some("日本"));
}

#[test]
fn mouse_drag_maps_to_the_scrolled_composer_viewport() {
    use crossterm::event::{KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
    let mut a = app();
    a.composer().set_text(
        &(1..=12)
            .map(|n| format!("line{n:02}"))
            .collect::<Vec<_>>()
            .join("\n"),
    );
    let (_, _) = draw(&mut a, 120, 36);
    let x = a.regions.composer.x + 3;
    let y = a.regions.composer.y + 1;
    for (kind, row, column) in [
        (MouseEventKind::Down(MouseButton::Left), y, x),
        (MouseEventKind::Drag(MouseButton::Left), y + 1, x + 3),
        (MouseEventKind::Up(MouseButton::Left), y + 1, x + 3),
    ] {
        a.handle_mouse(MouseEvent {
            kind,
            column,
            row,
            modifiers: KeyModifiers::NONE,
        });
    }
    assert_eq!(
        a.composer_ref().selected_text().as_deref(),
        Some("line05\nlin")
    );
}
