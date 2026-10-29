//! Totals for the viewed session (the right sidebar's Session info): turn and
//! tool counts from the messages, tokens and cost from Core's spend ledger
//! (`usage` on `GET /api/session/{id}/turn`), which survives turn pruning, plus
//! the running turn's live `usage` events until its summary arrives.

use std::collections::BTreeMap;

use serde_json::Value;

use crate::reduce::SelectedView;

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
    /// When the ledger began counting (`None`: no ledger rows yet).
    pub since: Option<f64>,
}

impl SessionStats {
    pub fn has_tokens(&self) -> bool {
        self.input + self.output + self.cached + self.reasoning > 0
    }
}

pub fn compute(view: &SelectedView, models: &[Value], fallback: &str) -> SessionStats {
    let mut s = SessionStats::default();
    let mut counts: BTreeMap<String, usize> = BTreeMap::new();
    let mut seen = std::collections::HashSet::new();
    for m in &view.messages {
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
    let mut total = 0.0;
    let mut priced = false;
    let mut add = |s: &mut SessionStats, model: &str, calls: u64, u: &crate::api::Usage| {
        s.calls += calls;
        s.input += u.input;
        s.cached += u.cached;
        s.output += u.output;
        s.reasoning += u.reasoning;
        match catalog_entry(models, model).and_then(price_of) {
            Some(p) => {
                total += cost(p, u.input, u.cached, u.output);
                priced = true;
            }
            None => s.partial_cost = true,
        }
    };
    for (model, row) in &view.usage.models {
        if row.requests > 0 || row.usage != crate::api::Usage::default() {
            add(&mut s, model, row.requests, &row.usage);
        }
    }
    s.since = view.usage.since;
    // The running turn: live usage, counted by the ledger once its summary lands.
    let mut model = fallback.to_owned();
    for e in &view.scratch {
        match e.kind.as_str() {
            "wire_request" => {
                if let Some(m) = e.data["body"]["model"].as_str() {
                    model = m.to_owned();
                }
            }
            "usage" => {
                let u = crate::api::Usage {
                    input: e.data["input_tokens"].as_u64().unwrap_or(0),
                    cached: e.data["cached_input_tokens"].as_u64().unwrap_or(0),
                    output: e.data["output_tokens"].as_u64().unwrap_or(0),
                    reasoning: e.data["reasoning_tokens"].as_u64().unwrap_or(0),
                };
                add(&mut s, &model, 1, &u);
            }
            _ => {}
        }
    }
    s.last_input = view.latest_input();
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
