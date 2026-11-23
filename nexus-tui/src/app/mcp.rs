//! Settings › MCP: the list of servers (global and the project's), adding a remote one, and one server's
//! detail modal (sign in, switch off, restart, remove). NSH's `ui/mcp-settings.tsx` draws the same thing.
//!
//! Core does the work (`docs/mcp.md#managing-servers`): this only shows `GET /api/mcp` and the project's
//! `GET /api/project/{id}/mcp` rows and sends the writes. A server's secret values never reach the client;
//! a header is typed as `Name: value`, sent once, and only its name comes back.

use serde_json::{json, Value};

use super::*;
use crate::actions::McpOp;

/// A server as the page keys it: scope and name (the same name may exist in both scopes).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct McpKey {
    pub project: Option<String>,
    pub name: String,
}

pub fn state_label(row: &Value) -> String {
    let tools = row["tools"].as_u64().unwrap_or(0);
    match row["state"].as_str().unwrap_or("") {
        "connected" if tools == 1 => "connected \u{b7} 1 tool".into(),
        "connected" if tools > 0 => format!("connected \u{b7} {tools} tools"),
        "failed" if row["auth"].as_str() == Some("required") => "sign-in required".into(),
        "idle" if row["auth"].as_str() == Some("none") => "not signed in".into(),
        "" => String::new(),
        other => other.replace('_', " "),
    }
}

fn is_remote(row: &Value) -> bool {
    matches!(row["transport"].as_str(), Some("http" | "sse"))
}

impl App {
    /// Ask Core for the server list (global, plus this project's when one is open).
    pub fn refresh_mcp(&mut self) {
        self.api(ApiCall::McpList {
            project: self.current_project(),
        });
    }

    pub fn page_summary_mcp(&self) -> String {
        let Some(list) = self.ui.mcp_servers.as_ref() else {
            return "loading\u{2026}".into();
        };
        let rows = list["servers"].as_array().map_or(0, Vec::len);
        let waiting = list["servers"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|r| r["auth"].as_str() == Some("required"))
            .count();
        match (rows, waiting) {
            (0, _) => "none yet".into(),
            (n, 0) => format!("{n} server{}", if n == 1 { "" } else { "s" }),
            (n, w) => format!("{n} servers \u{b7} {w} to sign in"),
        }
    }

    fn mcp_row(&self, key: &McpKey) -> Option<Value> {
        self.ui
            .mcp_servers
            .as_ref()?
            .get("servers")?
            .as_array()?
            .iter()
            .find(|r| {
                r["server"].as_str() == Some(key.name.as_str())
                    && (r["scope"].as_str() == Some("project")) == key.project.is_some()
            })
            .cloned()
    }

    fn mcp_key_of(row: &Value) -> McpKey {
        McpKey {
            project: (row["scope"].as_str() == Some("project"))
                .then(|| row["project_id"].as_str().unwrap_or_default().to_owned()),
            name: row["server"].as_str().unwrap_or_default().to_owned(),
        }
    }

    /// The MCP page: add, then every server with its state.
    pub fn mcp_page_lines(&self) -> Vec<PageLine> {
        let mut out = vec![PageLine::Item {
            label: "Add a remote server".into(),
            value: "address".into(),
            tone: Tone::Secondary,
            action: PageAction::McpAdd { project: None },
        }];
        if let Some(project) = self.current_project() {
            out.push(PageLine::Item {
                label: "Add to this project".into(),
                value: ".mcp.json".into(),
                tone: Tone::Secondary,
                action: PageAction::McpAdd {
                    project: Some(project),
                },
            });
        }
        let Some(list) = self.ui.mcp_servers.as_ref() else {
            out.push(PageLine::Text(
                "Loading servers\u{2026}".into(),
                Tone::Muted,
            ));
            return out;
        };
        let rows: Vec<&Value> = list["servers"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|r| r["server"].as_str() != Some("(file)"))
            .collect();
        if let Some(error) = list["project_file"]["error"].as_str() {
            out.push(PageLine::Text(
                format!("Project MCP files: {error}"),
                Tone::Error,
            ));
        }
        for scope in ["global", "project"] {
            let mine: Vec<&&Value> = rows
                .iter()
                .filter(|r| r["scope"].as_str() == Some(scope))
                .collect();
            if mine.is_empty() {
                continue;
            }
            out.push(PageLine::Heading(
                if scope == "global" {
                    "Every project"
                } else {
                    "This project"
                }
                .into(),
            ));
            for row in mine {
                let state = state_label(row);
                let tone = match row["state"].as_str().unwrap_or("") {
                    "connected" => Tone::Secondary,
                    "failed" | "invalid" => Tone::Warning,
                    _ => Tone::Muted,
                };
                out.push(PageLine::Item {
                    label: row["server"].as_str().unwrap_or_default().to_owned(),
                    value: format!("{state} \u{b7} open"),
                    tone,
                    action: PageAction::McpOpen(Self::mcp_key_of(row)),
                });
            }
        }
        if rows.is_empty() {
            out.push(PageLine::Text(
                "No MCP servers yet. A remote one needs only its address; local ones are added with `nexus config import-mcp`."
                    .into(),
                Tone::Muted,
            ));
        }
        out
    }

    /// One server's detail modal: what it is, then one action per row.
    pub fn mcp_detail_lines(&self, key: &McpKey) -> Vec<PageLine> {
        let Some(row) = self.mcp_row(key) else {
            return vec![PageLine::Text(
                format!("Server {} is gone.", key.name),
                Tone::Error,
            )];
        };
        let login = self.ui.mcp_logins.get(&Self::mcp_login_key(key));
        let status = login.and_then(|l| l["status"].as_str()).unwrap_or("");
        let mut out = vec![PageLine::Text(
            format!(
                "{} \u{b7} {}",
                state_label(&row),
                if key.project.is_some() {
                    "this project"
                } else {
                    "every project"
                }
            ),
            Tone::Secondary,
        )];
        if let Some(url) = row["url"].as_str() {
            out.push(PageLine::Text(url.to_owned(), Tone::Muted));
        } else if let Some(command) = row["command"].as_str() {
            out.push(PageLine::Text(
                format!(
                    "{command} \u{b7} {} arguments",
                    row["arg_count"].as_u64().unwrap_or(0)
                ),
                Tone::Muted,
            ));
        }
        if let Some(error) = row["error"].as_str().filter(|e| !e.is_empty()) {
            out.push(PageLine::Text(error.to_owned(), Tone::Warning));
        }
        let names: Vec<&str> = row["header_names"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .collect();
        if !names.is_empty() {
            out.push(PageLine::Text(
                format!("headers {} (values never shown)", names.join(", ")),
                Tone::Muted,
            ));
        }
        if let Some(file) = row["file"].as_str() {
            out.push(PageLine::Text(format!("in {file}"), Tone::Muted));
        }
        let enabled = row["enabled"].as_bool().unwrap_or(true);
        if is_remote(&row) && row["oauth"].as_bool() == Some(true) {
            if status == "pending" {
                if let Some(url) = login.and_then(|l| l["verification_uri"].as_str()) {
                    out.push(PageLine::Item {
                        label: url.to_owned(),
                        value: "enter opens".into(),
                        tone: Tone::Secondary,
                        action: PageAction::OpenUrl(url.to_owned()),
                    });
                } else {
                    out.push(PageLine::Text(
                        "Starting sign-in\u{2026}".into(),
                        Tone::Muted,
                    ));
                }
                out.push(PageLine::Text(
                    "Waiting for authorization\u{2026}".into(),
                    Tone::Muted,
                ));
                out.push(PageLine::Item {
                    label: "Cancel sign-in".into(),
                    value: "enter".into(),
                    tone: Tone::Muted,
                    action: PageAction::McpRun(key.clone(), "cancel".into()),
                });
            } else {
                if status == "failed" {
                    out.push(PageLine::Text(
                        login
                            .and_then(|l| l["message"].as_str())
                            .unwrap_or("Sign-in failed")
                            .to_owned(),
                        Tone::Error,
                    ));
                }
                let signed = row["auth"].as_str() == Some("signed_in");
                out.push(PageLine::Item {
                    label: if signed { "Sign in again" } else { "Sign in" }.into(),
                    value: "enter".into(),
                    tone: Tone::Secondary,
                    action: PageAction::McpRun(key.clone(), "sign_in".into()),
                });
                if signed {
                    out.push(PageLine::Item {
                        label: "Sign out".into(),
                        value: "enter".into(),
                        tone: Tone::Muted,
                        action: PageAction::McpRun(key.clone(), "sign_out".into()),
                    });
                }
            }
        }
        if is_remote(&row) {
            out.push(PageLine::Item {
                label: "Add a header".into(),
                value: "Name: value".into(),
                tone: Tone::Secondary,
                action: PageAction::McpRun(key.clone(), "header".into()),
            });
            if key.project.is_some() {
                out.push(PageLine::Text(
                    "This file may be committed: write a secret as ${env:NAME}, not the value."
                        .into(),
                    Tone::Muted,
                ));
            }
        }
        out.push(PageLine::Item {
            label: if enabled { "Switch off" } else { "Switch on" }.into(),
            value: "enter".into(),
            tone: Tone::Secondary,
            action: PageAction::McpRun(key.clone(), "toggle".into()),
        });
        out.push(PageLine::Item {
            label: "Restart".into(),
            value: "enter".into(),
            tone: Tone::Secondary,
            action: PageAction::McpRun(key.clone(), "restart".into()),
        });
        out.push(PageLine::Item {
            label: "Remove".into(),
            value: "enter".into(),
            tone: Tone::Warning,
            action: PageAction::McpRun(key.clone(), "remove".into()),
        });
        out.push(PageLine::Text(
            "New sessions use the change; running sessions keep the servers they started with."
                .into(),
            Tone::Muted,
        ));
        out
    }

    pub fn mcp_detail_items(&self, key: &McpKey) -> Vec<usize> {
        self.mcp_detail_lines(key)
            .iter()
            .enumerate()
            .filter(|(_, l)| matches!(l, PageLine::Item { .. }))
            .map(|(i, _)| i)
            .collect()
    }

    pub fn mcp_detail_action(&self, key: &McpKey) -> Option<PageAction> {
        let lines = self.mcp_detail_lines(key);
        let items = self.mcp_detail_items(key);
        let at = *items.get(self.ui.mcp_detail_row.min(items.len().saturating_sub(1)))?;
        match lines.into_iter().nth(at)? {
            PageLine::Item { action, .. } => Some(action),
            _ => None,
        }
    }

    pub(super) fn mcp_detail_key(&mut self, name: &str) -> bool {
        let Some(key) = self.ui.mcp_detail.clone() else {
            return false;
        };
        let last = self.mcp_detail_items(&key).len().saturating_sub(1);
        match name {
            "esc" => self.ui.mcp_detail = None,
            "up" => self.ui.mcp_detail_row = self.ui.mcp_detail_row.saturating_sub(1),
            "down" => self.ui.mcp_detail_row = (self.ui.mcp_detail_row + 1).min(last),
            "pageup" => self.ui.mcp_detail_row = self.ui.mcp_detail_row.saturating_sub(6),
            "pagedown" => self.ui.mcp_detail_row = (self.ui.mcp_detail_row + 6).min(last),
            "home" => self.ui.mcp_detail_row = 0,
            "end" => self.ui.mcp_detail_row = last,
            "enter" => {
                if let Some(action) = self.mcp_detail_action(&key) {
                    self.page_activate(action);
                }
            }
            _ => {}
        }
        true
    }

    fn mcp_login_key(key: &McpKey) -> String {
        format!(
            "{}:{}",
            key.project.as_deref().unwrap_or("global"),
            key.name
        )
    }

    pub(super) fn mcp_add(&mut self, project: Option<String>) {
        let restore = self.restore_point();
        self.ui.edit = Some(TextEdit {
            label: if project.is_some() {
                "MCP server address, added to this project (https://\u{2026})".into()
            } else {
                "MCP server address (https://\u{2026})".into()
            },
            target: EditTarget::McpAddress { project },
            buf: Composer::new(),
            restore,
        });
    }

    /// A finished address or header edit.
    pub(super) fn mcp_edit_done(&mut self, target: &EditTarget, text: String) {
        match target {
            EditTarget::McpAddress { project } => {
                let address = text.trim();
                if !(address.starts_with("https://") || address.starts_with("http://")) {
                    self.toast(Level::Warning, "The address must start with https://");
                    return;
                }
                self.api(ApiCall::Mcp {
                    project: project.clone(),
                    op: McpOp::Add {
                        entry: json!({ "url": address }),
                    },
                });
            }
            EditTarget::McpHeader { project, name } => {
                let Some((header, value)) = text.split_once(':') else {
                    self.toast(Level::Warning, "Write the header as Name: value");
                    return;
                };
                let (header, value) = (header.trim(), value.trim());
                if header.is_empty() || value.is_empty() {
                    self.toast(Level::Warning, "Write the header as Name: value");
                    return;
                }
                self.api(ApiCall::Mcp {
                    project: project.clone(),
                    op: McpOp::Patch {
                        name: name.clone(),
                        entry: json!({ "headers": { header: value } }),
                    },
                });
            }
            _ => {}
        }
    }

    pub(super) fn mcp_run(&mut self, key: McpKey, what: &str) {
        let project = key.project.clone();
        let name = key.name.clone();
        match what {
            "sign_in" => {
                self.ui
                    .mcp_logins
                    .insert(Self::mcp_login_key(&key), json!({"status": "pending"}));
                self.api(ApiCall::Mcp {
                    project,
                    op: McpOp::SignIn { name },
                });
            }
            "cancel" | "sign_out" => {
                self.ui.mcp_logins.remove(&Self::mcp_login_key(&key));
                self.api(ApiCall::Mcp {
                    project,
                    op: McpOp::SignOut { name },
                });
            }
            "restart" => self.api(ApiCall::Mcp {
                project,
                op: McpOp::Restart { name },
            }),
            "toggle" => {
                let on = self
                    .mcp_row(&key)
                    .and_then(|r| r["enabled"].as_bool())
                    .unwrap_or(true);
                self.api(ApiCall::Mcp {
                    project,
                    op: McpOp::Patch {
                        name,
                        entry: json!({ "enabled": if on { json!(false) } else { Value::Null } }),
                    },
                });
            }
            "header" => {
                let restore = self.restore_point();
                self.ui.edit = Some(TextEdit {
                    label: format!("Header for {name}: Name: value"),
                    target: EditTarget::McpHeader { project, name },
                    buf: Composer::new(),
                    restore,
                });
            }
            "remove" => {
                let restore = self.restore_point();
                self.ui.dialog = Some(Dialog {
                    title: format!("Remove {name}?"),
                    body: vec![
                        if project.is_some() {
                            "It is deleted from this project's MCP file.".into()
                        } else {
                            "It is deleted from your MCP settings.".into()
                        },
                        "Its sign-in is forgotten. Running sessions keep what they started with."
                            .into(),
                    ],
                    cancel: "Cancel".into(),
                    confirm: "Remove".into(),
                    danger: true,
                    action: Confirmed::RemoveMcp(project, name),
                    button: 0,
                    restore,
                });
            }
            _ => {}
        }
    }

    /// An MCP call finished: keep the list current and say what happened.
    pub(super) fn mcp_done(&mut self, call: &ApiCall, result: Result<Done, String>) {
        match (call, result) {
            (ApiCall::McpList { .. }, Ok(done)) => {
                self.ui.mcp_servers = done.mcp;
                self.ui.mcp_error = None;
            }
            (ApiCall::McpList { .. }, Err(error)) => self.ui.mcp_error = Some(error),
            (ApiCall::Mcp { project, op }, Ok(done)) => {
                if let McpOp::Add { .. } = op {
                    let row = done.mcp.unwrap_or(Value::Null);
                    let name = row["server"].as_str().unwrap_or_default().to_owned();
                    self.toast(Level::Success, format!("Added {name}"));
                    // A server that wants a sign-in opens on its own detail, ready for Enter.
                    let key = McpKey {
                        project: project.clone(),
                        name,
                    };
                    self.ui.mcp_detail = Some(key);
                    self.ui.mcp_detail_row = 0;
                } else if let McpOp::SignIn { name } = op {
                    let key = McpKey {
                        project: project.clone(),
                        name: name.clone(),
                    };
                    let mut state = done.mcp.unwrap_or_else(|| json!({"status": "pending"}));
                    if state["status"].is_null() {
                        state = json!({"status": "pending"});
                    }
                    self.ui.mcp_logins.insert(Self::mcp_login_key(&key), state);
                } else if let McpOp::Remove { name } = op {
                    if self.ui.mcp_detail.as_ref().is_some_and(|k| k.name == *name) {
                        self.ui.mcp_detail = None;
                    }
                }
                self.refresh_mcp();
            }
            (ApiCall::Mcp { .. }, Err(_)) => self.refresh_mcp(),
            _ => {}
        }
    }

    /// `mcp.login`: a sign-in's link, then its end.
    pub(super) fn mcp_login_event(&mut self, data: &Value) {
        let name = data["server"].as_str().unwrap_or_default().to_owned();
        let key = McpKey {
            project: (data["scope"].as_str() == Some("project"))
                .then(|| data["project_id"].as_str().unwrap_or_default().to_owned()),
            name: name.clone(),
        };
        let status = data["status"].as_str().unwrap_or_default();
        match status {
            "signed_out" => {
                self.ui.mcp_logins.remove(&Self::mcp_login_key(&key));
            }
            _ => {
                self.ui
                    .mcp_logins
                    .insert(Self::mcp_login_key(&key), data.clone());
            }
        }
        match status {
            "signed_in" => self.toast(Level::Success, format!("Signed in to {name}")),
            "failed" => self.toast(
                Level::Error,
                data["message"]
                    .as_str()
                    .unwrap_or("Sign-in failed")
                    .to_owned(),
            ),
            _ => {}
        }
        if status == "pending" {
            if let Some(url) = data["verification_uri"].as_str() {
                // The link is the point: open it for the user, who asked for the sign-in.
                if self
                    .ui
                    .mcp_opened
                    .insert(Self::mcp_login_key(&key), url.to_owned())
                    != Some(url.to_owned())
                {
                    self.effects.push(Effect::OpenUrl(url.to_owned()));
                }
            }
        } else {
            self.ui.mcp_opened.remove(&Self::mcp_login_key(&key));
        }
        if status != "pending" {
            self.refresh_mcp();
        }
    }
}
