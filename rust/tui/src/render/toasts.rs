//! Toasts: dismissible, self-expiring notices drawn by the shared widget kit.
//!
//! The host (Python) assigns ids and sends the newest bounded list in every
//! snapshot; this module shows each id once and owns timers (paused while hovered),
//! same-key dedup and dismissal locally, like hover state. Layout is the kit's
//! `toast_layout`, used for both drawing and mouse hit-testing.
use super::{Palette, Snapshot};
use crate::bridge::ToastWire;
use nexus_widgets::{
    anim, theme::{mix, Level, Mode}, toast_layout, toast_stack, Glyphs, Theme, ToastRect, ToastStack, Ui,
};
use ratatui::{buffer::Buffer, layout::Rect, Frame};
use serde_json::Value;
use std::{collections::HashMap, time::{Duration, Instant}};

/// Redraw cadence while a toast is visible (the hairline moves; nothing else does).
const REDRAW: Duration = Duration::from_millis(250);

#[derive(Default)]
pub struct Toasts {
    pub stack: ToastStack,
    actions: HashMap<u64, Value>,
    rects: Vec<ToastRect>,
    last_redraw: Option<Instant>,
}

#[derive(Debug, PartialEq, Eq)]
pub enum Hit {
    Close(u64),
    Action(u64),
    Body(u64),
}

pub fn level_of(name: &str) -> Level {
    match name {
        "success" => Level::Success,
        "warning" => Level::Warning,
        "error" => Level::Error,
        _ => Level::Info,
    }
}

/// Map the native palette to the kit theme so toasts match everything else.
pub fn theme(p: &Palette, light: bool) -> Theme {
    Theme {
        mode: if light { Mode::Light } else { Mode::Dark },
        bg: p.background,
        text: p.text,
        muted: p.muted,
        quiet: p.quiet,
        accent: p.accent,
        surface: p.panel,
        raised: p.dialog,
        element: p.element,
        element_hi: p.element_hi,
        border: p.border,
        border_strong: p.border_strong,
        focus_bg: mix(p.panel, p.accent, 0.12),
        blue: p.blue,
        purple: p.purple,
        cyan: p.cyan,
        success: p.success,
        warning: p.warning,
        error: p.error,
    }
}

impl Toasts {
    /// Show every wire toast not seen before. Returns true when something was added.
    pub fn ingest(&mut self, wire: &[ToastWire]) -> bool {
        let mut added = false;
        for t in wire {
            let label = t.action.as_ref().map(|a| a.label.as_str()).unwrap_or("");
            if self.stack.ingest(t.id, level_of(&t.level), &t.title, &t.body, &t.key, label) {
                if let Some(op) = t.action.as_ref().and_then(|a| a.operation.clone()) {
                    self.actions.insert(t.id, op);
                }
                added = true;
            }
        }
        if self.actions.len() > 64 {
            let live: Vec<u64> = self.stack.toasts.iter().map(|t| t.id).collect();
            self.actions.retain(|id, _| live.contains(id));
        }
        added
    }

    #[allow(dead_code)]
    pub fn is_empty(&self) -> bool {
        self.stack.toasts.is_empty()
    }

    /// Advance timers. `pointer` pauses the toast under it. True when a redraw is due.
    pub fn tick(&mut self, now: Instant, pointer: Option<(u16, u16)>) -> bool {
        let hovered = pointer.and_then(|(x, y)| self.hit(x, y)).map(|h| match h {
            Hit::Close(id) | Hit::Action(id) | Hit::Body(id) => id,
        });
        let expired = self.stack.tick(now, hovered);
        if self.stack.toasts.is_empty() {
            self.rects.clear();
            self.last_redraw = None;
            return expired;
        }
        let due = self.last_redraw.map_or(true, |t| now.saturating_duration_since(t) >= REDRAW);
        expired || due
    }

    pub fn hit(&self, x: u16, y: u16) -> Option<Hit> {
        let inside = |r: Rect| x >= r.x && x < r.x + r.width && y >= r.y && y < r.y + r.height;
        self.rects.iter().find_map(|t| {
            if inside(t.close) {
                Some(Hit::Close(t.id))
            } else if t.action.is_some_and(inside) {
                Some(Hit::Action(t.id))
            } else if inside(t.rect) {
                Some(Hit::Body(t.id))
            } else {
                None
            }
        })
    }

    pub fn dismiss(&mut self, id: u64) {
        self.stack.dismiss(id);
    }
    pub fn dismiss_all(&mut self) {
        self.stack.dismiss_all();
    }
    /// The action of one toast, if it has one.
    pub fn action_of(&self, id: u64) -> Option<Value> {
        self.actions.get(&id).cloned()
    }
    /// The newest visible toast that has an action.
    pub fn newest_action(&self) -> Option<(u64, Value)> {
        self.stack.visible().find_map(|t| self.actions.get(&t.id).map(|op| (t.id, op.clone())))
    }

    /// Draw over everything else at the top-right of `area`.
    pub fn draw(&mut self, frame: &mut Frame, s: &Snapshot, p: &Palette, area: Rect) {
        if self.stack.toasts.is_empty() {
            self.rects.clear();
            return;
        }
        let light = s.theme == "nexus-light";
        let th = theme(p, light);
        let glyphs = Glyphs::from_env();
        let mut ui = Ui::new(&th, &glyphs);
        ui.begin_frame();
        draw_into(frame.buffer_mut(), &mut ui, area, &self.stack);
        self.rects = toast_layout(area, &self.stack);
        self.last_redraw = Some(Instant::now());
        let _ = anim::HOVER;
    }
}

fn draw_into(buf: &mut Buffer, ui: &mut Ui, area: Rect, stack: &ToastStack) {
    toast_stack(buf, ui, area, stack);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn wire(id: u64, level: &str, title: &str) -> ToastWire {
        ToastWire { id, level: level.into(), title: title.into(), ..Default::default() }
    }

    #[test]
    fn each_id_is_shown_once_even_though_snapshots_resend_the_list() {
        let mut t = Toasts::default();
        let list = vec![wire(5, "info", "Copied"), wire(6, "error", "Boom")];
        assert!(t.ingest(&list));
        assert!(!t.ingest(&list), "the same snapshot content adds nothing");
        t.dismiss(5);
        assert!(!t.ingest(&list), "a dismissed toast does not come back");
        assert_eq!(t.stack.toasts.len(), 1);
    }

    #[test]
    fn actions_are_kept_by_id_and_the_newest_wins() {
        let mut t = Toasts::default();
        let mut a = wire(1, "warning", "Removed");
        a.action = Some(crate::bridge::ToastAction { label: "Undo".into(), operation: Some(serde_json::json!({"kind": "undo"})) });
        t.ingest(&[a, wire(2, "info", "Plain")]);
        assert_eq!(t.newest_action().unwrap().0, 1);
        assert_eq!(t.action_of(1).unwrap()["kind"], "undo");
        assert!(t.action_of(2).is_none());
    }

    #[test]
    fn hover_pauses_expiry_and_info_toasts_expire() {
        let mut t = Toasts::default();
        let t0 = Instant::now();
        t.stack.tick(t0, None);
        t.ingest(&[wire(1, "info", "Copied")]);
        t.rects = toast_layout(Rect::new(0, 1, 120, 30), &t.stack);
        let r = t.rects[0].rect;
        let over = Some((r.x + 3, r.y));
        t.tick(t0 + Duration::from_secs(5), over);
        assert_eq!(t.stack.toasts.len(), 1, "paused while the pointer is on it");
        t.tick(t0 + Duration::from_secs(10), None);
        assert!(t.is_empty(), "info lives 4 s once unpaused");
    }

    #[test]
    fn close_button_and_action_have_their_own_hit_targets() {
        let mut t = Toasts::default();
        let mut a = wire(9, "error", "Could not reach OpenAI");
        a.body = "HTTP 503".into();
        a.action = Some(crate::bridge::ToastAction { label: "Details".into(), operation: None });
        t.ingest(&[a]);
        t.rects = toast_layout(Rect::new(0, 1, 120, 30), &t.stack);
        let l = t.rects[0];
        assert_eq!(t.hit(l.close.x + 1, l.close.y), Some(Hit::Close(9)));
        let act = l.action.unwrap();
        assert_eq!(t.hit(act.x, act.y), Some(Hit::Action(9)));
        assert_eq!(t.hit(l.rect.x + 6, l.rect.y + 1), Some(Hit::Body(9)));
        assert_eq!(t.hit(0, 0), None);
    }
}
