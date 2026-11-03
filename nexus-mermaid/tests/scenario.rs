//! The shared Mermaid scenario's compiler half. The client tests in
//! `nexus-tui/tests/scenarios.rs` and `nexus-nsh/scenarios.test.tsx` draw it;
//! this test keeps the scenario data and the compiler in agreement.
#![cfg(feature = "svg")]

use nexus_mermaid::svg::{self, Error, Options};

const SCENARIO: &str = include_str!("../../interface/scenarios/mermaid.json");

fn fences(text: &str) -> Vec<(String, String)> {
    let mut out = Vec::new();
    let mut language: Option<String> = None;
    let mut source = String::new();
    for line in text.lines() {
        if let Some(open) = language.take() {
            if line.trim_start().starts_with("```") {
                out.push((open, std::mem::take(&mut source)));
            } else {
                language = Some(open);
                source.push_str(line);
                source.push('\n');
            }
        } else if let Some(rest) = line.trim_start().strip_prefix("```") {
            language = Some(rest.trim().to_owned());
        }
    }
    out
}

#[test]
fn scenario_fences_compile_and_mindmap_falls_back() {
    let scenario: serde_json::Value = serde_json::from_str(SCENARIO).unwrap();
    let text = scenario["messages"][1]["parts"][0]["text"]
        .as_str()
        .unwrap();
    let fences = fences(text);
    assert_eq!(fences.len(), 3, "{fences:?}");
    assert!(fences.iter().all(|(language, _)| language == "mermaid"));

    for (_, source) in &fences[..2] {
        let diagram = svg::render(source, &Options::default()).unwrap();
        assert!(!diagram.svg.is_empty());
        assert!(!diagram.svg.contains("href="));
        assert!(!diagram.svg.contains("<image"));
    }
    let mindmap = svg::render(&fences[2].1, &Options::default()).unwrap_err();
    assert!(matches!(mindmap, Error::Unsupported { .. }), "{mindmap:?}");
}
