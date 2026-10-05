//! Rust-local disclosure with bounded session/page LRU choices.
use crate::bridge::Content;
use serde_json::Value;
use std::{
    borrow::Cow,
    cell::Cell,
    collections::{HashMap, HashSet},
};

/// Turns kept open by default; every older turn starts folded.
pub const RECENT_TURNS: usize = 2;

#[derive(Default)]
pub struct Disclosure {
    pub scope: String,
    pub verbose: bool,
    clock: Cell<u64>,
    scopes: HashMap<String, HashMap<String, (bool, Cell<u64>)>>,
    pub changed_from: Option<usize>,
    pub changed_until: Option<usize>,
    /// The newest turns of the page being shown; they start open.
    recent: Vec<String>,
    /// Turns that own a fold handle (their user row). Only these may start folded:
    /// folding a turn without one would hide it with no way to open it again.
    handled: HashSet<String>,
}
impl Disclosure {
    fn default_folded(&self, turn: &str) -> bool {
        self.handled.contains(turn) && !self.recent.iter().any(|recent| recent == turn)
    }
    /// Recompute which turns start open from the blocks about to be laid out.
    /// Returns the first block of a turn that has just left the window, so the
    /// layout rebuilds from there (no patch covers a turn that did not change).
    pub fn sync_window(&mut self, blocks: &[Content]) -> Option<usize> {
        let mut order: Vec<&str> = Vec::new();
        let mut handled = HashSet::new();
        for block in blocks {
            if block.turn_id.is_empty() {
                continue;
            }
            if order.last() != Some(&block.turn_id.as_str()) {
                order.push(&block.turn_id);
            }
            if block.kind == "user" && block.local_ui {
                handled.insert(block.turn_id.clone());
            }
        }
        let recent: Vec<String> = order
            .iter()
            .rev()
            .take(RECENT_TURNS)
            .rev()
            .map(|turn| (*turn).to_owned())
            .collect();
        let dropped: Vec<&String> = self
            .recent
            .iter()
            .filter(|turn| !recent.contains(turn) && handled.contains(*turn))
            .collect();
        let first = if dropped.is_empty() {
            None
        } else {
            blocks
                .iter()
                .position(|block| dropped.iter().any(|turn| **turn == block.turn_id))
        };
        self.recent = recent;
        self.handled = handled;
        first
    }
    fn opened(&self, id: &str, default: bool) -> bool {
        self.scopes
            .get(&self.scope)
            .and_then(|scope| scope.get(id))
            .map(|entry| {
                self.clock.set(self.clock.get() + 1);
                entry.1.set(self.clock.get());
                entry.0
            })
            .unwrap_or(default)
    }
    pub fn toggle(&mut self, operation: &Value, blocks: &[Content]) -> bool {
        let kind = operation["kind"].as_str().unwrap_or("");
        if !matches!(kind, "block_toggle" | "turn_toggle") {
            return false;
        }
        let Some(id) = operation["id"].as_str() else {
            return false;
        };
        fn contains(block: &Content, op: &Value) -> bool {
            (block.local_ui
                && (block.operation.as_ref() == Some(op)
                    || block.output_operation.as_ref() == Some(op)))
                || block.members.iter().any(|member| contains(member, op))
        }
        let Some(index) = blocks.iter().position(|block| contains(block, operation)) else {
            return false;
        };
        if self.verbose && kind == "block_toggle" {
            return true;
        }
        let default = kind == "turn_toggle" && self.default_folded(id);
        let next = !self.opened(id, default);
        self.clock.set(self.clock.get() + 1);
        // At most 32 scopes, least recently changed scope evicted first.
        if !self.scopes.contains_key(&self.scope) && self.scopes.len() >= 32 {
            if let Some(oldest) = self
                .scopes
                .iter()
                .min_by_key(|(_, entries)| entries.values().map(|v| v.1.get()).max().unwrap_or(0))
                .map(|(key, _)| key.clone())
            {
                self.scopes.remove(&oldest);
            }
        }
        let entries = self.scopes.entry(self.scope.clone()).or_default();
        if entries.len() >= 4096 && !entries.contains_key(id) {
            if let Some(oldest) = entries
                .iter()
                .min_by_key(|(_, entry)| entry.1.get())
                .map(|(key, _)| key.clone())
            {
                entries.remove(&oldest);
            }
        }
        entries.insert(id.to_owned(), (next, Cell::new(self.clock.get())));
        self.changed_from = Some(index);
        self.changed_until = Some(if kind == "turn_toggle" {
            index
                + blocks[index..]
                    .iter()
                    .take_while(|block| block.turn_id == id)
                    .count()
        } else {
            index + 1
        });
        true
    }
    pub fn block<'a>(&self, source: &'a Content) -> Option<Cow<'a, Content>> {
        let folded = !self.verbose
            && !source.turn_id.is_empty()
            && self.opened(&source.turn_id, self.default_folded(&source.turn_id));
        if folded && source.kind != "user" {
            return None;
        }
        if !source.local_ui && !folded {
            return Some(Cow::Borrowed(source));
        }
        // Copy only visible metadata; never deep-clone hidden members or output.
        let mut shown = Content {
            id: source.id.clone(),
            title: source.title.clone(),
            text: source.text.clone(),
            kind: source.kind.clone(),
            operation: source.operation.clone(),
            gap: source.gap,
            number: source.number,
            collapsed: source.collapsed,
            chips: source.chips.clone(),
            chip_operation: source.chip_operation.clone(),
            heading: source.heading.clone(),
            status: source.status.clone(),
            batch_glyph: source.batch_glyph.clone(),
            color: source.color.clone(),
            rev: source.rev.clone(),
            count: source.count,
            failures: source.failures,
            counts: source.counts.clone(),
            output_operation: source.output_operation.clone(),
            ..Default::default()
        };
        let id = source
            .operation
            .as_ref()
            .and_then(|op| op["id"].as_str())
            .unwrap_or(&source.id);
        let open = self.verbose || self.opened(id, source.local_open);
        let preview = source.kind == "tool_group"
            && source.preview_limit > 0
            && !open
            && self
                .scopes
                .get(&self.scope)
                .is_none_or(|scope| !scope.contains_key(id));
        match source.kind.as_str() {
            "tool_group" => {
                shown.collapsed = !(open || preview);
            }
            "tool" | "thought" => {
                let full = self.verbose
                    || source
                        .output_operation
                        .as_ref()
                        .and_then(|op| op["id"].as_str())
                        .is_none_or(|id| self.opened(id, false));
                if open {
                    if full || source.fold_lines == 0 {
                        shown.detail = source.local_detail.clone();
                    } else {
                        let rows: Vec<_> = source.local_detail.lines().collect();
                        let end = source.fold_lines.min(rows.len());
                        shown.detail = rows[..end].join("\n");
                        if end < rows.len() {
                            shown.detail.push_str(&format!(
                                "\n… {} more lines · Enter for all",
                                rows.len() - end
                            ));
                        }
                    }
                    if source.kind == "thought" {
                        shown.text.clear();
                    }
                } else {
                    shown.output_operation = None;
                }
                shown.rev.push_str(if full { ":full" } else { ":preview" });
            }
            _ => {}
        }
        if (open || preview) && source.kind != "user" {
            let start = if preview {
                source.members.len().saturating_sub(source.preview_limit)
            } else {
                0
            };
            shown.members = source
                .members
                .iter()
                .skip(start)
                .filter_map(|member| self.block(member).map(Cow::into_owned))
                .collect();
            if start > 0 {
                shown.members.insert(
                    0,
                    Content {
                        kind: "activity_more".into(),
                        heading: format!("… {start} earlier items · Enter for all"),
                        operation: source.operation.clone(),
                        ..Default::default()
                    },
                );
            }
        }
        for member in &shown.members {
            shown.rev.push_str(&member.rev);
        }
        shown.rev.push_str(if open { ":open" } else { ":closed" });
        if preview {
            shown.rev.push_str(":live-preview");
        }
        if folded {
            shown.collapsed = true;
            if !source.text.is_empty() {
                shown.title.push_str(" …"); // the rest of the prompt is hidden
            }
            shown.text = source.fold_summary.clone();
            shown.chips.clear();
            shown.chip_operation = None;
            shown.rev.push_str(":folded");
        }
        if source.rev.is_empty() {
            shown.rev.clear();
        }
        Some(Cow::Owned(shown))
    }
    #[cfg(test)]
    pub fn project(&self, blocks: &[Content]) -> Vec<Content> {
        blocks
            .iter()
            .filter_map(|block| self.block(block).map(Cow::into_owned))
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn borrowed_plain_content_and_lru_eviction() {
        let mut ui = Disclosure::default();
        let plain = Content {
            kind: "markdown".into(),
            text: "unchanged".into(),
            ..Default::default()
        };
        assert!(matches!(ui.block(&plain), Some(Cow::Borrowed(_))));
        let mut entries = HashMap::new();
        for n in 0..4096 {
            entries.insert(n.to_string(), (true, Cell::new(n)));
        }
        ui.scopes.insert(String::new(), entries);
        ui.clock.set(4096);
        assert!(ui.opened("0", false)); // touching the oldest keeps it alive
        let op = serde_json::json!({"kind":"block_toggle","id":"new"});
        let group = Content {
            local_ui: true,
            operation: Some(op.clone()),
            ..Default::default()
        };
        assert!(ui.toggle(&op, &[group]));
        assert!(ui.scopes[""].contains_key("0"));
        assert!(!ui.scopes[""].contains_key("1"));
        assert_eq!(ui.scopes[""].len(), 4096);
    }
    #[test]
    fn expansion_survives_updates_and_is_scoped() {
        let op = json!({"kind":"block_toggle","id":"g"});
        let mut group = Content {
            id: "g".into(),
            kind: "tool_group".into(),
            local_ui: true,
            operation: Some(op.clone()),
            members: vec![Content {
                id: "c".into(),
                ..Default::default()
            }],
            ..Default::default()
        };
        let mut ui = Disclosure::default();
        assert!(ui.project(&[group.clone()])[0].members.is_empty());
        assert!(ui.toggle(&op, &[group.clone()]));
        group.members[0].text = "streamed result".into();
        assert_eq!(
            ui.project(&[group.clone()])[0].members[0].text,
            "streamed result"
        );
        ui.scope = "other session".into();
        assert!(ui.project(&[group])[0].members.is_empty());
    }
    #[test]
    fn detail_output_and_turn_have_independent_state() {
        let detail = json!({"kind":"block_toggle","id":"c:detail"});
        let output = json!({"kind":"block_toggle","id":"c:output"});
        let tool = Content {
            local_ui: true,
            kind: "tool".into(),
            operation: Some(detail.clone()),
            output_operation: Some(output.clone()),
            local_detail: "clipped\ncomplete".into(),
            fold_lines: 1,
            ..Default::default()
        };
        let mut ui = Disclosure::default();
        assert!(ui.toggle(&detail, &[tool.clone()]));
        assert!(ui.project(&[tool.clone()])[0]
            .detail
            .starts_with("clipped\n…"));
        assert!(ui.toggle(&output, &[tool.clone()]));
        assert_eq!(ui.project(&[tool.clone()])[0].detail, "clipped\ncomplete");
        ui.verbose = true;
        assert_eq!(ui.project(&[tool.clone()])[0].detail, "clipped\ncomplete");
        ui.verbose = false;
        assert!(ui.toggle(&detail, &[tool.clone()]));
        assert!(ui.project(&[tool])[0].detail.is_empty());
        let fold = json!({"kind":"turn_toggle","id":"t"});
        let blocks = vec![
            Content {
                kind: "user".into(),
                local_ui: true,
                turn_id: "t".into(),
                operation: Some(fold.clone()),
                ..Default::default()
            },
            Content {
                kind: "markdown".into(),
                turn_id: "t".into(),
                ..Default::default()
            },
        ];
        assert!(ui.toggle(&fold, &blocks));
        assert_eq!(ui.project(&blocks).len(), 1);
        assert!(!ui.toggle(&json!({"kind":"submit"}), &blocks));
    }

    #[test]
    fn live_activity_previews_five_then_expands_all_and_honors_collapse() {
        let op = json!({"kind":"block_toggle","id":"activity"});
        let group = Content {
            id: "activity".into(),
            kind: "tool_group".into(),
            local_ui: true,
            preview_limit: 5,
            operation: Some(op.clone()),
            members: (0..8)
                .map(|i| Content {
                    id: format!("tool-{i}"),
                    kind: "tool".into(),
                    local_ui: true,
                    ..Default::default()
                })
                .collect(),
            ..Default::default()
        };
        let mut ui = Disclosure::default();
        let rows = vec![group];
        let preview = ui.project(&rows);
        assert!(!preview[0].collapsed);
        assert_eq!(preview[0].members.len(), 6);
        assert_eq!(preview[0].members[0].kind, "activity_more");
        assert_eq!(preview[0].members[1].id, "tool-3");
        assert!(ui.toggle(&op, &rows));
        assert_eq!(ui.project(&rows)[0].members.len(), 8);
        assert!(ui.toggle(&op, &rows));
        assert!(ui.project(&rows)[0].collapsed);
        assert!(ui.project(&rows)[0].members.is_empty());
        let mut completed = rows.clone();
        completed[0].preview_limit = 0;
        let fresh = Disclosure::default();
        assert!(fresh.project(&completed)[0].collapsed);
    }

    fn turn(id: &str, handle: bool) -> Vec<Content> {
        let mut rows = Vec::new();
        if handle {
            rows.push(Content {
                id: format!("{id}:user"),
                kind: "user".into(),
                local_ui: true,
                turn_id: id.into(),
                operation: Some(json!({"kind":"turn_toggle","id":id})),
                ..Default::default()
            });
        }
        rows.push(Content {
            id: format!("{id}:reply"),
            kind: "markdown".into(),
            turn_id: id.into(),
            ..Default::default()
        });
        rows
    }
    fn page(ids: &[&str]) -> Vec<Content> {
        ids.iter().flat_map(|id| turn(id, true)).collect()
    }
    fn shown(ui: &Disclosure, blocks: &[Content]) -> Vec<String> {
        ui.project(blocks).into_iter().map(|b| b.id).collect()
    }
    #[test]
    fn only_the_newest_two_turns_start_open() {
        let mut ui = Disclosure::default();
        let blocks = page(&["t1", "t2", "t3"]);
        ui.sync_window(&blocks);
        assert_eq!(
            shown(&ui, &blocks),
            ["t1:user", "t2:user", "t2:reply", "t3:user", "t3:reply"]
        );
        let two = page(&["t1", "t2"]);
        ui.sync_window(&two);
        assert_eq!(shown(&ui, &two).len(), 4);
    }
    #[test]
    fn a_new_turn_folds_the_oldest_and_reports_where_to_rebuild() {
        let mut ui = Disclosure::default();
        assert_eq!(ui.sync_window(&page(&["t1", "t2"])), None);
        let blocks = page(&["t1", "t2", "t3"]);
        assert_eq!(ui.sync_window(&blocks), Some(0));
        assert_eq!(ui.sync_window(&blocks), None);
        let more = page(&["t1", "t2", "t3", "t4"]);
        assert_eq!(ui.sync_window(&more), Some(2)); // t2 left the window
    }
    #[test]
    fn explicit_choices_beat_the_default_and_survive_the_window_moving() {
        let mut ui = Disclosure::default();
        let blocks = page(&["t1", "t2", "t3"]);
        ui.sync_window(&blocks);
        let fold = json!({"kind":"turn_toggle","id":"t1"});
        assert!(ui.toggle(&fold, &blocks)); // opens the auto-folded turn
        assert_eq!(shown(&ui, &blocks).len(), 6);
        let more = page(&["t1", "t2", "t3", "t4"]);
        ui.sync_window(&more);
        assert!(shown(&ui, &more).contains(&"t1:reply".to_string()));
        assert!(!shown(&ui, &more).contains(&"t2:reply".to_string()));
        // Folding a recent turn is also remembered after it leaves the window.
        let t4 = json!({"kind":"turn_toggle","id":"t4"});
        assert!(ui.toggle(&t4, &more));
        assert!(!shown(&ui, &more).contains(&"t4:reply".to_string()));
        assert!(ui.toggle(&t4, &more));
        assert!(shown(&ui, &more).contains(&"t4:reply".to_string()));
    }
    #[test]
    fn a_turn_without_a_fold_handle_is_never_hidden() {
        let mut ui = Disclosure::default();
        let mut blocks = turn("t0", false);
        blocks.extend(page(&["t1", "t2", "t3"]));
        ui.sync_window(&blocks);
        assert!(shown(&ui, &blocks).contains(&"t0:reply".to_string()));
    }
    #[test]
    fn verbose_reveals_auto_folded_turns_and_restores_them() {
        let mut ui = Disclosure::default();
        let blocks = page(&["t1", "t2", "t3"]);
        ui.sync_window(&blocks);
        ui.verbose = true;
        assert_eq!(shown(&ui, &blocks).len(), 6);
        ui.verbose = false;
        assert_eq!(shown(&ui, &blocks).len(), 5);
    }

    #[test]
    fn folded_prompt_keeps_its_first_line_and_gets_the_stats_line() {
        let mut ui = Disclosure::default();
        let mut blocks = page(&["t1", "t2", "t3"]);
        blocks[0].title = "first line".into();
        blocks[0].text = "second line of the prompt".into();
        blocks[0].fold_summary = "2 tools · 1.2K tokens · model".into();
        ui.sync_window(&blocks);
        let folded = &ui.project(&blocks)[0];
        assert_eq!(folded.title, "first line …");
        assert_eq!(folded.text, "2 tools · 1.2K tokens · model");
        assert!(folded.collapsed);
        blocks[0].text.clear();
        assert_eq!(ui.project(&blocks)[0].title, "first line");
    }
}
