//! Target-independent host commands and labelled conversation presentation.
use serde_json::{json, Value};
pub fn command(kind: &str, session: &str, content: &str) -> Value {
    match kind {
        "SessionList" => json!({"type":kind}),
        "SessionCancel" => json!({"type":kind,"session":session,"drop_queue":false}),
        "SessionOpen" => json!({"type":kind,"session":session,"create":true,"recover":true}),
        "SessionEnqueue" => json!({"type":kind,"session":session,"content":content}),
        _ => json!({"type":kind,"session":session}),
    }
}
pub fn readable(value: &Value) -> String {
    fn rows(v: &Value, label: &str, out: &mut String) {
        match v {
            Value::Object(m) if m.is_empty() => {
                out.push_str(&format!(
                    "{}: (empty object)\n",
                    label.trim_end_matches(" / ")
                ));
            }
            Value::Array(a) if a.is_empty() => {
                out.push_str(&format!(
                    "{}: (empty list)\n",
                    label.trim_end_matches(" / ")
                ));
            }
            Value::Object(m) => {
                for (k, v) in m {
                    rows(v, &format!("{label}{k} / "), out);
                }
            }
            Value::Array(a) => {
                for (i, v) in a.iter().enumerate() {
                    rows(v, &format!("{label}{} / ", i + 1), out);
                }
            }
            Value::Null => {
                out.push_str(&format!(
                    "{}: not provided\n",
                    label.trim_end_matches(" / ")
                ));
            }
            Value::String(s) if s.is_empty() => {
                out.push_str(&format!("{}: (empty)\n", label.trim_end_matches(" / ")));
            }
            _ => {
                out.push_str(label.trim_end_matches(" / "));
                out.push_str(": ");
                out.push_str(
                    &v.as_str()
                        .map(str::to_owned)
                        .unwrap_or_else(|| v.to_string()),
                );
                out.push('\n');
            }
        }
    }
    let mut out = String::new();
    rows(value, "", &mut out);
    out
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn labels_all_tool_fields() {
        let s = readable(
            &json!({"tools":[{"name":"shell","arguments":{"command":"pwd"},"result":"/tmp"}]}),
        );
        assert!(s.contains("command: pwd"));
        assert!(s.contains("result: /tmp"));
    }
    #[test]
    fn preserves_empty_and_optional_tool_fields() {
        let s = readable(&json!({"tool":{"input":{},"result":[],"error":null,"display":""}}));
        for field in [
            "input: (empty object)",
            "result: (empty list)",
            "error: not provided",
            "display: (empty)",
        ] {
            assert!(s.contains(field), "{s}");
        }
        assert!(!s.contains("{\""));
    }
    #[test]
    fn stop_keeps_queue_and_open_creates() {
        assert_eq!(command("SessionCancel", "id", "")["drop_queue"], false);
        assert_eq!(command("SessionOpen", "id", "")["create"], true);
    }
    #[test]
    fn enqueue() {
        assert_eq!(command("SessionEnqueue", "a", "hello")["content"], "hello");
    }
}

/// Project reducer turns as labelled conversation rows, retaining structured tool details.
pub fn project_conversation(view: &Value) -> String {
    let mut out = String::new();
    if let Some(turns) = view["turns"].as_array() {
        for turn in turns {
            if let Some(text) = turn["user_text"].as_str().filter(|s| !s.is_empty()) {
                out.push_str(&format!("You\n{text}\n\n"));
            }
            if let Some(messages) = turn["messages"].as_array() {
                for message in messages {
                    let role = message["role"].as_str().unwrap_or("assistant");
                    out.push_str(&format!("{role}\n"));
                    if let Some(blocks) = message["blocks"].as_array() {
                        for block in blocks {
                            let kind = block["kind"].as_str().unwrap_or("text");
                            if kind != "text" {
                                out.push_str(&format!("[{kind}] "));
                            }
                            if let Some(text) = block["text"].as_str() {
                                out.push_str(text);
                            }
                            out.push('\n');
                        }
                    }
                    out.push('\n');
                }
            }
            if let Some(tools) = turn["tools"].as_array() {
                for tool in tools {
                    out.push_str("Tool call\n");
                    out.push_str(&readable(tool));
                    out.push('\n');
                }
            }
        }
    }
    if out.is_empty() {
        out.push_str("No conversation yet.");
    }
    out
}

#[cfg(unix)]
pub mod native;

#[cfg(test)]
mod projection_tests {
    use super::*;
    #[test]
    fn renders_messages_and_tool_parameters() {
        let view = json!({"turns":[{"user_text":"hello","messages":[{"role":"assistant","blocks":[{"kind":"text","text":"hi"}]}],"tools":[{"name":"shell","input":{"command":"pwd"},"result":[{"text":"/tmp"}]}]}]});
        let text = project_conversation(&view);
        for part in [
            "You",
            "hello",
            "assistant",
            "hi",
            "Tool call",
            "command: pwd",
            "/tmp",
        ] {
            assert!(text.contains(part), "{text}");
        }
    }
    #[test]
    fn empty_conversation_is_labelled() {
        assert_eq!(project_conversation(&json!({})), "No conversation yet.");
    }
}
