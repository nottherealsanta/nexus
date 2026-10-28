//! Common geometry for drawing and hit-testing.
//!
//! `Regions::compute` is a pure function of the terminal size and a few view
//! flags, so the renderer and the input handler agree on where things are. The
//! renderer registers every clickable rect in a `HitMap` as it draws it; the map
//! is rebuilt on every frame, so a resize never leaves a stale target.
//! (HitMap adapted from old Nexus `rust/widgets/src/hit.rs`: "the same rect
//! drawn is the rect registered".)

use ratatui::layout::Rect;

use crate::ui::{Side, Target};

/// Below this many columns the sidebars become drawers (INTERFACE_PLAN.md Scale).
pub const NARROW_BELOW: u16 = 100;
pub const MIN_CENTRE: u16 = 40;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Flags {
    pub left_open: bool,
    pub right_open: bool,
    pub drawer: Option<Side>,
}

/// Heights the centre column needs, given its width.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Heights {
    pub banner: u16,
    pub waiting: u16,
    pub composer: u16,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Regions {
    pub area: Rect,
    pub narrow: bool,
    pub header: Rect,
    /// Docked sidebars (wide layouts only).
    pub left: Option<Rect>,
    pub right: Option<Rect>,
    /// A drawer drawn over the centre (narrow layouts), with its side.
    pub drawer: Option<(Side, Rect)>,
    pub centre: Rect,
    pub banner: Rect,
    pub timeline: Rect,
    pub waiting: Rect,
    pub composer: Rect,
    // Header anchors: leading toggle, trailing toggle, notification bell, title.
    pub toggle_left: Rect,
    pub toggle_right: Rect,
    pub bell: Rect,
    pub title: Rect,
}

pub fn sidebar_width(total: u16) -> u16 {
    if total >= 150 {
        32
    } else {
        28
    }
}

fn r(x: u16, y: u16, w: u16, h: u16) -> Rect {
    Rect::new(x, y, w, h)
}

impl Regions {
    pub fn is_narrow(width: u16) -> bool {
        width < NARROW_BELOW
    }

    /// `heights` receives the centre column's width and returns the rows the
    /// banner, waiting card and composer want; they are clamped so the timeline
    /// keeps at least three rows.
    pub fn compute(area: Rect, flags: Flags, heights: impl Fn(u16) -> Heights) -> Regions {
        let narrow = Self::is_narrow(area.width);
        let header = r(area.x, area.y, area.width, area.height.min(1));
        let body_y = area.y + header.height;
        let body_h = area.height.saturating_sub(header.height);
        let sw = sidebar_width(area.width);

        let (mut x, mut w) = (area.x, area.width);
        let mut left = None;
        let mut right = None;
        if !narrow {
            if flags.left_open {
                left = Some(r(x, body_y, sw, body_h));
                x += sw;
                w -= sw;
            }
            if flags.right_open {
                w -= sw;
                right = Some(r(x + w, body_y, sw, body_h));
            }
        }
        let centre = r(x, body_y, w, body_h);

        let drawer = flags.drawer.filter(|_| narrow).map(|side| {
            let dw = sw.min(area.width.saturating_sub(4)).max(22.min(area.width));
            let rect = match side {
                Side::Left => r(area.x, body_y, dw, body_h),
                Side::Right => r(area.x + area.width - dw, body_y, dw, body_h),
            };
            (side, rect)
        });

        let mut h = heights(centre.width);
        let max_extra = centre.height.saturating_sub(3);
        let mut left_rows = max_extra;
        for v in [&mut h.composer, &mut h.banner, &mut h.waiting] {
            *v = (*v).min(left_rows);
            left_rows -= *v;
        }
        let banner = r(centre.x, centre.y, centre.width, h.banner);
        let composer = r(
            centre.x,
            centre.y + centre.height - h.composer,
            centre.width,
            h.composer,
        );
        let waiting = r(centre.x, composer.y - h.waiting, centre.width, h.waiting);
        let timeline_y = banner.y + banner.height;
        let timeline = r(
            centre.x,
            timeline_y,
            centre.width,
            waiting.y.saturating_sub(timeline_y),
        );

        // Header: [ toggle ] [ title ............ ] [ bell ] [ toggle ]
        let toggle_left = r(header.x, header.y, 4.min(header.width), header.height);
        let toggle_right = r(
            header.x + header.width.saturating_sub(4),
            header.y,
            4.min(header.width),
            header.height,
        );
        let bell = r(
            toggle_right.x.saturating_sub(6),
            header.y,
            6.min(toggle_right.x.saturating_sub(header.x)),
            header.height,
        );
        let title = r(
            toggle_left.x + toggle_left.width,
            header.y,
            bell.x.saturating_sub(toggle_left.x + toggle_left.width),
            header.height,
        );

        Regions {
            area,
            narrow,
            header,
            left,
            right,
            drawer,
            centre,
            banner,
            timeline,
            waiting,
            composer,
            toggle_left,
            toggle_right,
            bell,
            title,
        }
    }
}

pub fn contains(rect: Rect, x: u16, y: u16) -> bool {
    x >= rect.x && x < rect.x + rect.width && y >= rect.y && y < rect.y + rect.height
}

/// Clickable rects registered during a draw. Later registrations are on top.
#[derive(Debug, Default, Clone)]
pub struct HitMap {
    hits: Vec<(Rect, Target)>,
}

impl HitMap {
    pub fn clear(&mut self) {
        self.hits.clear();
    }

    pub fn add(&mut self, rect: Rect, target: Target) {
        if rect.width > 0 && rect.height > 0 && self.hits.len() < 4096 {
            self.hits.push((rect, target));
        }
    }

    pub fn at(&self, x: u16, y: u16) -> Option<&Target> {
        self.hits
            .iter()
            .rev()
            .find(|(rect, _)| contains(*rect, x, y))
            .map(|(_, t)| t)
    }

    pub fn rect_of(&self, target: &Target) -> Option<Rect> {
        self.hits
            .iter()
            .rev()
            .find(|(_, t)| t == target)
            .map(|(r, _)| *r)
    }

    pub fn len(&self) -> usize {
        self.hits.len()
    }

    pub fn is_empty(&self) -> bool {
        self.hits.is_empty()
    }

    pub fn all(&self) -> &[(Rect, Target)] {
        &self.hits
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn flags(l: bool, r: bool) -> Flags {
        Flags {
            left_open: l,
            right_open: r,
            drawer: None,
        }
    }
    fn heights(_: u16) -> Heights {
        Heights {
            banner: 1,
            waiting: 0,
            composer: 5,
        }
    }

    #[test]
    fn wide_layout_docks_both_sidebars() {
        let g = Regions::compute(Rect::new(0, 0, 120, 36), flags(true, true), heights);
        assert!(!g.narrow);
        assert_eq!(g.left.unwrap().width, 28);
        assert_eq!(g.right.unwrap().width, 28);
        assert_eq!(g.centre.width, 120 - 56);
        assert_eq!(g.header.height + g.centre.height, 36);
        assert_eq!(
            g.composer.y + g.composer.height,
            g.centre.y + g.centre.height
        );
    }

    #[test]
    fn narrow_layout_has_one_pane_and_drawers() {
        let mut f = flags(true, true);
        f.drawer = Some(Side::Left);
        let g = Regions::compute(Rect::new(0, 0, 80, 24), f, heights);
        assert!(g.narrow && g.left.is_none() && g.right.is_none());
        assert_eq!(g.centre.width, 80);
        let (side, rect) = g.drawer.unwrap();
        assert_eq!((side, rect.x), (Side::Left, 0));
        // anchors stay put
        assert_eq!(g.toggle_left.x, 0);
        assert_eq!(g.toggle_right.x + g.toggle_right.width, 80);
    }

    #[test]
    fn tiny_heights_keep_a_timeline() {
        let g = Regions::compute(Rect::new(0, 0, 80, 10), flags(false, false), |_| Heights {
            banner: 2,
            waiting: 6,
            composer: 8,
        });
        assert!(g.timeline.height >= 3);
        assert!(g.composer.y >= g.timeline.y + g.timeline.height);
    }

    #[test]
    fn hit_map_prefers_the_topmost_and_clears() {
        let mut m = HitMap::default();
        m.add(Rect::new(0, 0, 10, 10), Target::Timeline);
        m.add(Rect::new(2, 2, 3, 3), Target::Swallow);
        assert_eq!(m.at(3, 3), Some(&Target::Swallow));
        assert_eq!(m.at(8, 8), Some(&Target::Timeline));
        assert_eq!(m.at(20, 20), None);
        m.clear();
        assert!(m.is_empty());
    }
}
