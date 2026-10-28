//! Totals for the viewed session, derived from the messages and events the
//! client has loaded (the right sidebar's Session info). Nothing here is
//! stored or sent: a bounded history gives bounded totals, and the caller says
//! so when messages or diagnostics are incomplete.

use std::collections::BTreeMap;

use serde_json::Value;

use crate::api::{Event, Message};

/// Catalog prices in USD per million tokens.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Price {
    pub input: f64,
    pub output: f64,
    pub cache_read: f64,
}

/// The catalog entry for a model used (`provider/model` or a bare id).
pub fn catalog_entry<'a>(models: &'a [Value], used: &str) -> Option<&'a Value> {
    let id = used.rsplit('/').next().unwrap_or(used);
    models.iter().find(|m| {
        m["ref"]
            .as_str()
            .is_some_and(|r| r == used || r.rsplit('/').next() == Some(id))
    })
}

/// Prices from a catalog entry; `None` when the model has no input and output price.
pub fn price_of(entry: &Value) -> Option<Price> {
    let input = entry["input_cost"].as_f64()?;
    Some(Price {
        input,
        output: entry["output_cost"].as_f64()?,
        cache_read: entry["cache_read_cost"].as_f64().unwrap_or(input),
    })
}

/// Cost of one request: uncached input, cached input and output (reasoning
/// tokens are billed as output and already counted there).
pub fn cost(price: Price, input: u64, cached: u64, output: u64) -> f64 {
    let fresh = input.saturating_sub(cached) as f64;
    (fresh * price.input + cached as f64 * price.cache_read + output as f64 * price.output) / 1e6
}

/// `$0.16`, or `<$0.01`.
pub fn money(usd: f64) -> String {
    if usd < 0.01 {
        "<$0.01".to_owned()
    } else {
        format!("${usd:.2}")
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct SessionStats {
    pub turns: usize,
    /// Model calls: one per reported usage.
    pub calls: u64,
    /// Tool calls by name, most used first.
    pub tools: Vec<(String, usize)>,
    pub input: u64,
    pub cached: u64,
    pub output: u64,
    pub reasoning: u64,
    /// Input tokens of the latest request: what the context holds now.
    pub last_input: Option<u64>,
    /// Sum of priced requests; `None` when no request's model has a price.
    pub cost: Option<f64>,
    /// Some requests used a model without a price, so the cost is a floor.
    pub partial_cost: bool,
}

impl SessionStats {
    pub fn has_tokens(&self) -> bool {
        self.input + self.output + self.cached + self.reasoning > 0
    }
}

pub fn compute(
    messages: &[Message],
    events: &[Event],
    models: &[Value],
    fallback: &str,
) -> SessionStats {
    let mut s = SessionStats::default();
    let mut counts: BTreeMap<String, usize> = BTreeMap::new();
    let mut seen = std::collections::HashSet::new();
    for m in messages {
        if m.role == "user" {
            let text = m
                .parts
                .iter()
                .filter_map(|p| p["text"].as_str())
                .collect::<String>();
            if !text.starts_with('[') {
                s.turns += 1;
            }
        }
        for p in &m.parts {
            if p["type"] == "tool_call" {
                if let (Some(id), Some(name)) = (p["id"].as_str(), p["name"].as_str()) {
                    if seen.insert(id.to_owned()) {
                        *counts.entry(name.to_owned()).or_default() += 1;
                    }
                }
            }
        }
    }
    let mut tools: Vec<(String, usize)> = counts.into_iter().collect();
    tools.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
    s.tools = tools;
    let mut model = fallback.to_owned();
    let mut total = 0.0;
    let mut priced = false;
    for e in events {
        match e.kind.as_str() {
            "wire_request" => {
                if let Some(m) = e.data["body"]["model"].as_str() {
                    model = m.to_owned();
                }
            }
            "usage" => {
                let input = e.data["input_tokens"].as_u64().unwrap_or(0);
                let cached = e.data["cached_input_tokens"].as_u64().unwrap_or(0);
                let output = e.data["output_tokens"].as_u64().unwrap_or(0);
                s.calls += 1;
                s.input += input;
                s.cached += cached;
                s.output += output;
                s.reasoning += e.data["reasoning_tokens"].as_u64().unwrap_or(0);
                s.last_input = Some(input);
                match catalog_entry(models, &model).and_then(price_of) {
                    Some(p) => {
                        total += cost(p, input, cached, output);
                        priced = true;
                    }
                    None => s.partial_cost = true,
                }
            }
            _ => {}
        }
    }
    s.cost = priced.then_some(total);
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn prices_and_money() {
        let p = Price {
            input: 1.0,
            output: 10.0,
            cache_read: 0.2,
        };
        assert!((cost(p, 100_000, 50_000, 10_000) - 0.16).abs() < 1e-9);
        assert_eq!(money(0.16), "$0.16");
        assert_eq!(money(0.004), "<$0.01");
    }

    #[test]
    fn entries_match_refs_and_bare_ids() {
        let models =
            vec![serde_json::json!({"ref": "codex/gpt-5", "input_cost": 1.0, "output_cost": 2.0})];
        assert!(catalog_entry(&models, "gpt-5").is_some());
        assert!(catalog_entry(&models, "codex/gpt-5").is_some());
        assert!(catalog_entry(&models, "other").is_none());
        assert_eq!(price_of(&models[0]).unwrap().cache_read, 1.0);
    }
}
