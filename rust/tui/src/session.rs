//! Stable prompt anchoring and permanent transcript tail space.
/// Adapter between the transcript cache and the pure [`Follow`] state: it owns the pending /
/// pinned prompt (scoped by session, page and generation) while the caller keeps the
/// live-vs-frozen choice.
#[derive(Default, Clone)]
pub struct PromptPin {
    mode: Follow,
}
impl PromptPin {
    /// Explicit submit: wait for the next user message not already in `blocks`.
    pub fn arm(&mut self, scope: Scope, blocks: &[crate::bridge::Content]) {
        let users: Vec<UserRow> = blocks
            .iter()
            .filter(|b| b.kind == "user" && !b.turn_id.is_empty())
            .enumerate()
            .map(|(rank, b)| UserRow {
                id: b.turn_id.clone(),
                rank,
                top: 0,
                height: 0,
            })
            .collect();
        self.mode = Follow::submit(scope, &users);
    }
    /// End key, scrolling away, or a frozen view: drop any pin.
    pub fn release(&mut self) {
        self.mode = Follow::end();
    }
    /// An async turn finished: a submit that never produced a prompt stops waiting.
    /// A prompt already present in `blocks` is kept: the next frame pins it.
    pub fn finish(&mut self, blocks: &[crate::bridge::Content]) {
        if let Follow::Pending { seen, .. } = &self.mode {
            let arrived = blocks
                .iter()
                .any(|b| b.kind == "user" && !b.turn_id.is_empty() && !seen.contains(&b.turn_id));
            if arrived {
                return;
            }
        }
        self.mode.finish();
    }
    /// Whether a submit is still waiting for its prompt.
    #[cfg(test)]
    fn is_pending(&self) -> bool {
        matches!(self.mode, Follow::Pending { .. })
    }
    /// Offset pinning the prompt's top row, or `None` to follow the live bottom.
    pub fn target(
        &mut self,
        scope: &Scope,
        blocks: &[crate::bridge::Content],
        cache: &crate::render::Cache,
        height: usize,
    ) -> Option<usize> {
        if self.mode.is_latest() {
            return None;
        }
        let ops = &cache.operations;
        let mut users = Vec::new();
        for (index, b) in blocks
            .iter()
            .enumerate()
            .filter(|(_, b)| b.kind == "user" && !b.turn_id.is_empty())
        {
            let Some(top) = cache.prompt_top(&b.turn_id) else {
                continue;
            };
            // The whole card: the block's laid-out rows. Without block metadata, infer up to
            // the next prompt marker (the marker row itself counts as one).
            let rows = cache.block_rows(index).unwrap_or_else(|| {
                1 + ops
                    .range(top + 1, usize::MAX)
                    .take_while(|op| {
                        op.as_ref().map_or(true, |op| op["prompt_turn"].is_null())
                    })
                    .count()
            });
            users.push(UserRow {
                id: b.turn_id.clone(),
                rank: users.len(),
                top,
                height: rows,
            });
        }
        let m = Metrics {
            content: cache.lines.len(),
            height,
        };
        let r = self.mode.resolve(scope, m, &users);
        self.mode = r.follow;
        matches!(self.mode, Follow::Prompt(_)).then_some(r.offset)
    }
}
pub fn tail_padding(content: usize, height: usize) -> usize {
    if content == 0 {
        0
    } else {
        height.saturating_sub(2)
    }
}

/// Which transcript is on screen. A pin never applies outside the scope that made it, so
/// switching session or between the root and a subagent page cannot reuse a stale pin.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Scope {
    pub session: String,
    /// Agent page id; empty for the root transcript.
    pub page: String,
    /// Host snapshot generation; a reset (resume, clear) changes it and drops pins.
    pub generation: u64,
}

/// A user message in the current transcript with its laid-out rows (no tail padding).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct UserRow {
    pub id: String,
    pub rank: usize,
    pub top: usize,
    pub height: usize,
}

/// Stable identity of a pinned prompt: scope + message id (+ ordinal rank among user messages).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PromptAnchor {
    pub scope: Scope,
    pub id: String,
    pub rank: usize,
    pub height: usize,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Metrics {
    /// Transcript rows before tail padding.
    pub content: usize,
    /// Viewport rows.
    pub height: usize,
}
impl Metrics {
    pub fn pad(self) -> usize {
        tail_padding(self.content, self.height)
    }
    pub fn max_scroll(self) -> usize {
        (self.content + self.pad()).saturating_sub(self.height)
    }
    /// Offset that shows the last row at the bottom of the viewport.
    pub fn live(self) -> usize {
        self.content.saturating_sub(self.height)
    }
}

/// Scroll mode, replacing the `follow` bool + `scroll` pair.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub enum Follow {
    /// Stick to the latest row.
    #[default]
    Latest,
    /// Submitted; waiting for the new user message (ids in `seen` are older).
    Pending { scope: Scope, seen: Vec<String> },
    /// The prompt's top row is pinned until its turn fills the viewport.
    Prompt(PromptAnchor),
    /// The user scrolled away; only an explicit submit/jump/End moves it again.
    Frozen(usize),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Resolved {
    pub offset: usize,
    pub follow: Follow,
}

impl Follow {
    pub fn is_latest(&self) -> bool {
        matches!(self, Follow::Latest)
    }
    /// Explicit submit: wait for the new prompt, whatever the previous mode (even Frozen).
    pub fn submit(scope: Scope, users: &[UserRow]) -> Self {
        Follow::Pending {
            scope,
            seen: users.iter().map(|u| u.id.clone()).collect(),
        }
    }
    /// Explicit jump to a prompt (e.g. navigation to a turn).
    pub fn jump(scope: Scope, user: &UserRow) -> Self {
        Follow::Prompt(PromptAnchor {
            scope,
            id: user.id.clone(),
            rank: user.rank,
            height: user.height,
        })
    }
    /// Explicit jump to the end (End key).
    pub fn end() -> Self {
        Follow::Latest
    }
    /// Async turn finish. Never moves the viewport: a frozen view stays frozen and a pinned
    /// prompt stays; a still-pending submit that produced no prompt stops waiting.
    pub fn finish(&mut self) {
        if matches!(self, Follow::Pending { .. }) {
            *self = Follow::Latest;
        }
    }
    /// User scroll by `delta` rows from what is on screen (`current` is the resolved offset).
    /// Landing on the live bottom resumes following; anything else freezes.
    pub fn scrolled(current: usize, delta: isize, m: Metrics) -> Self {
        let next = current.saturating_add_signed(delta).min(m.max_scroll());
        if next == m.live() {
            Follow::Latest
        } else {
            Follow::Frozen(next)
        }
    }
    /// Pure resolution of the viewport offset for this frame. `users` are the user messages
    /// of the transcript on screen in order. Returns the offset (clamped to `max_scroll`)
    /// and the mode to store (pins release when their turn fills the viewport, vanish on a
    /// scope change or when the message is gone).
    pub fn resolve(&self, scope: &Scope, m: Metrics, users: &[UserRow]) -> Resolved {
        let latest = || Resolved {
            offset: m.live(),
            follow: Follow::Latest,
        };
        match self {
            Follow::Latest => latest(),
            Follow::Frozen(n) => Resolved {
                offset: (*n).min(m.max_scroll()),
                follow: Follow::Frozen((*n).min(m.max_scroll())),
            },
            Follow::Pending { scope: s, seen } => {
                if s != scope {
                    return latest();
                }
                match users.iter().find(|u| !seen.contains(&u.id)) {
                    Some(u) => Follow::jump(scope.clone(), u).resolve(scope, m, users),
                    // The prompt has not arrived: keep following the bottom, keep waiting.
                    None => Resolved {
                        offset: m.live(),
                        follow: self.clone(),
                    },
                }
            }
            Follow::Prompt(a) => {
                if &a.scope != scope {
                    return latest();
                }
                let Some(u) = users.iter().find(|u| u.id == a.id) else {
                    return latest();
                };
                // A card at least as tall as the viewport leaves nothing to pin below it.
                if u.height >= m.height || m.content.saturating_sub(u.top) >= m.height {
                    return latest();
                }
                let mut anchor = a.clone();
                anchor.rank = u.rank;
                anchor.height = u.height;
                Resolved {
                    offset: u.top.min(m.max_scroll()),
                    follow: Follow::Prompt(anchor),
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn scope(page: &str) -> Scope {
        Scope {
            session: "s".into(),
            page: page.into(),
            generation: 1,
        }
    }
    fn user(id: &str, rank: usize, top: usize) -> UserRow {
        UserRow {
            id: id.into(),
            rank,
            top,
            height: 2,
        }
    }
    const M: Metrics = Metrics {
        content: 30,
        height: 10,
    };

    #[test]
    fn latest_shows_bottom_with_tail_available() {
        let r = Follow::Latest.resolve(&scope(""), M, &[]);
        assert_eq!((r.offset, M.max_scroll()), (20, 28));
    }
    #[test]
    fn submit_pins_new_prompt_and_adopts_it() {
        let old = [user("a", 0, 0)];
        let f = Follow::submit(scope(""), &old);
        // Not arrived yet: follow bottom, keep waiting.
        let r = f.resolve(&scope(""), M, &old);
        assert_eq!((r.offset, &r.follow), (20, &f));
        let now = [user("a", 0, 0), user("b", 1, 26)];
        let m = Metrics {
            content: 30,
            height: 10,
        };
        let r = f.resolve(&scope(""), m, &now);
        assert_eq!(r.offset, 26);
        assert!(matches!(r.follow, Follow::Prompt(ref a) if a.id == "b" && a.rank == 1));
    }
    #[test]
    fn pin_releases_when_turn_fills_viewport() {
        let now = [user("b", 1, 26)];
        let f = Follow::jump(scope(""), &now[0]);
        let grown = Metrics {
            content: 36,
            height: 10,
        };
        let r = f.resolve(&scope(""), grown, &now);
        assert_eq!((r.offset, r.follow), (26, Follow::Latest));
    }
    #[test]
    fn frozen_ignores_arrival_and_finish_until_submit() {
        let mut f = Follow::scrolled(20, -5, M);
        assert_eq!(f, Follow::Frozen(15));
        f.finish();
        let grown = Metrics {
            content: 60,
            height: 10,
        };
        assert_eq!(f.resolve(&scope(""), grown, &[]).offset, 15);
        let f = Follow::submit(scope(""), &[]);
        assert!(matches!(f, Follow::Pending { .. }));
    }
    #[test]
    fn frozen_clamps_on_resize_and_scroll_to_bottom_follows() {
        let small = Metrics {
            content: 12,
            height: 10,
        };
        assert_eq!(Follow::Frozen(25).resolve(&scope(""), small, &[]).offset, 10);
        // M.live() is 20: landing exactly there follows; past it (tail padding) freezes.
        assert_eq!(Follow::scrolled(18, 2, M), Follow::Latest);
        assert_eq!(Follow::scrolled(18, 5, M), Follow::Frozen(23));
        assert_eq!(Follow::scrolled(2, -9, M), Follow::Frozen(0));
    }
    #[test]
    fn pin_resets_on_scope_change_root_vs_subagent_and_generation() {
        let u = [user("b", 1, 26)];
        let f = Follow::jump(scope(""), &u[0]);
        assert_eq!(f.resolve(&scope("agent-1"), M, &u).follow, Follow::Latest);
        let mut g = scope("");
        g.generation = 2;
        assert_eq!(f.resolve(&g, M, &u).follow, Follow::Latest);
        assert!(matches!(f.resolve(&scope(""), M, &u).follow, Follow::Prompt(_)));
        // Missing message drops the pin.
        assert_eq!(f.resolve(&scope(""), M, &[]).follow, Follow::Latest);
    }
    #[test]
    fn pin_survives_resize_by_id() {
        let u = [user("b", 1, 26)];
        let f = Follow::jump(scope(""), &u[0]);
        let moved = [user("b", 1, 20)];
        let m = Metrics {
            content: 24,
            height: 10,
        };
        assert_eq!(f.resolve(&scope(""), m, &moved).offset, 20);
    }
    #[test]
    fn finish_ends_pending_but_keeps_pin() {
        let mut f = Follow::submit(scope(""), &[]);
        f.finish();
        assert!(f.is_latest());
        let mut p = Follow::jump(scope(""), &user("b", 1, 26));
        p.finish();
        assert!(matches!(p, Follow::Prompt(_)));
    }
    fn user_block(turn: &str) -> crate::bridge::Content {
        crate::bridge::Content {
            kind: "user".into(),
            turn_id: turn.into(),
            ..Default::default()
        }
    }
    fn cache_with(rows: usize, prompts: &[(usize, &str)]) -> crate::render::Cache {
        let mut cache = crate::render::Cache::default();
        let mut ops = vec![None; rows];
        for (row, turn) in prompts {
            ops[*row] = Some(serde_json::json!({ "prompt_turn": turn }));
        }
        cache.operations = ops.into();
        cache.lines = vec![ratatui::text::Line::default(); rows].into();
        cache
    }

    #[test]
    fn adapter_pins_next_user_in_scope_and_ignores_prior() {
        let old = [user_block("a")];
        let mut pin = PromptPin::default();
        pin.arm(scope(""), &old);
        let now = [user_block("a"), user_block("b")];
        let cache = cache_with(30, &[(0, "a"), (26, "b")]);
        assert_eq!(pin.target(&scope(""), &now, &cache, 10), Some(26));
        // Another page never reuses the pin; it is gone afterwards even back on the root.
        assert_eq!(pin.target(&scope("agent-1"), &now, &cache, 10), None);
        assert_eq!(pin.target(&scope(""), &now, &cache, 10), None);
    }
    #[test]
    fn adapter_waits_then_releases_when_turn_fills_viewport() {
        let old = [user_block("a")];
        let mut pin = PromptPin::default();
        pin.arm(scope(""), &old);
        let cache = cache_with(30, &[(0, "a")]);
        assert_eq!(pin.target(&scope(""), &old, &cache, 10), None);
        let now = [user_block("a"), user_block("b")];
        let grown = cache_with(40, &[(0, "a"), (26, "b")]);
        assert_eq!(pin.target(&scope(""), &now, &grown, 10), None);
        pin.release();
        assert_eq!(pin.target(&scope(""), &now, &cache, 10), None);
    }

    fn laid_out(blocks: serde_json::Value, width: u16) -> crate::render::Cache {
        let blocks: Vec<crate::bridge::Content> = serde_json::from_value(blocks).unwrap();
        let mut cache = crate::render::Cache::default();
        cache.update_content(&[], &blocks, width, &crate::render::Palette::new(false));
        cache
    }
    fn snapshot(blocks: serde_json::Value) -> crate::bridge::Snapshot {
        serde_json::from_value(serde_json::json!({
            "composer_key": "s", "generation": 1, "blocks": blocks
        }))
        .unwrap()
    }
    fn filler(count: usize) -> Vec<serde_json::Value> {
        (0..count)
            .map(|i| serde_json::json!({"id": format!("m{i}"), "kind": "markdown", "text": format!("line {i}")}))
            .collect()
    }
    fn frame(
        s: &crate::bridge::Snapshot,
        cache: &mut crate::render::Cache,
        scroll: usize,
        follow: bool,
    ) {
        use crate::editor::Editor;
        let mut terminal =
            ratatui::Terminal::new(ratatui::backend::TestBackend::new(60, 20)).unwrap();
        terminal
            .draw(|f| {
                crate::render::draw(
                    f,
                    s,
                    &Editor::default(),
                    &Editor::default(),
                    &Editor::default(),
                    "",
                    0,
                    scroll,
                    0,
                    cache,
                    follow,
                    false,
                    false,
                    false,
                    0,
                    0,
                );
            })
            .unwrap();
    }

    #[test]
    fn user_row_height_is_the_whole_multiline_card() {
        let blocks = serde_json::json!([
            {"id": "u", "kind": "user", "turn_id": "t1", "title": "prompt", "text": "one\ntwo\nthree\nfour"}
        ]);
        let cache = laid_out(blocks.clone(), 60);
        let parsed: Vec<crate::bridge::Content> = serde_json::from_value(blocks).unwrap();
        // Padding + header + four body lines: the body, not just the header, is counted.
        assert!(cache.block_rows(0).unwrap() >= 7);
        let mut pin = PromptPin::default();
        pin.arm(scope(""), &[]);
        // Adopting the prompt stores its card height in the anchor.
        pin.target(&scope(""), &parsed, &cache, 40);
        let Follow::Prompt(a) = &pin.mode else {
            panic!("prompt not pinned");
        };
        assert_eq!(Some(a.height), cache.block_rows(0));
        assert!(a.height >= 7);
    }
    #[test]
    fn prompt_taller_than_viewport_releases_and_offset_clamps() {
        let blocks = serde_json::json!([
            {"id": "u", "kind": "user", "turn_id": "t1", "title": "prompt", "text": "a\nb\nc\nd\ne\nf\ng\nh"}
        ]);
        let cache = laid_out(blocks.clone(), 60);
        let parsed: Vec<crate::bridge::Content> = serde_json::from_value(blocks).unwrap();
        // The fixture must really be tall, or the assertion below proves nothing.
        assert!(cache.block_rows(0).unwrap() >= 4);
        let mut pin = PromptPin::default();
        pin.arm(scope(""), &[]);
        // The card fills the 4-row viewport: nothing left to pin, follow the bottom.
        assert_eq!(pin.target(&scope(""), &parsed, &cache, 4), None);
        // Pure: a pin whose top sits past the last scrollable row is clamped.
        let m = Metrics {
            content: 12,
            height: 10,
        };
        let f = Follow::jump(scope(""), &user("b", 1, 11));
        assert_eq!(f.resolve(&scope(""), m, &[user("b", 1, 11)]).offset, m.max_scroll());
    }
    #[test]
    fn finish_only_stops_a_pending_submit_without_a_prompt() {
        let mut pin = PromptPin::default();
        pin.arm(scope(""), &[]);
        pin.finish(&[]);
        assert!(!pin.is_pending());
        // The prompt arrived in the same snapshot as the finish: keep it for the next frame.
        pin.arm(scope(""), &[]);
        pin.finish(&[user_block("a")]);
        assert!(pin.is_pending());
        // A frozen view (released pin) stays released.
        pin.release();
        pin.finish(&[user_block("a")]);
        assert!(!pin.is_pending());
    }
    #[test]
    fn drawn_offset_tracks_live_pinned_and_frozen_frames() {
        let mut items = filler(30);
        let mut cache = crate::render::Cache::default();
        let s = snapshot(items.clone().into());
        frame(&s, &mut cache, 0, true);
        assert_eq!(cache.current_offset, cache.live_scroll);
        assert!(cache.live_scroll > 0);

        // Submit, then the prompt lands near the bottom: the pin shows it, not the live row.
        cache.prompt_pin.arm(
            Scope {
                session: "s".into(),
                page: String::new(),
                generation: 1,
            },
            &s.blocks,
        );
        items.push(serde_json::json!({"id": "u", "kind": "user", "turn_id": "t1", "title": "hi"}));
        let s = snapshot(items.clone().into());
        frame(&s, &mut cache, 0, true);
        let top = cache.prompt_top("t1").unwrap();
        assert_eq!(cache.current_offset, top.min(cache.max_scroll));
        assert!(cache.current_offset > cache.live_scroll);

        // Scrolling away (follow false) draws the requested offset and drops the pin.
        frame(&s, &mut cache, 3, false);
        assert_eq!(cache.current_offset, 3);
        frame(&s, &mut cache, usize::MAX, false);
        assert_eq!(cache.current_offset, cache.max_scroll);
        frame(&s, &mut cache, 0, true);
        assert_eq!(cache.current_offset, cache.live_scroll);
    }

    #[test]
    fn explicit_end_follows() {
        assert!(Follow::end().is_latest());
    }

    #[test]
    fn permanent_tail_padding() {
        assert_eq!(super::tail_padding(0, 20), 0);
        assert_eq!(super::tail_padding(3, 20), 18);
        assert_eq!(super::tail_padding(300, 20), 18);
        assert_eq!(super::tail_padding(3, 1), 0);
    }
}
