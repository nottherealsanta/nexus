//! Mouse hit rectangles. The same rect drawn is the rect registered (plan §13).
use ratatui::layout::Rect;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Part {
    Body,
    /// A named sub-part, e.g. "close", "up", "down", "segment:2".
    Named(String),
}

#[derive(Default)]
pub struct HitMap {
    hits: Vec<(Rect, String, Part)>,
}
impl HitMap {
    pub fn clear(&mut self) {
        self.hits.clear();
    }
    pub fn add(&mut self, rect: Rect, id: &str, part: Part) {
        if rect.width > 0 && rect.height > 0 && self.hits.len() < 4096 {
            self.hits.push((rect, id.to_string(), part));
        }
    }
    /// Later registrations win (overlays are drawn after the page).
    pub fn at(&self, x: u16, y: u16) -> Option<(&str, &Part)> {
        self.hits
            .iter()
            .rev()
            .find(|(r, _, _)| x >= r.x && x < r.x + r.width && y >= r.y && y < r.y + r.height)
            .map(|(_, id, part)| (id.as_str(), part))
    }
    pub fn rect_of(&self, id: &str) -> Option<Rect> {
        self.hits.iter().rev().find(|(_, i, p)| i == id && *p == Part::Body).map(|(r, _, _)| *r)
    }
    pub fn len(&self) -> usize {
        self.hits.len()
    }
    pub fn is_empty(&self) -> bool {
        self.hits.is_empty()
    }
}
