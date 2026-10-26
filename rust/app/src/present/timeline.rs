//! Pure view JSON -> timeline rows for the NSH client (no NSH types).
//!
//! The wire view (`session-view`, kept current by patch frames) is read as `serde_json::Value`
//! and flattened into labelled rows: nothing is shown as raw JSON except
//! non-string tool inputs, which are compact JSON by contract. Every value is
//! clipped at [`CLIP`] chars and the clipping is announced.

use serde_json::Value;

/// Longest single value shown, in chars.
pub const CLIP: usize = 2000;

/// One `label: value` (or bare text when `label` is empty) line of a row.
#[derive(Clone, Debug, PartialEq)]
pub struct Line {
    pub label: String,
    pub text: String,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum RowKind {
    User,
    Assistant,
    Tool,
    Error,
}

/// One timeline row.
#[derive(Clone, Debug, PartialEq)]
pub struct Row {
    pub key: String,
    pub kind: RowKind,
    pub heading: String,
    pub lines: Vec<Line>,
}

/// Clips `s` to [`CLIP`] chars, announcing the clipping.
pub fn clip(s: &str) -> String {
    let total = s.chars().count();
    if total <= CLIP {
        return s.to_owned();
    }
    let head: String = s.chars().take(CLIP).collect();
    format!("{head}… clipped ({total} chars total)")
}

fn str_of<'a>(v: &'a Value, key: &str) -> &'a str {
    v.get(key).and_then(Value::as_str).unwrap_or("")
}

fn message_row(m: &Value, fallback: usize) -> Option<(u64, Row)> {
    let role = str_of(m, "role");
    let blocks = m.get("blocks").and_then(Value::as_array)?;
    let parts: Vec<String> = blocks
        .iter()
        .map(|b| match b.get("kind").and_then(Value::as_str) {
            Some("text") => str_of(b, "text").to_owned(),
            Some(kind) => format!("[{kind}]"),
            None => "[unknown]".to_owned(),
        })
        .collect();
    let user = role == "user";
    if !user && parts.is_empty() {
        return None;
    }
    let seq = m.get("event_seq").and_then(Value::as_u64).unwrap_or(0);
    let id = m
        .get("id")
        .map(|i| i.as_str().map_or_else(|| i.to_string(), str::to_owned))
        .unwrap_or_else(|| fallback.to_string());
    Some((
        seq,
        Row {
            key: format!("m-{id}"),
            kind: if user { RowKind::User } else { RowKind::Assistant },
            heading: if user { "You" } else { "Assistant" }.to_owned(),
            lines: vec![Line { label: String::new(), text: clip(&parts.join("\n")) }],
        },
    ))
}

fn value_text(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

fn tool_row(t: &Value, fallback: usize) -> (u64, Row) {
    let seq = t.get("event_seq").and_then(Value::as_u64).unwrap_or(0);
    let name = str_of(t, "name");
    let status = str_of(t, "status");
    let mut lines = Vec::new();
    if let Some(input) = t.get("input").and_then(Value::as_object) {
        for (k, v) in input {
            lines.push(Line { label: k.clone(), text: clip(&value_text(v)) });
        }
    }
    let result: Vec<&str> = t
        .get("result")
        .and_then(Value::as_array)
        .map(|a| a.iter().filter(|r| r.get("type").and_then(Value::as_str) == Some("text")).map(|r| str_of(r, "text")).collect())
        .unwrap_or_default();
    if !result.is_empty() {
        lines.push(Line { label: "Result".into(), text: clip(&result.join("\n")) });
    }
    match t.get("error") {
        None | Some(Value::Null) => {}
        Some(e) => {
            let text = value_text(e);
            if !text.is_empty() {
                lines.push(Line { label: "Error".into(), text: clip(&text) });
            }
        }
    }
    let call = t.get("call_id").and_then(Value::as_str).map_or_else(|| fallback.to_string(), str::to_owned);
    (
        seq,
        Row {
            key: format!("t-{call}"),
            kind: RowKind::Tool,
            heading: format!("Tool · {name} · {status}"),
            lines,
        },
    )
}

/// Session phase from a view (`idle`, `running`, ...); empty when unknown.
pub fn phase_of(view: &Value) -> String {
    str_of(view, "phase").to_owned()
}

/// Selected model name from a view's context; empty when unknown.
pub fn model_of(view: &Value) -> String {
    view.get("context").map(|c| str_of(c, "model")).unwrap_or("").to_owned()
}

/// Wire `view` -> timeline rows. Rows are ordered by `event_seq` within each
/// turn; a turn error follows its turn's rows.
pub fn rows_from_view(view: &Value) -> Vec<Row> {
    let mut rows: Vec<Row> = Vec::new();
    let turns = view.get("turns").and_then(Value::as_array).filter(|t| !t.is_empty());
    let mut n = 0usize;
    if let Some(turns) = turns {
        for turn in turns {
            let mut items = Vec::new();
            for m in turn.get("messages").and_then(Value::as_array).into_iter().flatten() {
                n += 1;
                items.extend(message_row(m, n));
            }
            for t in turn.get("tools").and_then(Value::as_array).into_iter().flatten() {
                n += 1;
                items.push(tool_row(t, n));
            }
            items.sort_by_key(|(seq, _)| *seq);
            rows.extend(items.into_iter().map(|(_, r)| r));
            let err = match turn.get("error") {
                None | Some(Value::Null) => String::new(),
                Some(e) => value_text(e),
            };
            if !err.is_empty() {
                let index = turn.get("index").map(Value::to_string).unwrap_or_default();
                rows.push(Row {
                    key: format!("e-{index}"),
                    kind: RowKind::Error,
                    heading: "Turn error".into(),
                    lines: vec![Line { label: "Error".into(), text: clip(&err) }],
                });
            }
        }
    } else {
        let mut items = Vec::new();
        for m in view.get("messages").and_then(Value::as_array).into_iter().flatten() {
            n += 1;
            items.extend(message_row(m, n));
        }
        items.sort_by_key(|(seq, _)| *seq);
        rows.extend(items.into_iter().map(|(_, r)| r));
    }
    // Keys must be unique for `each`.
    let mut seen = std::collections::HashSet::new();
    for (i, row) in rows.iter_mut().enumerate() {
        if !seen.insert(row.key.clone()) {
            row.key = format!("{}#{i}", row.key);
            seen.insert(row.key.clone());
        }
    }
    rows
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn clips_and_announces() {
        assert_eq!(clip("abc"), "abc");
        let long = "x".repeat(2500);
        let c = clip(&long);
        assert!(c.starts_with(&"x".repeat(2000)));
        assert!(c.ends_with("… clipped (2500 chars total)"));
    }

    #[test]
    fn snapshot_orders_rows_and_labels_tools() {
        let v = json!({"seq":7,"view":{"phase":"idle","context":{"model":"m"},"turns":[
          {"index":0,"phase":"done","error":"boom","messages":[
            {"id":"u1","event_seq":1,"role":"user","blocks":[{"kind":"text","text":"hi"}]},
            {"id":"a0","event_seq":5,"role":"assistant","blocks":[]},
            {"id":"a1","event_seq":6,"role":"assistant","blocks":[{"kind":"image"},{"kind":"text","text":"ok"}]}],
           "tools":[{"call_id":"c1","event_seq":3,"name":"bash","status":"done","input":{"cmd":"ls","n":2},
             "result":[{"type":"text","text":"out"}],"error":null}]}]}});
        let view = &v["view"];
        assert_eq!((phase_of(view).as_str(), model_of(view).as_str()), ("idle", "m"));
        let rows = rows_from_view(view);
        let heads: Vec<_> = rows.iter().map(|r| r.heading.as_str()).collect();
        assert_eq!(heads, ["You", "Tool · bash · done", "Assistant", "Turn error"]);
        let tool = &rows[1];
        assert_eq!(tool.lines[0], Line { label: "cmd".into(), text: "ls".into() });
        assert_eq!(tool.lines[1], Line { label: "n".into(), text: "2".into() });
        assert_eq!(tool.lines[2].label, "Result");
        assert_eq!(rows[2].lines[0].text, "[image]\nok");
    }

    #[test]
    fn falls_back_to_view_messages() {
        let v = json!({"seq":1,"view":{"messages":[
            {"id":"u","event_seq":1,"role":"user","blocks":[{"kind":"text","text":"yo"}]}]}});
        assert_eq!(rows_from_view(&v["view"]).len(), 1);
    }
}
