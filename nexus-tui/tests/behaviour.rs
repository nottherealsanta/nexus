//! Focus model, the Esc precedence chain, the palette and command availability.

mod common;
use common::*;
use nexus_tui::actions::{ActionDone, ApiCall, Done};
use nexus_tui::api::Frame;
use nexus_tui::app::{App, Effect, Notify, SettingsItem, WIRED};
use nexus_tui::dictation::{CaptureCommand, Phase};
use nexus_tui::sync::SyncMsg;
use nexus_tui::theme::{Mode, Tier};
use nexus_tui::ui::{Focus, OverlayKind, PaletteKind, Tab, Target, View};

fn app() -> App {
    with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36))
}

fn begin_learn(a: &mut App, source: &str, scope: &str) -> ApiCall {
    typing(a, &format!("/learn {source}"));
    press(a, &["enter"]);
    assert!(
        matches!(
            a.ui.overlay.as_ref().map(|overlay| &overlay.kind),
            Some(OverlayKind::Palette(_))
        ),
        "composer={:?}, project={:?}, overlay={:?}, effects={:?}, toasts={:?}",
        a.composer_ref().text(),
        a.current_project(),
        a.ui.overlay,
        a.effects,
        a.ui.alerts.toasts
    );
    if scope == "global" {
        press(a, &["down"]);
    }
    press(a, &["enter"]);
    a.effects
        .iter()
        .find_map(|effect| match effect {
            Effect::Api(call @ ApiCall::LearnPreview { .. }) => Some(call.clone()),
            _ => None,
        })
        .expect("scope choice starts a preview")
}

fn apply_preview(a: &mut App, call: ApiCall, preview: serde_json::Value) {
    a.effects.clear();
    a.apply_done(ActionDone {
        call,
        result: Ok(Done {
            learn_preview: Some(preview),
            ..Done::default()
        }),
    });
}

fn global_event(a: &App, id: i64, kind: &str, data: serde_json::Value) -> nexus_tui::api::Event {
    let generation = a.state.model.generation.as_deref().unwrap();
    let value = serde_json::json!({
        "id": id,
        "type": kind,
        "session_id": null,
        "cursor": format!("{generation}:{id}"),
        "time": 1793113317.0,
        "data": data,
    });
    let Frame::Event(event) = Frame::from_value(kind, None, &value).unwrap() else {
        panic!("expected a global event")
    };
    event
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
            serde_json::json!({"name": "custom", "source": "project", "mode": "primary"}),
        ],
        source: "models.dev".into(),
        last_model: "codex/gpt-6.1-terra".into(),
    });
    let items = a.palette_items();
    assert!(items.iter().any(|i| i.label == "build"));
    assert!(items.iter().any(|i| i.label == "custom"));
    assert!(!items.iter().any(|i| i.label == "scout"));
    a.state.model.sessions.get_mut(SESSION).unwrap().parent_id = Some("parent".into());
    let items = a.palette_items();
    assert!(items.iter().any(|i| i.label == "build"));
    assert!(items.iter().any(|i| i.label == "scout"));
    assert!(!items.iter().any(|i| i.label == "custom"));
    assert_eq!(a.state.agents.len(), 3, "the shared catalog remains intact");
    a.execute("model.change");
    let items = a.palette_items();
    // the model's name on the left, its provider on the right (nexus-tui README)
    assert!(
        items.iter().any(
            |i| i.kind == PaletteKind::Model("codex/gpt-6.1-terra".into())
                && i.label == "GPT"
                && i.hint == "codex"
        ),
        "models listed: {items:?}"
    );
}

#[test]
fn learn_checklist_defaults_and_all_selection_keys_drive_install() {
    let mut a = app();
    let call = begin_learn(&mut a, "owner/repo", "project");
    let ApiCall::LearnPreview {
        project,
        source,
        scope,
    } = call.clone()
    else {
        panic!("expected preview")
    };
    apply_preview(
        &mut a,
        call,
        serde_json::json!({
            "fetch_id": "fetch-1",
            "requested": ["alpha", "beta"],
            "problems": [],
            "found": [
                {"name": "alpha", "description": "A new skill", "installed": false},
                {"name": "beta", "description": "An existing skill", "installed": true}
            ]
        }),
    );
    let Some(OverlayKind::LearnChecklist(checklist)) =
        a.ui.overlay.as_ref().map(|overlay| &overlay.kind)
    else {
        panic!("expected checklist")
    };
    assert_eq!(checklist.selected, [true, false], "new skills start ticked");
    assert_eq!(checklist.project, project);
    assert_eq!(checklist.source, source);
    assert_eq!(checklist.scope, scope);

    press(&mut a, &["down", "up"]);
    assert_eq!(
        match a.ui.overlay.as_ref().map(|overlay| &overlay.kind) {
            Some(OverlayKind::LearnChecklist(checklist)) => checklist.cursor,
            _ => usize::MAX,
        },
        0
    );
    press(&mut a, &["a"]);
    assert_eq!(checklist_selected(&a), [true, true]);
    press(&mut a, &["n"]);
    assert_eq!(checklist_selected(&a), [false, false]);
    press(&mut a, &["i"]);
    assert_eq!(checklist_selected(&a), [true, true]);
    press(&mut a, &["n", "enter"]);
    assert!(matches!(
        a.ui.alerts.toasts.last().map(|toast| toast.text.as_str()),
        Some("Choose at least one skill")
    ));
    assert!(
        a.effects.is_empty(),
        "nothing installs while none are ticked"
    );

    press(&mut a, &["down", "space", "enter"]);
    let call = a.effects.iter().find_map(|effect| match effect {
        Effect::Api(call @ ApiCall::LearnInstall { .. }) => Some(call),
        _ => None,
    });
    assert!(matches!(
        call,
        Some(ApiCall::LearnInstall {
            skills,
            overwrite: true,
            replacing,
            fetch_id,
            scope,
            ..
        }) if skills == &["beta"] && replacing == &["beta"] && fetch_id == "fetch-1" && scope == "project"
    ));
    assert!(a.ui.overlay.is_none());
}

fn checklist_selected(a: &App) -> Vec<bool> {
    match a.ui.overlay.as_ref().map(|overlay| &overlay.kind) {
        Some(OverlayKind::LearnChecklist(checklist)) => checklist.selected.clone(),
        other => panic!("expected checklist, got {other:?}"),
    }
}

#[test]
fn learn_installs_a_requested_unambiguous_set_without_opening_a_checklist() {
    let mut a = app();
    let call = begin_learn(&mut a, "owner/repo", "global");
    let preview = serde_json::json!({
        "fetch_id": "fetch-direct",
        "requested": ["one"],
        "problems": [],
        "found": [
            {"name": "one", "description": "Requested", "installed": false},
            {"name": "extra", "description": "Additional", "installed": true}
        ]
    });
    apply_preview(&mut a, call, preview);
    assert!(
        a.ui.overlay.is_none(),
        "unambiguous new skills install directly"
    );
    assert!(matches!(
        a.effects.last(),
        Some(Effect::Api(ApiCall::LearnInstall {
            skills,
            overwrite: false,
            scope,
            fetch_id,
            ..
        })) if skills == &["one"] && scope == "global" && fetch_id == "fetch-direct"
    ));
}

#[test]
fn escape_cancels_the_learn_checklist_without_installing() {
    let mut a = app();
    let call = begin_learn(&mut a, "owner/repo", "project");
    apply_preview(
        &mut a,
        call,
        serde_json::json!({
            "fetch_id": "fetch-cancel",
            "requested": [],
            "found": [{"name": "one", "description": "One", "installed": false}]
        }),
    );
    assert!(matches!(
        a.ui.overlay.as_ref().map(|overlay| &overlay.kind),
        Some(OverlayKind::LearnChecklist(_))
    ));
    press(&mut a, &["esc"]);
    assert!(a.ui.overlay.is_none());
    assert!(a.effects.is_empty());
}

#[test]
fn context_switches_patch_the_full_session_list_and_locked_items_refuse() {
    let mut a = app();
    a.state.model.selected.as_mut().unwrap().starting = Some(serde_json::json!({
        "tools": ["read", "bash"],
        "tool_definitions": {},
        "instruction_file": {"status": "missing"}
    }));
    let session = a.state.model.sessions.get_mut(SESSION).unwrap();
    session.extra.insert(
        "disabled_tools".into(),
        serde_json::json!(["bash", "custom"]),
    );
    session.extra.insert(
        "skills".into(),
        serde_json::json!([
            {"name": "planner", "description": "Plan work", "scope": "project", "state": "on", "locked": false},
            {"name": "locked", "description": "Later", "scope": "global", "state": "off", "locked": true}
        ]),
    );
    session.extra.insert(
        "mcp".into(),
        serde_json::json!([
            {"server": "docs", "tools": 4, "state": "on", "locked": false, "error": null},
            {"server": "offline", "tools": 2, "state": "unavailable", "locked": true, "error": "timeout"}
        ]),
    );
    a.effects.clear();
    draw(&mut a, 120, 36);
    a.ui.focus = Focus::Timeline;
    a.ui.timeline_sel =
        a.ui.timeline_blocks
            .iter()
            .position(|block| block.kind == nexus_tui::timeline::BlockKind::Context);
    let items = a.context_items();
    let planner = items
        .iter()
        .position(|item| item.key == "skill:planner")
        .unwrap();
    a.ui.context_cursor = planner;
    press(&mut a, &["enter"]);
    assert!(matches!(
        a.effects.last(),
        Some(Effect::Api(ApiCall::SetDisabledTools {
            session,
            disabled_tools,
            ..
        })) if session == SESSION && disabled_tools == &["bash", "custom", "skill:planner"]
    ));

    a.effects.clear();
    let locked = a
        .context_items()
        .iter()
        .position(|item| item.key == "skill:locked")
        .unwrap();
    a.click(Target::ContextToggle(locked));
    assert!(a.effects.is_empty());
    assert!(a
        .ui
        .alerts
        .toasts
        .last()
        .unwrap()
        .text
        .contains("new session"));
    let items = a.context_items();
    assert!(items.iter().any(|item| item.key == "mcp:offline"
        && item.label.contains("2 tools")
        && item.label.contains("timeout")));
}

#[test]
fn draft_context_switches_are_sent_with_new_session_fields() {
    let mut a = nexus_tui::app::App::new(
        nexus_tui::state::AppState::new(),
        nexus_tui::theme::Theme::new(opts(Mode::Dark, Tier::TrueColor)),
        (120, 36),
    );
    a.state.apply(nexus_tui::sync::SyncMsg::Connection(
        nexus_tui::state::ConnectionState::Live,
    ));
    a.state.apply(nexus_tui::sync::SyncMsg::Snapshot {
        snapshot: Box::new(common::snapshot()),
        cause: nexus_tui::sync::ResyncCause::Initial,
    });
    // Core's plan for the draft is the only source of the lists.
    assert!(a.context_items().is_empty());
    let request = a.draft_request().unwrap();
    a.state.apply(nexus_tui::sync::SyncMsg::DraftContext {
        request,
        data: Ok(serde_json::json!({
            "tools": ["read", "skill"],
            "skills": [{"name": "writer", "description": "Write clearly", "scope": "project", "state": "on", "locked": false}],
            "mcp": [{"server": "enabled", "state": "on", "tools": 12, "locked": false, "error": null}]
        })),
    });
    let items = a.context_items();
    assert!(items.iter().any(|item| item.key == "skill"));
    assert!(items
        .iter()
        .any(|item| item.key == "mcp:enabled" && item.label.contains("12 tools")));
    let index = items
        .iter()
        .position(|item| item.key == "skill:writer")
        .unwrap();
    a.click(Target::ContextToggle(index));
    assert_eq!(
        a.ui.draft.fields()["disabled_tools"],
        serde_json::json!(["skill:writer"])
    );
}

#[test]
fn skills_and_mcp_events_toast_and_debounce() {
    let mut a = app();
    let first = a.ui.alerts.toasts.len();
    a.apply_sync(nexus_tui::sync::SyncMsg::Event(global_event(
        &a,
        500,
        "skills.changed",
        serde_json::json!({}),
    )));
    assert_eq!(a.ui.alerts.toasts.len(), first + 1);
    assert_eq!(
        a.ui.alerts.toasts.last().unwrap().text,
        "Skills changed. New sessions will use them."
    );
    a.apply_sync(nexus_tui::sync::SyncMsg::Event(global_event(
        &a,
        501,
        "skills.changed",
        serde_json::json!({}),
    )));
    assert_eq!(a.ui.alerts.toasts.len(), first + 1, "repeat is debounced");
    a.apply_sync(nexus_tui::sync::SyncMsg::Event(global_event(
        &a,
        502,
        "mcp.changed",
        serde_json::json!({}),
    )));
    let mcp_toasts = a.ui.alerts.toasts.len();
    a.apply_sync(nexus_tui::sync::SyncMsg::Event(global_event(
        &a,
        503,
        "mcp.changed",
        serde_json::json!({}),
    )));
    assert_eq!(
        a.ui.alerts.toasts.len(),
        mcp_toasts,
        "MCP repeats are debounced"
    );
    assert_eq!(
        a.ui.alerts.toasts.last().unwrap().text,
        "MCP servers changed. New sessions will use them."
    );
    a.apply_sync(nexus_tui::sync::SyncMsg::Event(global_event(
        &a,
        504,
        "mcp.failed",
        serde_json::json!({"server": "docs", "error": "connection timed out"}),
    )));
    assert_eq!(
        a.ui.alerts.toasts.last().unwrap().text,
        "MCP server docs failed: connection timed out"
    );

    let count = a.ui.alerts.toasts.len();
    a.apply_done(ActionDone {
        call: ApiCall::LearnInstall {
            project: a.current_project().unwrap(),
            scope: "project".into(),
            fetch_id: "fetch".into(),
            skills: vec!["new-skill".into()],
            overwrite: false,
            replacing: Vec::new(),
        },
        result: Ok(Done::default()),
    });
    a.apply_sync(nexus_tui::sync::SyncMsg::Event(global_event(
        &a,
        505,
        "skills.changed",
        serde_json::json!({}),
    )));
    assert_eq!(
        a.ui.alerts.toasts.len(),
        count + 1,
        "only the install toast is added"
    );
}

#[test]
fn mcp_settings_patch_core_values_with_a_bounded_number() {
    let mut a = app();
    a.state.config = Some(serde_json::json!({
        "revision": "cfg-1",
        "values": {"mcp": {"full_definitions": false, "max_listed_tools": 1000}}
    }));
    a.execute("app.settings");
    let rows = a.settings_rows();
    let items = a.settings_items();
    let position = |id: &str| {
        rows.iter()
            .position(|at| matches!(&items[*at], SettingsItem::Row { row, .. } if row.id == id))
            .unwrap()
    };
    a.ui.settings_row = position("mcp.full_definitions");
    press(&mut a, &["right"]);
    assert!(matches!(
        a.effects.last(),
        Some(Effect::Sync(nexus_tui::sync::Command::PatchConfig { revision, path, value }))
            if revision == "cfg-1" && path == "mcp.full_definitions" && value == &serde_json::json!(true)
    ));
    a.effects.clear();
    a.ui.settings_row = position("mcp.max_listed_tools");
    press(&mut a, &["right"]);
    assert!(matches!(
        a.effects.last(),
        Some(Effect::Sync(nexus_tui::sync::Command::PatchConfig { path, value, .. }))
            if path == "mcp.max_listed_tools" && value == &serde_json::json!(1000)
    ));
    a.state.config.as_mut().unwrap()["values"]["mcp"]["max_listed_tools"] = 1.into();
    a.effects.clear();
    press(&mut a, &["left"]);
    assert!(matches!(
        a.effects.last(),
        Some(Effect::Sync(nexus_tui::sync::Command::PatchConfig { path, value, .. }))
            if path == "mcp.max_listed_tools" && value == &serde_json::json!(1)
    ));
}

#[test]
fn voice_settings_refresh_consent_remove_and_track_progress() {
    let mut a = app();
    a.execute("app.settings");
    let status_call = a
        .effects
        .iter()
        .find_map(|effect| match effect {
            Effect::Api(call @ ApiCall::VoiceSettingsStatus { .. }) => Some(call.clone()),
            _ => None,
        })
        .expect("opening Settings refreshes voice status");
    a.apply_done(ActionDone {
        call: status_call,
        result: Ok(Done {
            voice: Some(serde_json::json!({
                "stt": {
                    "state": "absent", "repo": "ConwayResearch/Underdog-Bark-3.0-Clean",
                    "revision": "main", "size": 181_000_000, "cache_path": null, "error": null
                },
                "tts": {
                    "state": "absent", "repo": "gradium/phonon-31m-en",
                    "revision": "main", "size": 65_000_000, "cache_path": null, "error": null
                }
            })),
            ..Done::default()
        }),
    });
    a.effects.clear();

    let position = |a: &App, id: &str| {
        let rows = a.settings_rows();
        let items = a.settings_items();
        rows.iter()
            .position(|at| matches!(&items[*at], SettingsItem::Row { row, .. } if row.id == id))
            .unwrap_or_else(|| panic!("missing Settings row {id}"))
    };
    a.ui.settings_row = position(&a, "dictation.stt_model");
    let (_, rows) = draw(&mut a, 120, 36);
    let frame = rows.join("\n");
    assert!(frame.contains("State: absent"), "{frame}");
    assert!(
        frame.contains("ConwayResearch/Underdog-Bark-3.0-Clean"),
        "{frame}"
    );
    assert!(frame.contains("181 MB"), "{frame}");

    let remove_row = a
        .settings_items()
        .into_iter()
        .find_map(|item| match item {
            SettingsItem::Row { row, .. } if row.id == "dictation.stt_remove" => Some(*row),
            _ => None,
        })
        .unwrap();
    assert!(a.settings_reason(&remove_row, None).is_some());

    a.ui.settings_row = position(&a, "dictation.stt_download");
    press(&mut a, &["enter"]);
    let dialog = a.ui.dialog.as_ref().expect("download asks before consent");
    assert_eq!(dialog.button, 0, "Cancel is the default selection");
    let consent = dialog.body.join(" ");
    assert!(consent.contains("181 MB"));
    assert!(consent.contains("from Hugging Face into the standard cache"));
    press(&mut a, &["enter"]);
    assert!(a.ui.dialog.is_none());
    assert!(!a
        .effects
        .iter()
        .any(|effect| matches!(effect, Effect::Api(ApiCall::VoiceModel { .. }))));

    a.ui.settings_row = position(&a, "dictation.stt_download");
    press(&mut a, &["enter", "right", "enter"]);
    assert!(matches!(
        a.effects.last(),
        Some(Effect::Api(ApiCall::VoiceModel {
            target: nexus_tui::actions::VoiceTarget::Stt,
            operation: nexus_tui::actions::VoiceOperation::Download
        }))
    ));
    let action = a.effects.pop().unwrap();
    let Effect::Api(action) = action else {
        panic!("expected API call")
    };
    a.apply_done(ActionDone {
        call: action,
        result: Ok(Done {
            voice: Some(serde_json::json!({"state":"downloading"})),
            ..Done::default()
        }),
    });
    assert!(a
        .effects
        .iter()
        .any(|effect| matches!(effect, Effect::Api(ApiCall::VoiceSettingsStatus { .. }))));
    let refresh = a
        .effects
        .iter()
        .find_map(|effect| match effect {
            Effect::Api(call @ ApiCall::VoiceSettingsStatus { .. }) => Some(call.clone()),
            _ => None,
        })
        .unwrap();
    a.effects.clear();
    a.apply_done(ActionDone {
        call: refresh,
        result: Ok(Done {
            voice: Some(serde_json::json!({
                "stt": {"state":"downloading","repo":"ConwayResearch/Underdog-Bark-3.0-Clean","size":181_000_000},
                "tts": {
                    "state":"runtime_missing", "repo":"gradium/phonon-31m-en",
                    "size":65_000_000, "cache_path":"/cache/models--gradium--phonon-31m-en",
                    "error":"Install the voice runtime with nexus voice init"
                }
            })),
            ..Done::default()
        }),
    });

    a.ui.settings_row = position(&a, "dictation.tts_model");
    let (_, rows) = draw(&mut a, 120, 36);
    let frame = rows.join("\n");
    assert!(frame.contains("65 MB"), "{frame}");
    assert!(
        frame.contains("/cache/models--gradium--phonon-31m-en"),
        "{frame}"
    );
    assert!(frame.contains("nexus voice init"), "{frame}");

    a.ui.voice_status.as_mut().unwrap()["tts"]["state"] = "ready".into();
    a.ui.voice_status.as_mut().unwrap()["tts"]["error"] = serde_json::Value::Null;
    a.ui.settings_row = position(&a, "dictation.tts_remove");
    press(&mut a, &["enter"]);
    let dialog = a.ui.dialog.as_ref().expect("remove requires confirmation");
    assert_eq!(dialog.button, 0, "Cancel is the default selection");
    let removal = dialog.body.join(" ");
    assert_eq!(
        removal,
        "Only gradium/phonon-31m-en is removed from the Hugging Face cache."
    );
    press(&mut a, &["right", "enter"]);
    assert!(matches!(
        a.effects.first(),
        Some(Effect::Api(ApiCall::VoiceModel {
            target: nexus_tui::actions::VoiceTarget::Tts,
            operation: nexus_tui::actions::VoiceOperation::Remove
        }))
    ));

    a.effects.clear();
    a.ui.voice_status.as_mut().unwrap()["tts"]["state"] = "downloading".into();
    a.apply_sync(SyncMsg::Event(global_event(
        &a,
        80_001,
        "voice.progress",
        serde_json::json!({
            "target": "tts", "state": "downloading", "bytes_done": 32_500_000,
            "bytes_total": 65_000_000, "error": null
        }),
    )));
    assert!(a
        .voice_info_value(nexus_tui::actions::VoiceTarget::Tts)
        .contains("50%"));
    a.effects.clear();
    a.apply_sync(SyncMsg::Event(global_event(
        &a,
        80_002,
        "voice.progress",
        serde_json::json!({
            "target": "tts", "state": "ready", "bytes_done": 65_000_000,
            "bytes_total": 65_000_000, "error": null
        }),
    )));
    assert!(a
        .effects
        .iter()
        .any(|effect| matches!(effect, Effect::Api(ApiCall::VoiceSettingsStatus { .. }))));
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
    // grouped when nothing is typed (D13): each group's enabled commands come
    // before its disabled ones, and a group is contiguous
    let group = |i: &nexus_tui::ui::PaletteItem| nexus_tui::paint::palette_group(&i.category);
    let mut seen: Vec<String> = Vec::new();
    for g in all.chunk_by(|a, b| group(a) == group(b)) {
        let name = group(&g[0]);
        assert!(!seen.contains(&name), "{name} is split");
        seen.push(name);
        if let Some(d) = g.iter().position(|i| i.reason.is_some()) {
            assert!(g[d..].iter().all(|i| i.reason.is_some()));
        }
    }
    assert_eq!(seen[0], "Session");
    assert!(seen.contains(&"Model and agent".to_owned()));
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
        .find(|i| i.kind == PaletteKind::Command("composer.steer".into()))
        .expect("steering command is shown");
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
    // handoff needs an idle session; a running one is refused with the manifest's reason
    let handoff = a.manifest.command("session.handoff").unwrap().clone();
    assert!(a.availability(&handoff).is_ok());
    busy(&mut a);
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
    assert!(blocked.len() >= 4);
    for c in &blocked {
        assert!(a.availability(c).is_err(), "{}", c.id);
        a.run_command(&c.id);
    }
    assert!(a.effects.is_empty(), "disabled commands do nothing");
}

#[test]
fn dictation_shortcuts_check_status_before_starting_capture() {
    let mut a = app();
    press(&mut a, &["ctrl+d"]);
    assert_eq!(a.ui.dictation.phase, Phase::Checking);
    assert!(matches!(
        a.effects.last(),
        Some(Effect::Api(ApiCall::VoiceStatus { generation }))
            if *generation == a.ui.dictation.generation
    ));

    a.ui.dictation.cancel();
    a.effects.clear();
    press(&mut a, &["ctrl+x", "v"]);
    assert_eq!(a.ui.dictation.phase, Phase::Checking);
    assert!(matches!(
        a.effects.last(),
        Some(Effect::Api(ApiCall::VoiceStatus { .. }))
    ));
}

#[test]
fn enter_stops_and_sends_escape_stops_and_keeps_and_typing_stops() {
    let mut sent = app();
    sent.composer().set_text("typed already");
    let generation = sent.ui.dictation.begin(SESSION.to_owned());
    sent.ui.dictation.phase = Phase::Recording;
    sent.effects.clear();
    press(&mut sent, &["enter"]);
    assert_eq!(sent.ui.dictation.phase, Phase::Stopping);
    assert!(sent.ui.dictation.send_after);
    assert!(sent
        .effects
        .contains(&Effect::Dictation(CaptureCommand::Stop { generation })));
    assert_eq!(sent.composer_ref().text(), "typed already");

    let mut kept = app();
    kept.composer().set_text("keep this draft");
    kept.ui.dictation.begin(SESSION.to_owned());
    kept.ui.dictation.phase = Phase::Recording;
    press(&mut kept, &["esc"]);
    assert_eq!(kept.ui.dictation.phase, Phase::Stopping);
    assert!(!kept.ui.dictation.send_after);
    // Escape again, while transcribing, discards.
    let generation = kept.ui.dictation.generation;
    kept.effects.clear();
    press(&mut kept, &["esc"]);
    assert_eq!(kept.ui.dictation.phase, Phase::Idle);
    assert!(kept
        .effects
        .contains(&Effect::Dictation(CaptureCommand::Cancel { generation })));

    let mut typing = app();
    typing.ui.dictation.begin(SESSION.to_owned());
    typing.ui.dictation.phase = Phase::Recording;
    press(&mut typing, &["x"]);
    assert_eq!(typing.ui.dictation.phase, Phase::Stopping);
    assert_eq!(typing.composer_ref().text(), "x", "the key still applies");
}

#[test]
fn absent_stt_status_requires_repository_and_size_consent() {
    let mut a = app();
    let generation = a.ui.dictation.begin(SESSION.to_owned());
    a.apply_done(ActionDone {
        call: ApiCall::VoiceStatus { generation },
        result: Ok(Done {
            voice: Some(serde_json::json!({
                "stt": {
                    "state": "absent",
                    "repo": "ConwayResearch/Underdog-Bark-3.0-Clean",
                    "size": 181_000_000
                }
            })),
            ..Done::default()
        }),
    });
    assert_eq!(a.ui.dictation.phase, Phase::Consent);
    let dialog = a.ui.dialog.as_ref().unwrap();
    let body = dialog.body.join(" ");
    assert!(body.contains("ConwayResearch/Underdog-Bark-3.0-Clean"));
    assert!(body.contains("181 MB"));
    assert!(a.effects.is_empty(), "the model download waits for consent");

    press(&mut a, &["right", "enter"]);
    assert_eq!(a.ui.dictation.phase, Phase::Downloading);
    assert!(matches!(
        a.effects.last(),
        Some(Effect::Api(ApiCall::VoiceDownload { generation: got })) if *got == generation
    ));
}

#[test]
fn stt_ready_progress_wins_a_race_with_the_status_request() {
    let mut a = app();
    press(&mut a, &["ctrl+d"]);
    let generation = a.ui.dictation.generation;
    let log_generation = a.state.model.generation.as_deref().unwrap();
    let event = serde_json::json!({
        "id": 100_000,
        "cursor": format!("{log_generation}:100000"),
        "type": "voice.progress",
        "time": 1.0,
        "data": {"model":"stt", "state":"ready", "bytes_done":181_000_000, "bytes_total":181_000_000}
    });
    let Frame::Event(event) = Frame::from_value("voice.progress", None, &event).unwrap() else {
        panic!("expected a durable voice progress event");
    };
    a.apply_sync(SyncMsg::Event(event));
    assert_eq!(a.ui.dictation.generation, generation);
    assert_eq!(a.ui.dictation.phase, Phase::Starting);
    assert!(a
        .effects
        .contains(&Effect::Dictation(CaptureCommand::Start { generation })));
}

#[test]
fn live_words_sit_inside_the_composer_and_a_square_marks_recording() {
    let mut a = app();
    a.composer().set_text("typed text");
    a.ui.dictation.begin(SESSION.to_owned());
    a.ui.dictation.anchor = "typed".len();
    a.ui.dictation.phase = Phase::Recording;
    a.ui.dictation.preview = "pending words".into();
    let (_, rows) = draw(&mut a, 120, 36);
    let frame = rows.join("\n");
    assert!(frame.contains("typed pending words text"), "{frame}");
    assert!(!frame.contains("REC"), "{frame}");
    assert!(frame.contains('■'), "{frame}");
    assert_eq!(
        a.composer_ref().text(),
        "typed text",
        "the draft itself is untouched"
    );
}

/// Writes an SVG snapshot of a simulated recording state when `I4_SHOTS` is set.
/// It exercises the real Ratatui renderer without requiring a microphone.
#[test]
#[ignore]
fn dictation_strip_shot() {
    let Ok(dir) = std::env::var("I4_SHOTS") else {
        return;
    };
    let mut a = app();
    a.composer().set_text("typed draft stays editable");
    a.ui.dictation.begin(SESSION.to_owned());
    a.ui.dictation.phase = Phase::Recording;
    a.ui.dictation.levels.extend([
        0.02, 0.06, 0.18, 0.48, 0.83, 0.5, 0.21, 0.08, 0.28, 0.72, 0.4, 0.12,
    ]);
    a.ui.dictation.preview = "A pending transcript preview".into();
    let (buf, _) = draw(&mut a, 120, 36);
    let (fg, bg) = nexus_tui::svg::page_colors(false);
    std::fs::write(
        format!("{dir}/dictation-strip-120x36.svg"),
        nexus_tui::svg::to_svg(&buf, fg, bg),
    )
    .unwrap();
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
    let headings: Vec<String> = a
        .settings_items()
        .into_iter()
        .filter_map(|item| match item {
            nexus_tui::app::SettingsItem::Heading(label) => Some(label),
            _ => None,
        })
        .collect();
    let manifest: Vec<String> = a
        .manifest
        .settings_for_tui()
        .into_iter()
        .filter(|s| !s.row.is_empty())
        .map(|s| s.label)
        .collect();
    assert_eq!(headings, manifest);
    let (_, rows) = draw(&mut a, 120, 36);
    assert!(rows.join("\n").contains("APPEARANCE"));
    // down moves through the rows; esc closes
    press(&mut a, &["down", "down"]);
    assert_eq!(a.ui.settings_row, 2);
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
            mode: "send".into(),
            images: Vec::new(),
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
fn composer_moves_and_selects_by_word() {
    let mut a = app();
    a.composer().set_text("one two three");
    press(&mut a, &["alt+left", "alt+shift+left"]);
    assert_eq!(a.composer_ref().selected_text().as_deref(), Some("two "));
    typing(&mut a, "new ");
    assert_eq!(a.composer_ref().text(), "one new three");
}

#[test]
fn composer_ctrl_arrows_move_and_select_to_line_edges() {
    let mut a = app();
    a.composer().set_text("first line\nsecond line\nthird line");
    press(&mut a, &["up", "ctrl+left", "ctrl+shift+right"]);
    assert_eq!(
        a.composer_ref().selected_text().as_deref(),
        Some("second line")
    );
    press(&mut a, &["ctrl+right", "ctrl+shift+left"]);
    assert_eq!(
        a.composer_ref().selected_text().as_deref(),
        Some("second line")
    );
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
            mode: "send".into(),
            images: Vec::new(),
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
    let x = a.regions.composer.x + 5;
    let y = a.regions.composer.y + 2;
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
    let x = a.regions.composer.x + 5;
    let y = a.regions.composer.y + 2;
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
    let x = a.regions.composer.x + 5;
    let y = a.regions.composer.y + 2;
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

/// Feed one stored event for `session` (ids continue the fixture's log).
fn feed(a: &mut nexus_tui::app::App, id: u64, kind: &str, session: &str, data: &str) {
    let sse = format!(
        "id: log_c915bfc9b6d826c4:{id}\nevent: {kind}\ndata: {{\"id\": {id}, \"seq\": {id}, \"time\": 0, \"type\": \"{kind}\", \"session_id\": \"{session}\", \"data\": {data}, \"cursor\": \"log_c915bfc9b6d826c4:{id}\"}}\n\n"
    );
    for e in common::events(&sse) {
        a.apply_sync(nexus_tui::sync::SyncMsg::Event(e));
    }
}

#[test]
fn terminal_notifications_fire_only_for_sessions_out_of_view() {
    let mut a = app(); // viewing SESSION
    feed(
        &mut a,
        17,
        "session.created",
        "ses_2",
        r#"{"id": "ses_2", "title": "Other", "agent": "build"}"#,
    );
    a.effects.clear();
    let approval =
        r#"{"id": "per_2", "session_id": "ses_2", "tool": "bash", "call_id": "c", "args": {}}"#;
    let waiting = Effect::Notify("Nexus: Other needs approval".into());

    // the default is waiting only: another session's approval is announced
    assert_eq!(a.ui.notify, Notify::Waiting);
    feed(&mut a, 18, "permission.asked", "ses_2", approval);
    assert!(a.effects.contains(&waiting), "{:?}", a.effects);

    // the session in view is never announced
    a.effects.clear();
    feed(
        &mut a,
        19,
        "permission.asked",
        common::SESSION,
        r#"{"id": "per_3", "session_id": "ses_1a17de160eccc428a38", "tool": "bash", "call_id": "d", "args": {}}"#,
    );
    assert!(
        !a.effects.iter().any(|e| matches!(e, Effect::Notify(_))),
        "{:?}",
        a.effects
    );

    // off announces nothing
    a.ui.notify = Notify::Off;
    a.effects.clear();
    feed(
        &mut a,
        20,
        "question.asked",
        "ses_2",
        r#"{"id": "qst_2", "session_id": "ses_2", "call_id": "e", "created": 0, "questions": []}"#,
    );
    assert!(a.effects.is_empty(), "{:?}", a.effects);

    // waiting only: a finished turn is not announced
    a.ui.notify = Notify::Waiting;
    feed(
        &mut a,
        21,
        "turn_end",
        "ses_2",
        r#"{"type": "turn_end", "stop": "end_turn", "steps": 1}"#,
    );
    assert!(a.effects.is_empty(), "{:?}", a.effects);

    // all: finished turns and errors are announced too
    a.ui.notify = Notify::All;
    feed(
        &mut a,
        22,
        "turn_end",
        "ses_2",
        r#"{"type": "turn_end", "stop": "end_turn", "steps": 1}"#,
    );
    assert!(a
        .effects
        .contains(&Effect::Notify("Nexus: Other finished".into())));
    feed(
        &mut a,
        23,
        "session.error",
        "ses_2",
        r#"{"message": "boom"}"#,
    );
    assert!(a
        .effects
        .contains(&Effect::Notify("Nexus: Other failed".into())));
}

#[test]
fn terminal_notifications_setting_has_its_default_and_cycles() {
    assert_eq!(Notify::default(), Notify::Waiting);
    assert_eq!(Notify::Off.label(), "off");
    assert_eq!(Notify::Waiting.label(), "waiting only");
    assert_eq!(Notify::All.label(), "waiting, done and errors");
    assert!(App::settings_wired("notifications.terminal"));
}

#[test]
fn agent_and_model_chosen_before_a_session_exists_go_into_its_creation() {
    use nexus_tui::actions::ApiCall;
    let mut a = app();
    a.state.model.select(None);
    a.state.agents = vec![
        serde_json::json!({"name": "build", "source": "builtin"}),
        serde_json::json!({"name": "plan", "source": "builtin"}),
    ];
    a.state.models = vec![serde_json::json!({"ref": "codex/gpt-5", "name": "GPT-5"})];
    a.composer().set_text("/agent");
    press(&mut a, &["enter"]);
    typing(&mut a, "plan");
    press(&mut a, &["enter"]);
    assert_eq!(a.ui.draft.agent.as_deref(), Some("plan"));
    assert_eq!(a.current_agent(), "plan");
    a.composer().set_text("/model");
    press(&mut a, &["enter"]);
    press(&mut a, &["enter"]);
    assert_eq!(a.ui.draft.model.as_deref(), Some("codex/gpt-5"));
    a.effects.clear();
    a.composer().set_text("hello");
    press(&mut a, &["enter"]);
    assert!(a.effects.iter().any(|e| matches!(
        e,
        Effect::Api(ApiCall::NewAndPrompt { fields, .. })
            if fields["agent"] == "plan" && fields["model"] == "codex/gpt-5"
    )));
    assert_eq!(a.ui.draft, Default::default(), "the draft is spent");
}

#[test]
fn image_paste_sends_an_image_only_prompt_and_restores_it_on_failure() {
    use nexus_tui::actions::ApiCall;
    let mut a = app();
    let mut image = nexus_tui::images::Image::from_png(
        "clipboard.png".into(),
        include_bytes!("../fixtures/clipboard.png"),
    )
    .unwrap();
    a.apply_image_paste(SESSION.into(), Ok(Some(image.clone())));
    let (_, rows) = draw(&mut a, 120, 36);
    let preview_row = rows
        .iter()
        .position(|row| row.contains("1 attached"))
        .unwrap();
    let input_row = rows
        .iter()
        .position(|row| row.contains("[Image A]"))
        .unwrap();
    assert!(
        preview_row < input_row,
        "attachments appear above the composer"
    );
    image.name = "Image A".into();
    press(&mut a, &["enter"]);
    let call = a
        .effects
        .iter()
        .find_map(|e| {
            if let Effect::Api(c @ ApiCall::Prompt { images, text, .. }) = e {
                assert_eq!(images, std::slice::from_ref(&image));
                assert_eq!(text, "[Image A]");
                Some(c.clone())
            } else {
                None
            }
        })
        .expect("image-only send");
    assert!(a.composer_ref().images.is_empty());
    a.apply_done(nexus_tui::actions::ActionDone {
        call,
        result: Err("offline".into()),
    });
    assert_eq!(a.composer_ref().images, vec![image]);
    press(&mut a, &["backspace"]);
    assert!(a.composer_ref().images.is_empty());
}

#[test]
fn old_nexus_shortcuts_open_available_actions() {
    let mut a = app();
    press(&mut a, &["ctrl+g"]);
    assert!(matches!(
        a.ui.overlay.as_ref().map(|o| &o.kind),
        Some(nexus_tui::ui::OverlayKind::Palette(_))
    ));
    press(&mut a, &["esc", "ctrl+x", "s"]);
    assert_eq!(a.ui.view, View::Settings);
    press(&mut a, &["esc", "ctrl+o"]);
    assert!(a.ui.overlay.is_some());
}

#[test]
fn pasted_images_get_distinct_markers_and_removal_clears_the_last() {
    let mut a = app();
    let image = nexus_tui::images::Image::from_png(
        "clipboard.png".into(),
        include_bytes!("../fixtures/clipboard.png"),
    )
    .unwrap();
    a.apply_image_paste(SESSION.into(), Ok(Some(image.clone())));
    a.composer().insert_str(" compare with ");
    a.apply_image_paste(SESSION.into(), Ok(Some(image)));
    assert_eq!(a.composer_ref().text(), "[Image A] compare with [Image B]");
    assert_eq!(a.composer_ref().images[1].name, "Image B");
    press(&mut a, &["backspace"]);
    assert_eq!(a.composer_ref().text(), "[Image A] compare with ");
    assert_eq!(a.composer_ref().images.len(), 1);
}

#[test]
fn busy_composer_steers_queues_and_completion_wins_tab() {
    use nexus_tui::actions::ApiCall;
    let mut a = app();
    busy(&mut a);
    typing(&mut a, "run linter");
    press(&mut a, &["enter"]);
    assert!(a.effects.iter().any(|e| matches!(e, Effect::Api(ApiCall::Prompt { mode, text, .. }) if mode == "steer" && text == "run linter")));
    a.effects.clear();
    typing(&mut a, "write changelog");
    press(&mut a, &["tab"]);
    assert!(a.effects.iter().any(|e| matches!(e, Effect::Api(ApiCall::Prompt { mode, text, .. }) if mode == "queue" && text == "write changelog")));
    a.effects.clear();
    press(&mut a, &["tab"]);
    assert!(!a
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Api(ApiCall::Prompt { .. }))));
    typing(&mut a, "/mod");
    press(&mut a, &["tab"]);
    assert!(!a
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Api(ApiCall::Prompt { .. }))));
}

#[test]
fn pending_input_is_selected_and_removed_for_restoration() {
    use nexus_tui::actions::ApiCall;
    let mut a = app();
    a.state.inputs.insert(
        SESSION.into(),
        serde_json::json!({"held": true, "inputs": [
            {"id": "in_1", "mode": "queue", "parts": [{"type": "text", "text": "follow up"}]}
        ]}),
    );
    press(&mut a, &["up", "backspace"]);
    assert!(a.effects.iter().any(|e| matches!(e, Effect::Api(ApiCall::Input { id, action, .. }) if id == "in_1" && action == "remove")));
}

#[test]
fn core_history_recalls_multiline_entries_whole_and_keeps_the_draft() {
    let mut a = app();
    a.composer()
        .set_history(vec!["older".into(), "line one\nline two".into()]);
    typing(&mut a, "mine");
    press(&mut a, &["up"]);
    assert_eq!(a.composer_ref().text(), "line one\nline two");
    // Up inside a recalled multi-line entry moves the cursor first.
    press(&mut a, &["up"]);
    assert_eq!(a.composer_ref().text(), "line one\nline two");
    assert_eq!(a.composer_ref().cursor().0, 0);
    press(&mut a, &["up"]);
    assert_eq!(a.composer_ref().text(), "older");
    press(&mut a, &["down"]);
    assert_eq!(a.composer_ref().text(), "line one\nline two");
    press(&mut a, &["down"]);
    press(&mut a, &["down"]);
    assert_eq!(a.composer_ref().text(), "mine");
}

#[test]
fn up_in_the_middle_of_a_multiline_draft_moves_the_cursor() {
    let mut a = app();
    a.composer().set_history(vec!["older".into()]);
    typing(&mut a, "a");
    press(&mut a, &["ctrl+j"]);
    typing(&mut a, "b");
    press(&mut a, &["up"]);
    assert_eq!(a.composer_ref().text(), "a\nb");
    assert!(!a.composer_ref().in_history());
}

fn message_event(a: &mut App, id: i64, seq: i64, role: &str, delivery: Option<&str>, text: &str) {
    let mut data = serde_json::json!({"id": format!("m_h{id}"), "session_id": SESSION, "seq": seq, "created": 1793113300.0,
        "role": role, "parts": [{"type": "text", "text": text}]});
    if let Some(d) = delivery {
        data["delivery"] = d.into();
    }
    let v = serde_json::json!({"id": id, "type": "message.added", "session_id": SESSION,
        "cursor": format!("log_c915bfc9b6d826c4:{id}"), "data": data});
    if let Ok(Frame::Event(e)) = Frame::from_value("message.added", None, &v) {
        a.apply_sync(SyncMsg::Event(e));
    }
}

#[test]
fn handoff_summary_is_a_three_line_box_that_opens_whole() {
    let mut a = empty(opts(Mode::Dark, Tier::TrueColor), (110, 34));
    a.open_session(SESSION);
    a.effects.clear();
    let summary = "[Handoff from session ses_abcdef123456]\n\n## Goal\nFix the login bug.\n## State\nHalf done.\n## Decisions\nNone.\n## Next steps\nRun the tests.";
    message_event(&mut a, 900, 1, "user", Some("handoff"), summary);
    let (_, rows) = draw(&mut a, 110, 34);
    let text = rows.join("\n");
    assert!(text.contains("Continued from ses_abcdef12"), "{text}");
    assert!(text.contains("Fix the login bug."));
    assert!(text.contains("more lines · enter show all"));
    assert!(!text.contains("Run the tests."), "only the first rows show");
    let y = rows
        .iter()
        .position(|r| r.contains("Continued from"))
        .unwrap() as u16;
    click(&mut a, 40, y);
    match &a.ui.overlay {
        Some(nexus_tui::ui::Overlay {
            kind: OverlayKind::ContextDetail { title, text, .. },
            ..
        }) => {
            assert!(title.starts_with("Handoff from"));
            assert!(text.contains("Run the tests."));
        }
        other => panic!("expected the full summary, got {other:?}"),
    }
}

#[test]
fn handoff_and_btw_commands_call_core() {
    let mut a = app();
    a.run_command("session.handoff");
    assert!(a
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Api(ApiCall::Handoff { .. }))));
    a.effects.clear();
    typing(&mut a, "/btw what is the port?");
    press(&mut a, &["enter"]);
    assert!(a.effects.iter().any(|e| matches!(e, Effect::Api(ApiCall::Btw { question, .. }) if question == "what is the port?")));
}

#[test]
fn handoff_threshold_setting_steps_by_five_and_zero_is_off() {
    let mut a = app();
    a.state.config = Some(
        serde_json::json!({"revision": "cfg-1", "values": {"context": {"handoff_percent": 5}}}),
    );
    a.execute("app.settings");
    let rows = a.settings_rows();
    let items = a.settings_items();
    a.ui.settings_row = rows
        .iter()
        .position(|at| matches!(&items[*at], SettingsItem::Row { row, .. } if row.id == "context.handoff_percent"))
        .unwrap();
    press(&mut a, &["left"]);
    assert!(
        matches!(a.effects.last(), Some(Effect::Sync(nexus_tui::sync::Command::PatchConfig { path, value, .. }))
        if path == "context.handoff_percent" && value == &serde_json::json!(0))
    );
}

#[test]
fn btw_answer_opens_in_an_overlay_without_touching_the_timeline() {
    let mut a = app();
    let before = draw(&mut a, 110, 34).1.join("\n");
    a.apply_done(ActionDone {
        call: ApiCall::Btw {
            session: SESSION.into(),
            question: "what is the port?".into(),
        },
        result: Ok(Done {
            answer: Some("It is 4747.".into()),
            ..Done::default()
        }),
    });
    let (_, rows) = draw(&mut a, 110, 34);
    let text = rows.join("\n");
    assert!(text.contains("Side question"), "{text}");
    assert!(text.contains("It is 4747."));
    assert!(text.contains("Not added to the session's context."));
    press(&mut a, &["esc"]);
    assert!(a.ui.overlay.is_none());
    assert_eq!(draw(&mut a, 110, 34).1.join("\n"), before);
}

#[test]
fn continued_sessions_link_back_and_title_models_are_editable() {
    let mut a = app();
    a.run_command("session.open_origin");
    assert!(a.effects.is_empty(), "no link, nothing to open");
    let origin = a.manifest.command("session.open_origin").unwrap().clone();
    assert!(a.availability(&origin).is_err());
    let id = a.viewed_id().unwrap().to_owned();
    // the viewed session continued from another one the list knows
    let mut other = a.state.model.sessions.get(&id).unwrap().clone();
    other.id = "ses_original".into();
    other.title = "Original work".into();
    a.state.model.sessions.insert(other.id.clone(), other);
    a.state.model.sessions.get_mut(&id).unwrap().continued_from = Some("ses_original".into());
    assert!(a.availability(&origin).is_ok());
    a.run_command("session.open_origin");
    assert_eq!(a.viewed_id(), Some("ses_original"));

    a.state.config = Some(serde_json::json!({"revision": "cfg-1",
        "values": {"context": {"handoff_percent": 80, "title_models": ["copilot/gpt-6-luna", "codex/gpt-6-luna"]}}}));
    a.execute("app.settings");
    let rows = a.settings_rows();
    let items = a.settings_items();
    a.ui.settings_row = rows
        .iter()
        .position(|at| matches!(&items[*at], SettingsItem::Row { row, .. } if row.id == "context.title_models"))
        .unwrap();
    press(&mut a, &["enter"]);
    let edit = a.ui.edit.as_ref().expect("an inline editor opens");
    assert_eq!(edit.buf.text(), "copilot/gpt-6-luna, codex/gpt-6-luna");
    a.ui.edit
        .as_mut()
        .unwrap()
        .buf
        .set_text("copilot/gpt-6-luna");
    a.effects.clear();
    press(&mut a, &["enter"]);
    assert!(
        matches!(a.effects.last(), Some(Effect::Sync(nexus_tui::sync::Command::PatchConfig { path, value, .. }))
        if path == "context.title_models" && value == &serde_json::json!(["copilot/gpt-6-luna"]))
    );
}

#[test]
fn core_answers_the_starting_context_of_the_current_draft_only() {
    let mut a = nexus_tui::app::App::new(
        nexus_tui::state::AppState::new(),
        nexus_tui::theme::Theme::new(opts(Mode::Dark, Tier::TrueColor)),
        (120, 36),
    );
    a.state.apply(SyncMsg::Snapshot {
        snapshot: Box::new(common::snapshot()),
        cause: nexus_tui::sync::ResyncCause::Initial,
    });
    a.state
        .apply(SyncMsg::Connection(nexus_tui::state::ConnectionState::Live));
    a.effects.clear();
    let request = a.draft_request().expect("a project and no viewed session");
    a.tick(std::time::Instant::now());
    let asked = |a: &App| {
        a.effects
            .iter()
            .filter(|e| matches!(e, Effect::Sync(nexus_tui::sync::Command::DraftContext(_))))
            .count()
    };
    assert_eq!(asked(&a), 1);
    a.tick(std::time::Instant::now());
    assert_eq!(asked(&a), 1, "an unchanged draft is asked once");
    a.apply_sync(SyncMsg::DraftContext {
        request,
        data: Ok(serde_json::json!({"used": 1234, "parts": {"tools": 9}})),
    });
    assert_eq!(
        a.draft_context().map(|d| d["used"].as_u64()),
        Some(Some(1234))
    );
    a.ui.draft.effort = Some("high".into());
    assert!(
        a.draft_context().is_none(),
        "an answer for another draft is not shown"
    );
    a.tick(std::time::Instant::now());
    assert_eq!(asked(&a), 2);
}

#[test]
fn ctrl_u_reads_plan_limits_into_a_bar_modal() {
    let mut app = app();
    app.effects.clear();
    press(&mut app, &["ctrl+u"]);
    assert!(app
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Api(ApiCall::Usage))));
    let usage = serde_json::json!({"providers": [
        {"id": "codex", "label": "ChatGPT (Codex)", "plan": "plus", "notes": [], "windows": [
            {"label": "5-hour", "used_percent": 42.0, "resets_at": 1e12},
            {"label": "weekly", "used_percent": 95.0, "resets_at": null}]},
        {"id": "copilot", "label": "GitHub Copilot", "error": "timed out", "windows": []}]});
    app.apply_done(ActionDone {
        call: ApiCall::Usage,
        result: Ok(Done {
            usage: Some(usage),
            ..Done::default()
        }),
    });
    assert!(matches!(
        app.ui.overlay.as_ref().map(|o| &o.kind),
        Some(OverlayKind::Usage { .. })
    ));
    let (_, rows) = draw(&mut app, 120, 36);
    let text = rows.join("\n");
    assert!(text.contains("Plan limits"), "{text}");
    assert!(text.contains("ChatGPT (Codex)"), "{text}");
    assert!(text.contains(" 42%"), "{text}");
    assert!(text.contains(" 95%"), "{text}");
    assert!(
        text.contains("Could not read the limits: timed out"),
        "{text}"
    );
    assert!(text.contains("r refresh"), "{text}");
    app.effects.clear();
    press(&mut app, &["r"]);
    assert!(app
        .effects
        .iter()
        .any(|e| matches!(e, Effect::Api(ApiCall::Usage))));
    press(&mut app, &["esc"]);
    assert!(app.ui.overlay.is_none());
}

#[test]
fn a_new_session_keeps_the_drafts_tools_until_its_snapshot_loads() {
    let mut a = app();
    a.state.model.selected.as_mut().unwrap().starting = None;
    assert!(a.context_items().iter().all(|item| item.key != "skill"));
    a.state.apply(nexus_tui::sync::SyncMsg::DraftContext {
        request: serde_json::json!({}),
        data: Ok(serde_json::json!({"tools": ["read", "skill"]})),
    });
    assert!(a.context_items().iter().any(|item| item.key == "skill"));
}

#[test]
fn the_draft_shows_the_model_core_will_create_the_session_with() {
    let mut a = nexus_tui::app::App::new(
        nexus_tui::state::AppState::new(),
        nexus_tui::theme::Theme::new(opts(Mode::Dark, Tier::TrueColor)),
        (120, 36),
    );
    a.state.apply(nexus_tui::sync::SyncMsg::Snapshot {
        snapshot: Box::new(common::snapshot()),
        cause: nexus_tui::sync::ResyncCause::Initial,
    });
    let Some(request) = a.draft_request() else {
        return;
    };
    a.state.apply(nexus_tui::sync::SyncMsg::DraftContext {
        request,
        data: Ok(serde_json::json!({"agent": "plan", "session": {"model": "codex/configured", "effort": "high"}})),
    });
    assert_eq!(a.effective_model(None), "codex/configured");
    assert_eq!(a.current_agent(), "plan");
    assert_eq!(a.current_effort(), "high");
}
