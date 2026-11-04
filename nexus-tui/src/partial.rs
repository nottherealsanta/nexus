//! Request-scoped streaming checkpoints, independent of durable event cursors.
use serde_json::{json, Value};

#[derive(Debug, Clone, PartialEq, Default)]
pub struct Partial {
    pub request_id: String,
    pub seq: u64,
    pub parts: Vec<Value>,
    pub seeded: bool,
    pub pending: Vec<Value>,
}

impl Partial {
    pub fn event(&mut self, data: &Value) {
        let Some(request) = data["request_id"].as_str() else {
            return;
        };
        if self.request_id != request {
            *self = Self {
                request_id: request.into(),
                ..Self::default()
            };
        }
        let seq = data["seq"].as_u64().unwrap_or(0);
        if seq <= self.seq {
            return;
        }
        if self.seeded && seq > self.seq + 1 {
            self.seeded = false;
        }
        if !self.seeded && seq == 1 {
            self.seeded = true;
        }
        if !self.seeded {
            self.pending.push(data.clone());
            return;
        }
        self.fold(data);
    }

    pub fn seed(&mut self, checkpoint: &Value) {
        let Some(request) = checkpoint["request_id"].as_str() else {
            return;
        };
        if !self.request_id.is_empty() && self.request_id != request {
            return;
        }
        let seq = checkpoint["seq"].as_u64().unwrap_or(0);
        if self.seeded && self.seq >= seq {
            return;
        }
        self.request_id = request.into();
        self.seq = seq;
        self.parts = checkpoint["parts"].as_array().cloned().unwrap_or_default();
        self.seeded = true;
        let mut pending = std::mem::take(&mut self.pending);
        pending.sort_by_key(|d| d["seq"].as_u64().unwrap_or(0));
        for data in pending {
            if data["seq"].as_u64().unwrap_or(0) > self.seq {
                self.fold(&data);
            }
        }
    }

    fn fold(&mut self, data: &Value) {
        let i = data["part_index"].as_u64().unwrap_or(0) as usize;
        if i > 100_000 {
            return;
        }
        while self.parts.len() <= i {
            self.parts.push(Value::Null);
        }
        match data["type"].as_str().unwrap_or("") {
            "part_done" => self.parts[i] = data["part"].clone(),
            "text_delta" | "reasoning_delta" => {
                let reasoning = data["type"] == "reasoning_delta";
                let field = if reasoning { "summary" } else { "text" };
                if self.parts[i].is_null() {
                    self.parts[i] =
                        json!({"type": if reasoning { "reasoning" } else { "text" }, field: ""});
                }
                let mut text = self.parts[i][field].as_str().unwrap_or("").to_owned();
                text.push_str(data["text"].as_str().unwrap_or(""));
                self.parts[i][field] = Value::String(text);
            }
            _ => {} // Tool arguments are diagnostic and not rendered as text.
        }
        self.seq = data["seq"].as_u64().unwrap_or(self.seq);
    }

    pub fn text(&self) -> (String, String) {
        let mut text = String::new();
        let mut reasoning = String::new();
        for p in &self.parts {
            if p["type"] == "text" {
                text.push_str(p["text"].as_str().unwrap_or(""));
            }
            if p["type"] == "reasoning" {
                reasoning.push_str(p["summary"].as_str().unwrap_or(""));
            }
        }
        (text, reasoning)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn shared_join_checkpoint_race_and_completed_part() {
        let scenario: Value =
            serde_json::from_str(include_str!("../../interface/scenarios/live-partial.json"))
                .unwrap();
        let mut p = Partial::default();
        for d in scenario["queued"].as_array().unwrap() {
            p.event(d);
        }
        p.seed(&scenario["checkpoint"]);
        assert_eq!(p.text().0, "Hello world");
        p.event(&scenario["final"]);
        p.event(&scenario["final"]);
        assert_eq!(p.text(), ("Hello world!".into(), "Thinking".into()));
        p.seed(&scenario["checkpoint"]);
        assert_eq!(p.text().0, "Hello world!");
    }
    #[test]
    fn new_request_clears_old_streaming() {
        let mut p = Partial::default();
        p.event(
            &json!({"request_id":"a", "seq":1,"part_index":0,"type":"text_delta","text":"old"}),
        );
        p.event(
            &json!({"request_id":"b", "seq":1,"part_index":0,"type":"text_delta","text":"new"}),
        );
        assert_eq!(p.text().0, "new");
    }
    #[test]
    fn a_reconnect_gap_waits_for_a_checkpoint() {
        let mut p = Partial::default();
        p.event(&json!({"request_id":"r", "seq":1,"part_index":0,"type":"text_delta","text":"a"}));
        p.event(&json!({"request_id":"r", "seq":3,"part_index":0,"type":"text_delta","text":"c"}));
        p.seed(&json!({"request_id":"r", "seq":2,"parts":[{"type":"text","text":"ab"}]}));
        assert_eq!(p.text().0, "abc");
    }
    #[test]
    fn ephemeral_events_keep_the_cursor_and_saved_messages_clear_scratch() {
        use crate::{
            api::{Event, Frame},
            reduce::Model,
        };
        let raw = json!({"id":null,"type":"text_delta","session_id":"s","data":{
            "type":"text_delta","request_id":"r","seq":1,"part_index":0,"text":"hello"
        }});
        let Frame::Event(event) = Frame::from_value("text_delta", Some("log_t:10"), &raw).unwrap()
        else {
            panic!()
        };
        assert_eq!(event.id, 0);
        assert!(event.cursor.is_none());
        let mut m = Model {
            generation: Some("log_t".into()),
            last_id: 10,
            ..Model::default()
        };
        m.apply_event(&event);
        m.apply_event(&event);
        assert_eq!(m.streaming["s"].text, "hello");
        assert_eq!(m.cursor().as_deref(), Some("log_t:10"));
        m.apply_event(&Event { id:11, kind:"message.added".into(), data:json!({"id":"msg","session_id":"s","role":"assistant","parts":[],"seq":1,"created":1}), ..event });
        assert!(!m.streaming.contains_key("s"));
    }
}
