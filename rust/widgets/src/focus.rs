//! Focus ring (plan §6): stable string ids, regions with memory, overlay traps
//! that restore focus, one Esc ladder, and type-ahead.
use std::collections::HashMap;
use std::time::{Duration, Instant};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Region {
    Composer,
    Transcript,
    Sessions,
    Details,
    Overlay,
}

#[derive(Default)]
pub struct FocusRing {
    current: Option<String>,
    order: Vec<String>,
    prev_order: Vec<String>,
    memory: HashMap<Region, String>,
    region: Option<Region>,
    overlays: Vec<Option<String>>,
}

impl FocusRing {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn begin_frame(&mut self) {
        self.prev_order = std::mem::take(&mut self.order);
    }
    /// Register a focus stop in draw order; true when it holds focus.
    pub fn register(&mut self, id: &str) -> bool {
        if self.order.len() < 4096 {
            self.order.push(id.to_string());
        }
        self.current.as_deref() == Some(id)
    }
    /// Repair focus when the focused id vanished: nearest surviving neighbour by
    /// previous index, else the first stop (plan §6.5).
    pub fn end_frame(&mut self) {
        let Some(cur) = self.current.clone() else {
            self.current = self.order.first().cloned();
            return;
        };
        if self.order.contains(&cur) {
            return;
        }
        if self.order.is_empty() {
            return;
        }
        let at = self.prev_order.iter().position(|i| *i == cur).unwrap_or(0);
        let next = self.prev_order.iter().skip(at + 1).find(|i| self.order.contains(i));
        let before = self.prev_order.iter().take(at).rev().find(|i| self.order.contains(i));
        self.current = next.or(before).cloned().or_else(|| self.order.first().cloned());
    }
    pub fn current(&self) -> Option<&str> {
        self.current.as_deref()
    }
    pub fn is(&self, id: &str) -> bool {
        self.current.as_deref() == Some(id)
    }
    pub fn set(&mut self, id: &str) {
        self.current = Some(id.to_string());
        if let Some(r) = self.region {
            self.memory.insert(r, id.to_string());
        }
    }
    pub fn order(&self) -> &[String] {
        &self.order
    }
    fn index(&self) -> Option<usize> {
        let cur = self.current.as_deref()?;
        self.order.iter().position(|i| i == cur)
    }
    pub fn move_by(&mut self, delta: i32) {
        if self.order.is_empty() {
            return;
        }
        let n = self.order.len() as i32;
        let i = self.index().map(|i| i as i32).unwrap_or(if delta >= 0 { -1 } else { n });
        let j = (i + delta).clamp(0, n - 1) as usize;
        let id = self.order[j].clone();
        self.set(&id);
    }
    /// Tab-style movement that wraps.
    pub fn cycle(&mut self, delta: i32) {
        if self.order.is_empty() {
            return;
        }
        let n = self.order.len() as i32;
        let i = self.index().map(|i| i as i32).unwrap_or(-1);
        let j = (i + delta).rem_euclid(n) as usize;
        let id = self.order[j].clone();
        self.set(&id);
    }
    pub fn first(&mut self) {
        if let Some(id) = self.order.first().cloned() {
            self.set(&id);
        }
    }
    pub fn last(&mut self) {
        if let Some(id) = self.order.last().cloned() {
            self.set(&id);
        }
    }
    pub fn region(&self) -> Option<Region> {
        self.region
    }
    pub fn enter_region(&mut self, region: Region) {
        self.region = Some(region);
        if let Some(id) = self.memory.get(&region).cloned() {
            self.current = Some(id);
        }
    }
    pub fn push_overlay(&mut self) {
        self.overlays.push(self.current.clone());
        self.region = Some(Region::Overlay);
    }
    /// Close the top overlay and restore focus to its opener.
    pub fn pop_overlay(&mut self) {
        if let Some(opener) = self.overlays.pop() {
            self.current = opener;
        }
        if self.overlays.is_empty() {
            self.region = None;
        }
    }
    pub fn overlay_depth(&self) -> usize {
        self.overlays.len()
    }
}

/// What is open right now, innermost first. Input to the Esc ladder.
#[derive(Clone, Copy, Default, Debug)]
pub struct EscapeState {
    pub popup_open: bool,
    pub search_nonempty: bool,
    pub search_focused: bool,
    pub inner_focused: bool,
    pub overlay_open: bool,
    pub in_sidebar: bool,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Escape {
    ClosePopup,
    ClearSearch,
    LeaveSearch,
    LeaveInner,
    CloseOverlay,
    SidebarToComposer,
    Composer,
}
/// Single source of truth for Esc (plan §6.4): least destructive first.
pub fn escape(s: EscapeState) -> Escape {
    if s.popup_open {
        Escape::ClosePopup
    } else if s.search_nonempty {
        Escape::ClearSearch
    } else if s.search_focused {
        Escape::LeaveSearch
    } else if s.inner_focused {
        Escape::LeaveInner
    } else if s.overlay_open {
        Escape::CloseOverlay
    } else if s.in_sidebar {
        Escape::SidebarToComposer
    } else {
        Escape::Composer
    }
}

/// Type-ahead buffer that resets after 800 ms.
#[derive(Default)]
pub struct Typeahead {
    buf: String,
    at: Option<Instant>,
}
impl Typeahead {
    pub fn push(&mut self, c: char, now: Instant) -> &str {
        if self.at.map(|t| now.saturating_duration_since(t) > Duration::from_millis(800)).unwrap_or(true) {
            self.buf.clear();
        }
        self.at = Some(now);
        self.buf.extend(c.to_lowercase());
        &self.buf
    }
    /// Index of the first label at or after `from` (wrapping) that starts with the buffer.
    pub fn find(&self, labels: &[&str], from: usize) -> Option<usize> {
        let n = labels.len();
        (0..n).map(|k| (from + k) % n).find(|&i| labels[i].to_lowercase().starts_with(&self.buf))
    }
}
