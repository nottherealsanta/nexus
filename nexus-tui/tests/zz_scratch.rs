mod common;
use common::*;

use nexus_tui::theme::{Mode, Tier};

fn calls(a: &mut nexus_tui::app::App, tools: &[(&str, Option<&str>)]) {
    let mut parts: Vec<serde_json::Value> = vec![serde_json::json!({"type":"text","text":"I'll look at where this is rendered."})];
    parts.extend(tools.iter().enumerate().map(|(n, (name, _))| serde_json::json!({"type":"tool_call", "id":format!("c{n}"), "name":name, "arguments":"{\"command\":\"ls -la\"}"})));
    let results: Vec<_> = tools
        .iter()
        .enumerate()
        .filter_map(|(n, (_, out))| {
            out.map(|o| serde_json::json!({"type":"tool_result", "call_id":format!("c{n}"), "output":o, "is_error":false}))
        })
        .collect();
    let view = a.state.model.selected.as_mut().unwrap();
    view.messages = serde_json::from_value(serde_json::json!([
        {"id":"u", "session_id":SESSION, "seq":1, "created":1.0, "role":"user", "parts":[{"type":"text","text":"go"}]},
        {"id":"a", "session_id":SESSION, "seq":2, "created":2.0, "role":"assistant", "parts":parts},
        {"id":"t", "session_id":SESSION, "seq":3, "created":3.0, "role":"tool", "parts":results}
    ]))
    .unwrap();
    view.events.clear();
}

#[test]
fn print_layout() {
    let mut a = with_turn(opts(Mode::Dark, Tier::TrueColor), (120, 36));
    a.ui.detail = nexus_tui::ui::Detail::High;
    calls(&mut a, &[("bash", Some("ok")), ("read", Some("ok")), ("bash", Some("ok"))]);
    let (_, rows) = draw(&mut a, 120, 36);
    for (i, r) in rows.iter().enumerate() {
        let trimmed = r.trim_end();
        if !trimmed.is_empty() {
            println!("{i:02}|{trimmed}");
        }
    }
    let ruler: String = (0..120).map(|i| char::from(b'0' + (i % 10) as u8)).collect();
    println!("--|{ruler}");
}
