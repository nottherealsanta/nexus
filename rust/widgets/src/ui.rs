//! The per-frame context every component receives.
use crate::{anim, focus::FocusRing, glyphs::Glyphs, hit::HitMap, theme::Theme};
use ratatui::layout::Rect;
use std::collections::HashMap;
use std::time::Instant;

pub struct Ui<'t> {
    pub theme: &'t Theme,
    pub glyphs: &'t Glyphs,
    pub focus: FocusRing,
    pub hits: HitMap,
    /// Hover blend per id, 0.0..=1.0 (owner updates via `anim::Blend`).
    pub hover: HashMap<String, f32>,
    pub now: Instant,
    pub start: Instant,
    /// Reduce motion: no spinner/hover blend.
    pub still: bool,
}

#[derive(Clone, Copy, Debug, Default)]
pub struct Response {
    pub focused: bool,
    pub hovered: bool,
    pub rect: Rect,
}

impl<'t> Ui<'t> {
    pub fn new(theme: &'t Theme, glyphs: &'t Glyphs) -> Self {
        let now = Instant::now();
        Self {
            theme,
            glyphs,
            focus: FocusRing::new(),
            hits: HitMap::default(),
            hover: HashMap::new(),
            now,
            start: now,
            still: false,
        }
    }
    pub fn begin_frame(&mut self) {
        self.focus.begin_frame();
        self.hits.clear();
    }
    pub fn end_frame(&mut self) {
        self.focus.end_frame();
    }
    pub fn hover_of(&self, id: &str) -> f32 {
        if self.still {
            0.0
        } else {
            self.hover.get(id).copied().unwrap_or(0.0)
        }
    }
    pub fn spinner(&self) -> &'static str {
        let g = self.glyphs.spinner;
        if self.still {
            return g[0];
        }
        g[anim::frame(self.now, self.start, g.len())]
    }
    /// Register id as a focus stop and hit rect; returns the response.
    pub fn stop(&mut self, id: &str, rect: Rect) -> Response {
        let focused = self.focus.register(id);
        self.hits.add(rect, id, crate::hit::Part::Body);
        Response { focused, hovered: self.hover_of(id) > 0.5, rect }
    }
}
