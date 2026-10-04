//! Desktop bindings for actions handled by the native shell.
use gpui::KeyBinding;

use crate::{
    Agents, Attach, Cancel, CompleteFirst, ContextPopover, CycleAgent, CycleEffort, DetailsNext,
    DetailsPrevious, Dismiss, FavoriteModel, FocusNext, ForkSession, HistoryNext, HistoryPrevious,
    InspectContext, InterruptSubmit, JumpLatest, Models, NewSession, Palette, PreviousFocus,
    QueueSubmit, Quit, Reconnect, RefreshModels, SaveForm, Sessions, Settings, Shortcuts,
    SortModels, ToggleDetails, ToggleLogs, ToggleSessions, TranscriptPageDown, TranscriptPageUp,
    UpdateHelp, Usage, Voice,
};

pub fn key_bindings() -> Vec<KeyBinding> {
    vec![
        // Keep the baseline shortcuts aligned with the shared terminal table.
        KeyBinding::new("ctrl-p", Palette, Some("Nexus")),
        KeyBinding::new("ctrl-n", NewSession, Some("Nexus")),
        KeyBinding::new("ctrl-o", Sessions, Some("Nexus")),
        KeyBinding::new("ctrl-f", ForkSession, Some("Nexus")),
        KeyBinding::new("ctrl-g", Agents, Some("Nexus")),
        KeyBinding::new("ctrl-b", ToggleSessions, Some("Nexus")),
        KeyBinding::new("ctrl-l", ToggleDetails, Some("Nexus")),
        KeyBinding::new("ctrl-s", Settings, Some("Nexus")),
        KeyBinding::new("ctrl-i", InspectContext, Some("Nexus")),
        KeyBinding::new("ctrl-t", CycleEffort, Some("Nexus")),
        KeyBinding::new("ctrl-space", Voice, Some("Nexus")),
        KeyBinding::new("ctrl-e", ToggleLogs, Some("Nexus")),
        KeyBinding::new("ctrl-u", Usage, Some("Nexus")),
        KeyBinding::new("ctrl-c", Cancel, Some("Nexus")),
        KeyBinding::new("ctrl-r", Reconnect, Some("Nexus")),
        KeyBinding::new("ctrl-end", JumpLatest, Some("Nexus")),
        KeyBinding::new("ctrl-q", Quit, Some("Nexus")),
        KeyBinding::new("escape", Dismiss, Some("Nexus")),
        // Leader chord mirrors nexus.ui_support.shortcuts.LEADER_SHORTCUTS. The
        // shared table includes model, voice, session, picker, and panel routes.
        KeyBinding::new("ctrl-x m", Models, Some("Nexus")),
        KeyBinding::new("ctrl-x v", Voice, Some("Nexus")),
        KeyBinding::new("ctrl-x n", NewSession, Some("Nexus")),
        KeyBinding::new("ctrl-x o", Sessions, Some("Nexus")),
        KeyBinding::new("ctrl-x f", ForkSession, Some("Nexus")),
        KeyBinding::new("ctrl-x g", Agents, Some("Nexus")),
        KeyBinding::new("ctrl-x b", ToggleSessions, Some("Nexus")),
        KeyBinding::new("ctrl-x l", ToggleDetails, Some("Nexus")),
        KeyBinding::new("ctrl-x s", Settings, Some("Nexus")),
        KeyBinding::new("ctrl-x i", InspectContext, Some("Nexus")),
        KeyBinding::new("ctrl-x e", ToggleLogs, Some("Nexus")),
        KeyBinding::new("ctrl-x t", CycleEffort, Some("Nexus")),
        KeyBinding::new("ctrl-x u", Usage, Some("Nexus")),
        KeyBinding::new("ctrl-x r", Reconnect, Some("Nexus")),
        KeyBinding::new("ctrl-x c", ContextPopover, Some("Nexus")),
        KeyBinding::new("ctrl-x z", UpdateHelp, Some("Nexus")),
        KeyBinding::new("ctrl-x ?", Shortcuts, Some("Nexus")),
        // Transcript and details navigation mirrors the terminal scoped keys.
        // These avoid the editor so typing is never intercepted.
        KeyBinding::new("a", Agents, Some("Nexus && !Editor && !ModelPanel")),
        KeyBinding::new("pageup", TranscriptPageUp, Some("Nexus && !Editor")),
        KeyBinding::new("pagedown", TranscriptPageDown, Some("Nexus && !Editor")),
        KeyBinding::new("[", DetailsPrevious, Some("Nexus && !Editor")),
        KeyBinding::new("]", DetailsNext, Some("Nexus && !Editor")),
        // Tab navigates the transcript when the draft is empty and forces a
        // completion otherwise; focus traversal moves to Ctrl+Tab.
        KeyBinding::new("tab", CompleteFirst, Some("Nexus")),
        KeyBinding::new("ctrl-tab", FocusNext, Some("Nexus")),
        KeyBinding::new("ctrl-shift-tab", PreviousFocus, Some("Nexus")),
        // Editor/form action bindings.
        KeyBinding::new("shift-tab", CycleAgent, Some("Nexus")),
        KeyBinding::new("ctrl-enter", QueueSubmit, Some("Editor")),
        KeyBinding::new("alt-enter", InterruptSubmit, Some("Editor")),
        KeyBinding::new("up", HistoryPrevious, Some("Editor")),
        KeyBinding::new("down", HistoryNext, Some("Editor")),
        KeyBinding::new("cmd-s", SaveForm, Some("Nexus")),
        KeyBinding::new("ctrl-s", SaveForm, Some("Form")),
        KeyBinding::new("ctrl-a", Attach, Some("Nexus && Input")),
        KeyBinding::new("ctrl-f", FavoriteModel, Some("Nexus && ModelPanel")),
        KeyBinding::new("ctrl-s", SortModels, Some("Nexus && ModelPanel")),
        KeyBinding::new("ctrl-r", RefreshModels, Some("Nexus && ModelPanel")),
    ]
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashSet;

    #[test]
    fn bindings_have_unique_keystrokes() {
        let mut seen = HashSet::new();
        for binding in key_bindings() {
            let key = format!("{:?} {:?}", binding.keystrokes(), binding.predicate());
            assert!(seen.insert(key.clone()), "duplicate key binding: {key}");
        }
    }

    #[test]
    fn every_bound_action_has_a_handler() {
        // The shell wires every bound action with `.on_action` in main.rs. A
        // binding without a handler would silently swallow the key, so this
        // guards the keymap against drift from the render tree.
        let source = include_str!("main.rs");
        for binding in key_bindings() {
            let name = binding.action().name();
            let short = name.rsplit("::").next().unwrap_or(name);
            assert!(
                source.contains(&format!("&{short}")),
                "bound action {name} has no `&{short}` handler in main.rs"
            );
        }
    }

    #[test]
    fn native_routes_match_canonical_shortcut_actions() {
        let bindings = key_bindings();
        let matches_keystrokes = |binding: &KeyBinding, key: &str| {
            let expected = key
                .split_whitespace()
                .map(|stroke| gpui::Keystroke::parse(stroke).unwrap())
                .collect::<Vec<_>>();
            binding.keystrokes().len() == expected.len()
                && binding
                    .keystrokes()
                    .iter()
                    .zip(expected.iter())
                    .all(|(actual, expected)| actual.inner() == expected)
        };
        for (key, action, context) in [
            ("ctrl-f", "ForkSession", "Nexus"),
            ("ctrl-x f", "ForkSession", "Nexus"),
            ("ctrl-end", "JumpLatest", "Nexus"),
            ("ctrl-enter", "QueueSubmit", "Editor"),
            ("ctrl-x c", "ContextPopover", "Nexus"),
            ("ctrl-x z", "UpdateHelp", "Nexus"),
            ("ctrl-x ?", "Shortcuts", "Nexus"),
            ("a", "Agents", "Nexus"),
            ("pageup", "TranscriptPageUp", "Nexus"),
            ("pagedown", "TranscriptPageDown", "Nexus"),
            ("[", "DetailsPrevious", "Nexus"),
            ("]", "DetailsNext", "Nexus"),
            ("ctrl-s", "SaveForm", "Form"),
            ("ctrl-f", "FavoriteModel", "ModelPanel"),
            ("ctrl-s", "SortModels", "ModelPanel"),
            ("ctrl-r", "RefreshModels", "ModelPanel"),
        ] {
            assert!(
                bindings.iter().any(|binding| {
                    binding.action().name().ends_with(action)
                        && matches_keystrokes(binding, key)
                        && format!("{:?}", binding.predicate()).contains(context)
                }),
                "missing native route {key} -> {action}; bindings: {bindings:?}"
            );
        }
        assert!(!bindings.iter().any(|binding| {
            binding.action().name() == "Attach" && matches_keystrokes(binding, "tab")
        }));
        assert!(!bindings.iter().any(|binding| {
            binding.action().name() == "QueueSubmit" && matches_keystrokes(binding, "enter")
        }));
    }
}
