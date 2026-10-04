//! Optimistic presentation toggles with ordered persistence acknowledgements.
use crate::{bridge::Snapshot, input};
use serde_json::{json, Value};
use std::{collections::HashMap, io};
#[derive(Default)]
pub struct LocalUi {
    sequence: u64,
    pending: HashMap<String, (u64, Value)>,
}
impl LocalUi {
    fn apply(action: &Value, s: &mut Snapshot) {
        match action["type"].as_str().unwrap_or("") {
            "toggle" => match action["key"].as_str().unwrap_or("") {
                "sessions_sidebar" => {
                    s.sessions_sidebar = action["value"].as_bool().unwrap_or(s.sessions_sidebar)
                }
                "details_sidebar" => {
                    s.details_sidebar = action["value"].as_bool().unwrap_or(s.details_sidebar)
                }
                _ => {}
            },
            "details_tab" => {
                s.details_panel.tab = action["text"].as_str().unwrap_or("Session").to_owned()
            }
            "file_toggle" => {
                if let Some(file) = s
                    .details_panel
                    .files
                    .iter_mut()
                    .find(|file| Some(file.path.as_str()) == action["text"].as_str())
                {
                    file.open = action["value"].as_bool().unwrap_or(file.open);
                }
            }
            "logs" => {
                s.details_sidebar = action["open"].as_bool().unwrap_or(true);
                s.details_panel.tab = "Logs".into();
            }
            "logs_fold" => {
                s.logs_show_all = action["value"].as_bool().unwrap_or(false);
            }
            _ => {}
        }
        if s.details_panel.tab == "Logs" {
            s.logs = if s.logs_show_all {
                s.logs_all.clone()
            } else {
                s.logs_folded.clone()
            };
        }
        s.ui_local_revision += 1;
    }
    pub fn dispatch(&mut self, mut action: Value, s: &mut Snapshot) -> io::Result<()> {
        if !s.local_ui_enabled {
            return input::send(action);
        }
        let kind = action["type"].as_str().unwrap_or("").to_owned();
        let key = match kind.as_str() {
            "toggle" => {
                let key = action["key"].as_str().unwrap_or("").to_owned();
                let value = match key.as_str() {
                    "sessions_sidebar" => !s.sessions_sidebar,
                    "details_sidebar" => !s.details_sidebar,
                    _ => return input::send(action),
                };
                action["value"] = json!(value);
                key
            }
            "file_toggle" => {
                let path = action["text"].as_str().unwrap_or("").to_owned();
                action["value"] = json!(!s
                    .details_panel
                    .files
                    .iter()
                    .find(|file| file.path == path)
                    .is_some_and(|file| file.open));
                format!("file:{path}")
            }
            "logs_fold" => {
                action["value"] = json!(!s.logs_show_all);
                "logs_fold".into()
            }
            "details_tab" => "details_tab".into(),
            "logs" => "logs".into(),
            _ => return input::send(action),
        };
        self.sequence += 1;
        action["ui_sequence"] = json!(self.sequence);
        action["generation"] = json!(s.generation);
        Self::apply(&action, s);
        if self.pending.len() >= 260 && !self.pending.contains_key(&key) {
            return Err(io::Error::other("too many unacknowledged UI toggles"));
        }
        self.pending.insert(key, (self.sequence, action.clone()));
        input::send(action)
    }
    pub fn reconcile(&mut self, next: &mut Snapshot, old: &Snapshot) {
        if next.generation != old.generation {
            self.pending.clear();
        }
        self.pending.retain(|_, (seq, _)| *seq > next.ui_ack);
        let mut pending: Vec<_> = self.pending.values().collect();
        pending.sort_by_key(|(seq, _)| *seq);
        next.ui_local_revision = old.ui_local_revision;
        for (_, action) in pending {
            Self::apply(action, next);
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn stale_echo_preserves_newer_local_choice() {
        let mut ui = LocalUi::default();
        ui.pending.insert(
            "details_tab".into(),
            (2, json!({"type":"details_tab","text":"Files"})),
        );
        let old = Snapshot {
            generation: 1,
            ..Default::default()
        };
        let mut echo = Snapshot {
            generation: 1,
            ui_ack: 1,
            ..Default::default()
        };
        ui.reconcile(&mut echo, &old);
        assert_eq!(echo.details_panel.tab, "Files");
        echo.ui_ack = 2;
        ui.reconcile(&mut echo, &old);
        assert!(ui.pending.is_empty());
    }
}
