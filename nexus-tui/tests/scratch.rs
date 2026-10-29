//! Temporary visual scratch test.
mod common;
use common::*;

#[test]
fn scratch_render() {
    let mut a = with_turn(
        opts(
            nexus_tui::theme::Mode::Dark,
            nexus_tui::theme::Tier::TrueColor,
        ),
        (120, 40),
    );
    // Default detail (medium).
    let calls: Vec<_> = (0..7)
        .map(|n| {
            serde_json::json!({"type":"tool_call", "id":format!("c{n}"), "name":"bash",
                "arguments": format!("{{\"command\":\"echo {n}\"}}")})
        })
        .collect();
    a.state.model.selected.as_mut().unwrap().messages = serde_json::from_value(serde_json::json!([
        {"id":"u", "session_id":SESSION, "seq":1, "created":1.0, "role":"user", "parts":[{"type":"text","text":"start"}]},
        {"id":"a", "session_id":SESSION, "seq":2, "created":2.0, "role":"assistant", "parts":calls}
    ])).unwrap();
    a.state.model.selected.as_mut().unwrap().events.clear();
    busy(&mut a);
    for (id, text) in [
        (50, "Let me think about this. "),
        (
            51,
            "More thinking here that is long enough to wrap around the line maybe, yes.",
        ),
    ] {
        let v = serde_json::json!({"id": id, "type": "reasoning_delta", "session_id": SESSION,
            "cursor": "log_c915bfc9b6d826c4:20",
            "data": {"type": "reasoning_delta", "text": text}});
        if let Ok(nexus_tui::api::Frame::Event(e)) =
            nexus_tui::api::Frame::from_value("reasoning_delta", None, &v)
        {
            a.apply_sync(nexus_tui::sync::SyncMsg::Event(e));
        }
    }
    let (_, rows) = draw(&mut a, 120, 40);
    for (i, r) in rows.iter().enumerate() {
        println!("{i:02} |{r}|");
    }
    panic!("show rows");
}
