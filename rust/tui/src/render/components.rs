//! Shared native component styles and bounded, presentation-only hover state.
use super::{composer_context_at, composer_control_at, Palette, Snapshot};
use ratatui::{
    layout::Rect,
    style::{Color, Modifier, Style},
};
use std::time::{Duration, Instant};

pub const HOVER_DURATION: Duration = Duration::from_millis(100);

#[derive(Clone, Copy, Default)]
pub struct State {
    pub disabled: bool,
    pub selected: bool,
    pub focused: bool,
    pub hover: f32,
}

/// Color-only styling: none of these primitives changes geometry or focus.
pub fn button(p: &Palette, foreground: Color, state: State) -> Style {
    if state.disabled {
        return Style::default().fg(p.quiet).bg(p.panel);
    }
    let mut style = Style::default()
        .fg(foreground)
        .bg(mix(p.panel, p.element_hi, state.hover));
    if state.selected {
        style = style
            .fg(p.accent)
            .bg(mix(p.element, p.element_hi, state.hover))
            .add_modifier(Modifier::BOLD);
    }
    if state.focused {
        style = style.add_modifier(Modifier::UNDERLINED);
    }
    style
}
#[allow(dead_code)]
pub fn toggle(p: &Palette, state: State) -> Style {
    button(p, p.text, state)
}
#[allow(dead_code)]
pub fn selectable(p: &Palette, state: State) -> Style {
    let mut style = Style::default()
        .fg(p.text)
        .bg(mix(p.dialog, p.element_hi, state.hover));
    if state.selected {
        style = style
            .fg(p.background)
            .bg(mix(p.accent, p.element_hi, state.hover * 0.35))
            .add_modifier(Modifier::BOLD);
    }
    if state.focused {
        style = style.add_modifier(Modifier::UNDERLINED);
    }
    if state.disabled {
        style = Style::default().fg(p.quiet).bg(p.dialog);
    }
    style
}
#[allow(dead_code)]
pub fn section(p: &Palette) -> Style {
    Style::default().fg(p.muted).add_modifier(Modifier::BOLD)
}

fn mix(from: Color, to: Color, amount: f32) -> Color {
    match (from, to) {
        (Color::Rgb(r, g, b), Color::Rgb(rr, gg, bb)) => {
            let channel = |a: u8, b: u8| {
                (a as f32 + (b as f32 - a as f32) * amount.clamp(0.0, 1.0)).round() as u8
            };
            Color::Rgb(channel(r, rr), channel(g, gg), channel(b, bb))
        }
        _ => {
            if amount >= 0.5 {
                to
            } else {
                from
            }
        }
    }
}

/// Typed identities for overlay controls; legacy chrome retains its existing keys.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HoverId {
    SettingsNav(usize),
    PromptChoice(usize),
    CompletionRow(usize),
}
impl HoverId {
    fn key(self) -> String {
        match self {
            Self::SettingsNav(i) => format!("nav:{i}"),
            Self::PromptChoice(i) => format!("prompt:{i}"),
            Self::CompletionRow(i) => format!("completion:{i}"),
        }
    }
}

#[derive(Debug)]
struct Transition {
    id: String,
    from: f32,
    to: f32,
    began: Instant,
    finished: bool,
}
impl Transition {
    fn value(&self, now: Instant) -> f32 {
        let progress =
            now.saturating_duration_since(self.began).as_secs_f32() / HOVER_DURATION.as_secs_f32();
        self.from + (self.to - self.from) * progress.clamp(0.0, 1.0)
    }
}
#[derive(Default, Debug)]
pub struct Hover {
    target: Option<String>,
    transitions: Vec<Transition>,
    scope: Option<(u64, String, Rect, String)>,
    pending: bool,
    suppressed_pointer: Option<(u16, u16)>,
}
impl Hover {
    /// Reuse the click layout. Overlays and session/layout changes discard stale state.
    pub fn update(
        &mut self,
        s: &Snapshot,
        area: Rect,
        pointer: Option<(u16, u16)>,
        blocked: bool,
        now: Instant,
    ) -> bool {
        let target = pointer
            .and_then(|(x, y)| {
                composer_control_at(area, s, x, y)
                    .or_else(|| composer_context_at(area, s, x, y).then_some("context"))
            })
            .map(str::to_owned);
        self.update_resolved(s, area, String::new(), pointer, blocked, target, now)
    }
    /// Resolve local presentation targets with the same geometry used by clicks.
    pub fn update_surfaces(
        &mut self,
        s: &Snapshot,
        r: &super::Regions,
        filter: &str,
        selection: usize,
        detail: bool,
        pointer: Option<(u16, u16)>,
        completion: bool,
        now: Instant,
    ) -> bool {
        let blocked = completion;
        let target = pointer.and_then(|(x, y)| {
            let point = (x, y).into();
            if !s.panel_title.is_empty() {
                let area = super::panel_area(super::panel_host(&r, s), s);
                if !area.contains(point) {
                    return None;
                }
                if s.panel_loading {
                    return None;
                }
                if let Some(i) = super::dialogs::nav_at(s, area, x, y) {
                    return Some(HoverId::SettingsNav(i).key());
                }
                if detail || s.form.is_some() || s.panel_format == "image" {
                    return None;
                }
                let mut inner = super::panel_inner(area, s.panel_layout == "drawer");
                if s.nav.is_some() {
                    let taken = super::nav_rect(area).width + 2;
                    inner.x += taken;
                    inner.width = inner.width.saturating_sub(taken);
                }
                if !inner.contains(point) {
                    return None;
                }
                return super::panel_item_at(s, area, &filter.to_lowercase(), selection, y).map(
                    |i| {
                        if super::panel_toggle_at(s, area, &filter.to_lowercase(), selection, x, y)
                        {
                            format!("toggle:{i}")
                        } else {
                            format!("item:{i}")
                        }
                    },
                );
            }
            if let Some(prompt) = &s.prompt {
                return super::dialogs::prompt_choice_at(r.transcript, prompt, x, y)
                    .map(|i| HoverId::PromptChoice(i).key());
            }
            if r.tabs.contains(point) && y == r.tabs.y {
                let col = usize::from(x - r.tabs.x);
                return super::top_cells(s, r.tabs)
                    .into_iter()
                    .position(|cell| col >= cell.start && col < cell.end)
                    .map(|i| format!("tab:{i}"));
            }
            if r.details.contains(point) && y == r.details.y {
                return super::details_tab_at(r.details, x).map(|tab| format!("details:{tab}"));
            }
            composer_control_at(r.composer, s, x, y)
                .or_else(|| composer_context_at(r.composer, s, x, y).then_some("context"))
                .map(str::to_owned)
        });
        let scope = format!(
            "{}:{filter}:{selection}:{detail}:{}:{:?}:{:?}:{:?}:{:?}",
            s.panel_title,
            s.items.len(),
            r.tabs,
            r.transcript,
            r.details,
            s.prompt.as_ref().map(|p| (&p.id, p.choices.len()))
        );
        self.update_resolved(s, r.composer, scope, pointer, blocked, target, now)
    }
    pub fn amount_id(&self, id: HoverId, now: Instant) -> f32 {
        self.amount(&id.key(), now)
    }
    pub fn update_completion(
        &mut self,
        s: &Snapshot,
        r: &super::Regions,
        selected: usize,
        pointer: Option<(u16, u16)>,
        now: Instant,
    ) -> bool {
        let target = pointer.and_then(|(x, y)| {
            super::dialogs::completion_at(r.transcript, s.completions.len(), selected, x, y)
                .map(|i| HoverId::CompletionRow(i).key())
        });
        let scope = format!(
            "completion:{:?}:{selected}:{:?}",
            s.completions, r.transcript
        );
        self.update_resolved(s, r.composer, scope, pointer, false, target, now)
    }
    fn update_resolved(
        &mut self,
        s: &Snapshot,
        area: Rect,
        surface: String,
        pointer: Option<(u16, u16)>,
        blocked: bool,
        target: Option<String>,
        now: Instant,
    ) -> bool {
        let active = s
            .tabs
            .iter()
            .find(|tab| tab.active)
            .map(|tab| tab.id.clone())
            .unwrap_or_default();
        let scope = (s.generation, active, area, surface);
        let mut changed = false;
        if blocked || !s.agent_page.is_empty() || self.scope.as_ref() != Some(&scope) {
            changed = self.target.is_some() || !self.transitions.is_empty();
            self.target = None;
            self.transitions.clear();
            self.scope = Some(scope);
            self.suppressed_pointer = pointer;
            if blocked || !s.agent_page.is_empty() {
                self.pending |= changed;
                return changed;
            }
            // A new scope requires a fresh pointer event before showing hover.
            if changed {
                self.pending = true;
                return true;
            }
        }
        if pointer == self.suppressed_pointer {
            return changed;
        }
        self.suppressed_pointer = None;
        changed |= self.set_target(target.as_deref(), now);
        self.pending |= changed;
        changed
    }
    /// Tint existing tab chrome without changing its labels or click ranges.
    pub fn paint_tabs(&self, frame: &mut ratatui::Frame, s: &Snapshot, r: &super::Regions) {
        if !s.panel_title.is_empty() || s.prompt.is_some() {
            return;
        }
        let p = Palette::new(s.theme == "nexus-light");
        let now = Instant::now();
        for (i, cell) in super::top_cells(s, r.tabs).iter().enumerate() {
            let amount = self.amount(&format!("tab:{i}"), now);
            if amount <= 0.0 {
                continue;
            }
            let selected = match cell.kind {
                super::TabHit::Tab(index) => s.tabs[index].active,
                super::TabHit::Sessions => s.sessions_sidebar,
                super::TabHit::Details => s.details_sidebar,
                _ => false,
            };
            let style = button(
                &p,
                p.text,
                State {
                    selected,
                    hover: amount,
                    ..Default::default()
                },
            );
            frame.buffer_mut().set_style(
                Rect::new(
                    r.tabs.x + cell.start as u16,
                    r.tabs.y,
                    (cell.end - cell.start) as u16,
                    1,
                ),
                Style::default().bg(style.bg.unwrap()),
            );
        }
        let mut x = r.details.x + 2;
        for tab in ["Session", "Files", "MCP", "Logs"] {
            let width = tab.len() as u16 + 1;
            let amount = self.amount(&format!("details:{tab}"), now);
            if amount > 0.0 && r.details.width > 0 && x < r.details.right() {
                frame.buffer_mut().set_style(
                    Rect::new(x, r.details.y, width.min(r.details.right() - x), 1),
                    Style::default().bg(mix(p.panel, p.element_hi, amount)),
                );
            }
            x += width;
        }
    }
    fn set_target(&mut self, target: Option<&str>, now: Instant) -> bool {
        if self.target.as_deref() == target {
            return false;
        }
        let previous = self.target.take();
        self.target = target.map(str::to_owned);
        // Only the leaving and entering controls can animate. Rapid movement
        // through a long menu must not accumulate one transition per row.
        self.transitions.retain(|t| {
            Some(t.id.as_str()) == previous.as_deref() || Some(t.id.as_str()) == target
        });
        for (id, to) in previous
            .map(|id| (id, 0.0))
            .into_iter()
            .chain(target.map(|id| (id.to_owned(), 1.0)))
        {
            let from = self.amount(&id, now);
            self.transitions.retain(|transition| transition.id != id);
            self.transitions.push(Transition {
                id,
                from,
                to,
                began: now,
                finished: false,
            });
        }
        true
    }
    pub fn amount(&self, id: &str, now: Instant) -> f32 {
        self.transitions
            .iter()
            .find(|transition| transition.id == id)
            .map(|transition| transition.value(now))
            .unwrap_or(0.0)
    }
    /// Include the final endpoint frame, then stop requesting redraws at rest.
    pub fn needs_redraw(&self, now: Instant) -> bool {
        self.pending
            || self.transitions.iter().any(|transition| {
                !transition.finished
                    || now.saturating_duration_since(transition.began) < HOVER_DURATION
            })
    }
    pub fn painted(&mut self, now: Instant) {
        self.pending = false;
        for transition in &mut self.transitions {
            transition.finished = now.saturating_duration_since(transition.began) >= HOVER_DURATION;
        }
        self.transitions.retain(|transition| {
            transition.to != 0.0 || now.saturating_duration_since(transition.began) < HOVER_DURATION
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn tab_hover_reuses_click_ranges_and_preserves_active_tab() {
        let now = Instant::now();
        let s = Snapshot::default();
        let r = super::super::Regions {
            tabs: Rect::new(0, 0, 100, 2),
            ..Default::default()
        };
        let cells = super::super::top_cells(&s, r.tabs);
        let mut hover = Hover::default();
        let pointer = Some((cells[0].start as u16, 0));
        hover.update_surfaces(&s, &r, "", 0, false, None, false, now);
        hover.update_surfaces(&s, &r, "", 0, false, pointer, false, now);
        assert_eq!(hover.target.as_deref(), Some("tab:0"));
        hover.update_surfaces(&s, &r, "", 0, false, pointer, true, now);
        assert!(hover.target.is_none());
    }
    #[test]
    fn selected_hover_and_disabled_styles_are_distinct() {
        for light in [false, true] {
            let p = Palette::new(light);
            let selected = State {
                selected: true,
                ..Default::default()
            };
            assert_ne!(
                selectable(&p, selected).bg,
                selectable(
                    &p,
                    State {
                        hover: 1.0,
                        ..selected
                    }
                )
                .bg
            );
            assert_ne!(
                button(&p, p.text, selected).bg,
                button(
                    &p,
                    p.text,
                    State {
                        hover: 1.0,
                        ..selected
                    }
                )
                .bg
            );
            let disabled = State {
                disabled: true,
                ..selected
            };
            assert_eq!(
                toggle(&p, disabled),
                toggle(
                    &p,
                    State {
                        hover: 1.0,
                        ..disabled
                    }
                )
            );
        }
    }
    #[test]
    fn menu_overlay_targets_its_own_rows_without_selection_changes() {
        let now = Instant::now();
        let mut s = Snapshot {
            panel_title: "Models".into(),
            items: vec![crate::bridge::Item {
                label: "Model A".into(),
                ..Default::default()
            }],
            ..Default::default()
        };
        let r = super::super::Regions {
            transcript: Rect::new(0, 0, 100, 40),
            composer: Rect::new(0, 40, 100, 9),
            ..Default::default()
        };
        let area = super::super::panel_area(r.transcript, &s);
        let inner = super::super::panel_inner(area, false);
        let point = (inner.x + 1, inner.y + 2);
        assert_eq!(
            super::super::panel_item_at(&s, area, "", 0, point.1),
            Some(0)
        );
        let mut hover = Hover::default();
        hover.update_surfaces(&s, &r, "", 0, false, None, false, now);
        hover.update_surfaces(&s, &r, "", 0, false, Some(point), false, now);
        assert_eq!(hover.target.as_deref(), Some("item:0"));
        assert_eq!(s.items[0].label, "Model A");
        hover.painted(now + HOVER_DURATION);
        assert!(!hover.needs_redraw(now + HOVER_DURATION));
        s.panel_title.clear();
        hover.update_surfaces(
            &s,
            &r,
            "",
            0,
            false,
            Some(point),
            false,
            now + HOVER_DURATION,
        );
        assert!(hover.target.is_none());
    }
    #[test]
    fn dynamic_targets_remain_bounded() {
        let mut hover = Hover::default();
        let now = Instant::now();
        for i in 0..100 {
            hover.set_target(Some(&format!("item:{i}")), now);
            assert!(hover.transitions.len() <= 2);
        }
    }
    #[test]
    fn hover_is_bounded_and_reversible() {
        let now = Instant::now();
        let mut hover = Hover::default();
        assert!(hover.set_target(Some("/model"), now));
        assert!(!hover.set_target(Some("/model"), now));
        assert!((hover.amount("/model", now + Duration::from_millis(50)) - 0.5).abs() < 0.01);
        hover.set_target(None, now + Duration::from_millis(50));
        assert!((hover.amount("/model", now + Duration::from_millis(100)) - 0.25).abs() < 0.01);
        hover.painted(now + Duration::from_millis(150));
        assert!(!hover.needs_redraw(now + Duration::from_millis(150)));
        assert_eq!(
            hover.amount("/model", now + Duration::from_millis(150)),
            0.0
        );
    }
    #[test]
    fn component_states_are_distinct() {
        for light in [false, true] {
            let p = Palette::new(light);
            let base = button(&p, p.text, State::default());
            for state in [
                State {
                    disabled: true,
                    hover: 1.0,
                    ..State::default()
                },
                State {
                    selected: true,
                    ..State::default()
                },
                State {
                    focused: true,
                    ..State::default()
                },
                State {
                    hover: 1.0,
                    ..State::default()
                },
            ] {
                assert_ne!(base, button(&p, p.text, state));
            }
        }
    }
    #[test]
    fn hit_layout_and_overlay_clear() {
        let mut s = Snapshot::default();
        s.agent = "root".into();
        s.model = "model".into();
        let area = Rect::new(0, 10, 100, 9);
        let y = super::super::composer_rows(area, &s)[2].y;
        let now = Instant::now();
        let mut hover = Hover::default();
        hover.update(&s, area, None, false, now);
        hover.update(&s, area, Some((5, y)), false, now);
        assert_eq!(hover.target.as_deref(), Some("/agent"));
        hover.update(&s, area, Some((5, y)), true, now);
        assert_eq!(hover.target, None);
        assert_eq!(hover.amount("/agent", now), 0.0);
    }
}
