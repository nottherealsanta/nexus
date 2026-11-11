use crate::api::Event;
use serde_json::Value;
use std::collections::BTreeMap;
use std::time::Instant;

pub struct Btw {
    pub parent: String,
    pub question: String,
    pub request_id: String,
    pub session: Option<String>,
    pub inherited: u64,
    pub inherited_context: String,
    pub inherited_open: bool,
    pub context_open: bool,
    pub calls: u64,
    pub tools: BTreeMap<String, String>,
    tool_order: Vec<String>,
    pub messages: Vec<Value>,
    pub answer: String,
    pub running: bool,
    pub refreshing: bool,
    pub synced: bool,
    pub last_refresh: Option<Instant>,
    head: i64,
    partials: BTreeMap<String, u64>,
}

impl Btw {
    pub fn new(parent: String, question: String) -> Self {
        Self {
            parent,
            question,
            request_id: uuid::Uuid::new_v4().to_string(),
            session: None,
            inherited: 0,
            inherited_context: String::new(),
            inherited_open: false,
            context_open: false,
            calls: 0,
            tools: BTreeMap::new(),
            tool_order: Vec::new(),
            messages: Vec::new(),
            answer: String::new(),
            running: true,
            refreshing: false,
            synced: false,
            last_refresh: None,
            head: 0,
            partials: BTreeMap::new(),
        }
    }

    fn tool_event(&mut self, kind: &str, d: &Value) {
        if kind == "code.call" {
            let id = format!("{}:{}:{}", d["parent_call_id"], d["cell"], d["index"]);
            if !self.tools.contains_key(&id) {
                self.tool_order.push(id.clone());
            }
            self.tools.insert(
                id,
                format!(
                    "- `{}` · {} (nested)",
                    d["tool"].as_str().unwrap_or("tool"),
                    if d["error"].is_null() {
                        "done"
                    } else {
                        "refused or failed"
                    }
                ),
            );
        } else {
            let call = &d["call"];
            if let Some(id) = call["id"].as_str() {
                if !self.tools.contains_key(id) {
                    self.tool_order.push(id.into());
                }
                self.tools.insert(
                    id.into(),
                    format!(
                        "- `{}` · {}",
                        call["name"].as_str().unwrap_or("tool"),
                        if kind == "tool_start" {
                            "running"
                        } else if d["result"]["is_error"] == true {
                            "refused or failed"
                        } else {
                            "done"
                        }
                    ),
                );
            }
        }
    }

    pub fn event(&mut self, e: &Event) -> bool {
        let d = &e.data;
        if e.kind == "btw.started"
            && e.session_id.as_deref() == Some(&self.parent)
            && d["request_id"].as_str() == Some(&self.request_id)
        {
            self.session = d["session"].as_str().map(str::to_owned);
            self.inherited = d["inherited"].as_u64().unwrap_or(0);
            return true;
        }
        if self.session.is_none() || e.session_id != self.session {
            return false;
        }
        if e.id > 0 && e.id <= self.head {
            return false;
        }
        if e.id > 0 {
            self.head = e.id;
        }
        if let (Some(req), Some(seq)) = (d["request_id"].as_str(), d["seq"].as_u64()) {
            let seen = self.partials.entry(req.into()).or_default();
            if seq <= *seen {
                return false;
            }
            *seen = seq;
        }
        match e.kind.as_str() {
            "step_start" => self.answer.clear(),
            "btw.budget" => self.calls = d["calls"].as_u64().unwrap_or(self.calls),
            "tool_start" | "tool_end" | "code.call" => self.tool_event(&e.kind, d),
            "text_delta" => self.answer.push_str(d["text"].as_str().unwrap_or("")),
            "message.added" => {
                if !self.messages.iter().any(|m| m["id"] == d["id"]) {
                    self.messages.push(d.clone());
                }
                if d["role"] == "assistant" {
                    self.answer.clear();
                }
            }
            "session.error" => {
                self.answer = d["message"]
                    .as_str()
                    .unwrap_or("Side question failed")
                    .into()
            }
            "session.status" if d["status"] == "idle" => self.running = false,
            _ => return false,
        }
        true
    }

    pub fn snapshot(&mut self, state: &Value) {
        self.refreshing = false;
        if state["head"].as_i64().unwrap_or(0) < self.head {
            return;
        }
        let partial = &state["partial"];
        if let Some(req) = partial["request_id"].as_str() {
            let seq = partial["seq"].as_u64().unwrap_or(0);
            if self.partials.get(req).is_some_and(|seen| *seen > seq) {
                return;
            }
            self.partials.insert(req.into(), seq);
        }
        self.head = state["head"].as_i64().unwrap_or(self.head);
        self.calls = state["calls"].as_u64().unwrap_or(self.calls);
        self.inherited = state["inherited"].as_u64().unwrap_or(self.inherited);
        self.running = state["running"].as_bool().unwrap_or(false);
        self.synced = !self.running;
        self.messages = state["messages"].as_array().cloned().unwrap_or_default();
        self.answer = partial["parts"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|p| p["type"] == "text")
            .filter_map(|p| p["text"].as_str())
            .collect::<Vec<_>>()
            .join("\n");
        if let Some(error) = state["error"].as_str() {
            self.answer = error.into();
        }
        self.tools.clear();
        self.tool_order.clear();
        for e in state["events"].as_array().into_iter().flatten() {
            self.tool_event(e["type"].as_str().unwrap_or(""), &e["data"]);
        }
    }

    pub fn detail(&self) -> String {
        let inherited = if self.inherited_open {
            format!("\n\n```json\n{}\n```", self.inherited_context)
        } else {
            String::new()
        };
        let mut transcript = String::new();
        for m in &self.messages {
            transcript.push_str(&format!(
                "\n\n**{}**\n\n",
                m["role"].as_str().unwrap_or("message")
            ));
            for p in m["parts"].as_array().into_iter().flatten() {
                if p["type"] == "text" {
                    transcript.push_str(p["text"].as_str().unwrap_or(""));
                } else {
                    transcript.push_str(&format!(
                        "\n```json\n{}\n```\n",
                        serde_json::to_string_pretty(p).unwrap_or_default()
                    ));
                }
            }
        }
        let answers = self
            .messages
            .iter()
            .filter(|m| m["role"] == "assistant")
            .flat_map(|m| m["parts"].as_array().into_iter().flatten())
            .filter(|p| p["type"] == "text")
            .filter_map(|p| p["text"].as_str())
            .collect::<Vec<_>>()
            .join("\n\n");
        format!("Inherited context · {} messages · [i] {}{}\n\nRead-only side question · {}/10 tool calls · {}\n\n{}\n\n{}\n\n{}\n\nContext · [c] {}{}\n\nNot added to the session's context.", self.inherited, if self.inherited_open { "collapse" } else { "expand" }, inherited, self.calls, if self.running { "Running…" } else { "Finished" }, self.tool_order.iter().filter_map(|id| self.tools.get(id).cloned()).collect::<Vec<_>>().join("\n"), answers, self.answer, if self.context_open { "collapse" } else { "expand" }, if self.context_open { transcript } else { String::new() })
    }
}
