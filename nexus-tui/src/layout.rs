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
/// The medium breakpoint of D17. The right sidebar starts closed at every width.
pub const RIGHT_OPEN_FROM: u16 = 120;
/// From this width the sidebars are 34 / 36 cells and the reading column is
/// centred (D17); below it they are 28 / 30.
pub const WIDE_FROM: u16 = 160;
pub const MIN_CENTRE: u16 = 40;
/// The project rail, always left of the sidebar: initials per project.
/// Cells of the header's Manager button (a space each side of the word).
pub const MANAGER_BUTTON: u16 = 10;

pub const RAIL_WIDTH: u16 = 5;
/// Below this many columns there is no room for the rail beside the page.
pub const RAIL_FROM: u16 = 70;
/// The left sidebar's drag bounds; a drag below `RAIL_SNAP` closes it, leaving
/// the rail.
pub const LEFT_MIN: u16 = 20;
pub const LEFT_MAX: u16 = 60;
pub const RAIL_SNAP: u16 = 14;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Flags {
    pub left_open: bool,
    pub right_open: bool,
    pub drawer: Option<Side>,
    /// Stored full width of the left sidebar; 0 means the default.
    pub left_width: u16,
    /// A subagent is viewed: reserve a four-cell root-back strip at the centre's left.
    pub session_child: bool,
}

/// Width of the docked left sidebar, bounded so the centre keeps `MIN_CENTRE`.
pub fn left_width(total: u16, flags: Flags) -> u16 {
    let right = if flags.right_open {
        right_width(total)
    } else {
        0
    };
    let room = total
        .saturating_sub(MIN_CENTRE + right + RAIL_WIDTH)
        .max(LEFT_MIN);
    let want = if flags.left_width == 0 {
        sidebar_width(total)
    } else {
        flags.left_width
    };
    want.clamp(LEFT_MIN, LEFT_MAX.min(room))
}

/// Heights the centre column needs, given its width.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Heights {
    /// The viewed session's title row, first in the page: 0 or 1.
    pub heading: u16,
    pub banner: u16,
    pub waiting: u16,
    pub composer: u16,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Regions {
    pub area: Rect,
    pub narrow: bool,
    pub header: Rect,
    /// The project rail: there whether or not the sidebar is open.
    pub rail: Option<Rect>,
    /// Docked sidebars (wide layouts only).
    pub left: Option<Rect>,
    pub right: Option<Rect>,
    /// The focused group's body while its active tab is a file or diff (set
    /// by the renderer from the layout tree; `None` otherwise).
    pub panel: Option<Rect>,
    /// A drawer drawn over the centre (narrow layouts), with its side.
    pub drawer: Option<(Side, Rect)>,
    /// The centre content, right of the root-back strip when one is reserved:
    /// the area the layout tree (groups of tabs) fills.
    pub centre: Rect,
    /// The four-cell full-height root-back strip, subagent views only.
    pub back: Option<Rect>,
    pub heading: Rect,
    pub banner: Rect,
    pub timeline: Rect,
    pub waiting: Rect,
    pub composer: Rect,
    /// The body of the group showing the live terminal screen (renderer-set).
    pub terminal: Option<Rect>,
    // Header anchors: leading toggle, trailing toggle, notification bell, title.
    pub toggle_left: Rect,
    pub toggle_right: Rect,
    pub bell: Rect,
    /// The header's Manager button, left of the right toggle (and the bell).
    pub manager: Rect,
    pub title: Rect,
}

/// The D17 breakpoints, by terminal width.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Breakpoint {
    /// 160 columns and up: both sidebars docked (34 / 36), the reading column
    /// capped and centred.
    Wide,
    /// 120-159: both sidebars docked (28 / 30).
    Medium,
    /// 100-119: the left sidebar docked.
    Compact,
    /// Under 100: both sidebars are drawers over the page with the scrim; the
    /// header keeps the toggles and a left-truncated crumb.
    Drawers,
}

impl Breakpoint {
    pub fn of(width: u16) -> Self {
        match width {
            w if w >= WIDE_FROM => Self::Wide,
            w if w >= RIGHT_OPEN_FROM => Self::Medium,
            w if w >= NARROW_BELOW => Self::Compact,
            _ => Self::Drawers,
        }
    }

    pub fn drawers(self) -> bool {
        self == Self::Drawers
    }
}

/// Width of a drawer: the wide sidebars' 34 / 36 cells, within 4 cells of the
/// page and never under 22.
pub fn drawer_width(side: Side, total: u16) -> u16 {
    let want = match side {
        Side::Left => 34,
        Side::Right => 36,
    };
    want.min(total.saturating_sub(4)).max(22.min(total))
}

/// Default width of the docked left sidebar (DESIGN_PLAN.md D17): 34 cells from
/// 160 columns, 28 below.
pub fn sidebar_width(total: u16) -> u16 {
    if total >= WIDE_FROM {
        34
    } else {
        28
    }
}

/// Width of the docked right sidebar: 36 cells from 160 columns, 30 below.
pub fn right_width(total: u16) -> u16 {
    if total >= WIDE_FROM {
        36
    } else {
        30
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

        let (mut x, mut w) = (area.x, area.width);
        let rail = (area.width >= RAIL_FROM).then(|| r(x, body_y, RAIL_WIDTH, body_h));
        if rail.is_some() {
            x += RAIL_WIDTH;
            w -= RAIL_WIDTH;
        }
        let page_x = x;
        let mut left = None;
        let mut right = None;
        if !narrow {
            if flags.left_open {
                let lw = left_width(area.width, flags);
                left = Some(r(x, body_y, lw, body_h));
                x += lw;
                w -= lw;
            }
            if flags.right_open {
                let rw = right_width(area.width);
                w -= rw;
                right = Some(r(x + w, body_y, rw, body_h));
            }
        }
        let centre = r(x, body_y, w, body_h);
        let back = flags
            .session_child
            .then(|| r(centre.x, centre.y, 4.min(centre.width), centre.height));
        let centre = match back {
            Some(strip) => r(
                strip.right(),
                centre.y,
                centre.width.saturating_sub(strip.width),
                centre.height,
            ),
            None => centre,
        };

        let drawer = flags.drawer.filter(|_| narrow).map(|side| {
            let dw = drawer_width(side, area.width - (page_x - area.x));
            let rect = match side {
                Side::Left => r(page_x, body_y, dw, body_h),
                Side::Right => r(area.x + area.width - dw, body_y, dw, body_h),
            };
            (side, rect)
        });

        // One group fills the centre by default: its strip is the first row.
        let body = group_body(centre);
        let (heading, banner, timeline, waiting, composer) =
            session_rects(body, heights(body.width));

        // Header: [ toggle ] [ title ............ ] [ bell ] [ toggle ]
        let toggle_width = if rail.is_some() { RAIL_WIDTH } else { 3 };
        let toggle_left = r(
            header.x,
            header.y,
            toggle_width.min(header.width),
            header.height,
        );
        let toggle_right = r(
            header.x + header.width.saturating_sub(4),
            header.y,
            4.min(header.width),
            header.height,
        );
        // [ title ] [ bell ] [ Manager ] [ toggle ]: the Manager button sits against the toggle, and the bell
        // (only with unread notices) moves left of it. Too narrow to fit beside a title: no button.
        let wide = header.width >= 60;
        let manager = r(
            toggle_right.x.saturating_sub(MANAGER_BUTTON),
            header.y,
            if wide { MANAGER_BUTTON } else { 0 },
            header.height,
        );
        let bell_right = if wide { manager.x } else { toggle_right.x };
        let bell = r(
            bell_right.saturating_sub(6),
            header.y,
            6.min(bell_right.saturating_sub(header.x)),
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
            rail,
            left,
            right,
            panel: None,
            drawer,
            centre,
            back,
            heading,
            banner,
            timeline,
            waiting,
            composer,
            terminal: None,
            toggle_left,
            toggle_right,
            bell,
            manager,
            title,
        }
    }
}

/// A group's body: its rect below the one-row tab strip.
pub fn group_body(group: Rect) -> Rect {
    let strip = group.height.min(1);
    r(group.x, group.y + strip, group.width, group.height - strip)
}

/// The session page inside `body`: heading, banner, timeline, waiting card and
/// composer, clamped so the timeline keeps at least three rows.
pub fn session_rects(body: Rect, mut h: Heights) -> (Rect, Rect, Rect, Rect, Rect) {
    let max_extra = body.height.saturating_sub(3);
    let mut left_rows = max_extra;
    for v in [
        &mut h.composer,
        &mut h.heading,
        &mut h.banner,
        &mut h.waiting,
    ] {
        *v = (*v).min(left_rows);
        left_rows -= *v;
    }
    let heading = r(body.x, body.y, body.width, h.heading);
    let banner = r(body.x, body.y + h.heading, body.width, h.banner);
    let composer = r(
        body.x,
        body.y + body.height - h.composer,
        body.width,
        h.composer,
    );
    let waiting = r(body.x, composer.y - h.waiting, body.width, h.waiting);
    let timeline_y = banner.y + banner.height;
    let timeline = r(
        body.x,
        timeline_y,
        body.width,
        waiting.y.saturating_sub(timeline_y),
    );
    (heading, banner, timeline, waiting, composer)
}

impl Regions {
    /// Lay the session page into `body` (the focused group's body), or take it
    /// away (`None`) when the focused group shows a file, diff or terminal.
    pub fn retarget_session(&mut self, body: Option<Rect>, heights: impl Fn(u16) -> Heights) {
        let (heading, banner, timeline, waiting, composer) = match body {
            Some(b) => session_rects(b, heights(b.width)),
            None => {
                let none = r(self.centre.x, self.centre.y, 0, 0);
                (none, none, none, none, none)
            }
        };
        self.heading = heading;
        self.banner = banner;
        self.timeline = timeline;
        self.waiting = waiting;
        self.composer = composer;
    }
}

pub fn contains(rect: Rect, x: u16, y: u16) -> bool {
    x >= rect.x && x < rect.x + rect.width && y >= rect.y && y < rect.y + rect.height
}

/// Clickable rects registered during a draw. Later registrations are on top.
#[derive(Debug, Default, Clone, PartialEq)]
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
            ..Flags::default()
        }
    }
    fn heights(_: u16) -> Heights {
        Heights {
            banner: 1,
            waiting: 0,
            composer: 5,
            ..Default::default()
        }
    }

    #[test]
    fn wide_layout_docks_both_sidebars() {
        let g = Regions::compute(Rect::new(0, 0, 120, 36), flags(true, true), heights);
        assert!(!g.narrow);
        assert_eq!(g.left.unwrap().width, 28);
        assert_eq!(g.right.unwrap().width, 30);
        assert_eq!(g.centre.width, 120 - 58 - RAIL_WIDTH);
        assert_eq!(g.header.height + g.centre.height, 36);
        assert_eq!(
            g.composer.y + g.composer.height,
            g.centre.y + g.centre.height
        );
    }

    #[test]
    fn the_centre_is_one_group_whose_strip_is_its_first_row() {
        let g = Regions::compute(Rect::new(0, 0, 160, 40), flags(true, true), heights);
        let body = group_body(g.centre);
        assert_eq!(body.y, g.centre.y + 1);
        assert_eq!(body.height, g.centre.height - 1);
        assert_eq!(g.heading.y, body.y, "the page starts under the strip");
        assert_eq!(g.composer.bottom(), g.centre.bottom());
        assert!(g.panel.is_none() && g.terminal.is_none(), "no dock or modal");
    }

    #[test]
    fn the_session_page_retargets_into_another_group_or_goes_away() {
        let mut g = Regions::compute(Rect::new(0, 0, 160, 40), flags(true, true), heights);
        let body = Rect::new(60, 5, 50, 20);
        g.retarget_session(Some(body), heights);
        assert_eq!(g.composer.bottom(), body.bottom());
        assert_eq!((g.timeline.x, g.timeline.width), (60, 50));
        g.retarget_session(None, heights);
        assert_eq!((g.timeline.width, g.timeline.height), (0, 0));
        assert_eq!((g.composer.width, g.composer.height), (0, 0));
    }

    #[test]
    fn sidebar_widths_follow_the_breakpoints() {
        // D17: left 34 / right 36 from 160 columns, 28 / 30 below.
        for (total, l, r) in [(200, 34, 36), (160, 34, 36), (159, 28, 30), (120, 28, 30)] {
            let g = Regions::compute(Rect::new(0, 0, total, 40), flags(true, true), heights);
            assert_eq!(g.left.unwrap().width, l, "left at {total}");
            assert_eq!(g.right.unwrap().width, r, "right at {total}");
        }
        // a saved width still wins
        let mut f = flags(true, true);
        f.left_width = 40;
        let g = Regions::compute(Rect::new(0, 0, 200, 40), f, heights);
        assert_eq!(g.left.unwrap().width, 40);
    }

    #[test]
    fn left_sidebar_resizes_and_collapses_to_a_rail() {
        let mut f = flags(true, true);
        f.left_width = 44;
        let g = Regions::compute(Rect::new(0, 0, 160, 40), f, heights);
        assert_eq!(g.left.unwrap().width, 44);
        f.left_width = 200;
        let g = Regions::compute(Rect::new(0, 0, 120, 40), f, heights);
        assert!(g.centre.width >= MIN_CENTRE);
        f.left_open = false;
        let g = Regions::compute(Rect::new(0, 0, 120, 40), f, heights);
        assert!(g.left.is_none());
        assert_eq!(g.rail.unwrap().width, RAIL_WIDTH);
        assert_eq!(g.centre.x, RAIL_WIDTH);
    }

    #[test]
    fn the_d17_breakpoint_table() {
        // (width, class, drawers, right sidebar open by default, left, right)
        let table = [
            (200, Breakpoint::Wide, false, true, 34, 36),
            (160, Breakpoint::Wide, false, true, 34, 36),
            (159, Breakpoint::Medium, false, true, 28, 30),
            (120, Breakpoint::Medium, false, true, 28, 30),
            (119, Breakpoint::Compact, false, false, 28, 30),
            (100, Breakpoint::Compact, false, false, 28, 30),
            (99, Breakpoint::Drawers, true, false, 28, 30),
            (80, Breakpoint::Drawers, true, false, 28, 30),
        ];
        for (width, class, drawers, right, left_w, right_w) in table {
            assert_eq!(Breakpoint::of(width), class, "{width}");
            assert_eq!(class.drawers(), drawers, "{width}");
            let _ = right;
            assert_eq!(Regions::is_narrow(width), drawers, "{width}");
            assert_eq!(sidebar_width(width), left_w, "{width}");
            assert_eq!(right_width(width), right_w, "{width}");
        }
    }

    #[test]
    fn docked_sidebars_by_breakpoint() {
        let docked = |w: u16, right: bool| {
            Regions::compute(Rect::new(0, 0, w, 40), flags(true, right), heights)
        };
        let g = docked(120, true);
        assert_eq!((g.left.unwrap().width, g.right.unwrap().width), (28, 30));
        let g = docked(160, true);
        assert_eq!((g.left.unwrap().width, g.right.unwrap().width), (34, 36));
        // 100-119: the right sidebar can still be docked on request; the
        // centre keeps its minimum
        let g = docked(100, true);
        assert!(g.centre.width >= MIN_CENTRE && g.right.is_some());
        let g = docked(100, false);
        assert!(g.right.is_none() && g.left.is_some());
    }

    #[test]
    fn drawers_are_34_and_36_cells_and_stay_inside_the_page() {
        for (side, w) in [(Side::Left, 34), (Side::Right, 36)] {
            let mut f = flags(true, true);
            f.drawer = Some(side);
            let g = Regions::compute(Rect::new(0, 0, 80, 24), f, heights);
            let (s, rect) = g.drawer.unwrap();
            assert_eq!((s, rect.width), (side, w));
            assert_eq!(rect.height, 23, "from under the header to the bottom");
            match side {
                Side::Left => assert_eq!(rect.x, RAIL_WIDTH),
                Side::Right => assert_eq!(rect.right(), 80),
            }
        }
        // a very small terminal keeps a gap of 4 cells for the scrim
        let mut f = flags(true, true);
        f.drawer = Some(Side::Right);
        let g = Regions::compute(Rect::new(0, 0, 40, 24), f, heights);
        assert_eq!(g.drawer.unwrap().1.width, 36);
        let g = Regions::compute(Rect::new(0, 0, 30, 24), f, heights);
        assert_eq!(g.drawer.unwrap().1.width, 26);
        // never under 22 cells, nor wider than the terminal
        let g = Regions::compute(Rect::new(0, 0, 24, 24), f, heights);
        assert_eq!(g.drawer.unwrap().1.width, 22);
        // a drawer is a narrow-layout feature: docked layouts ignore the flag
        let g = Regions::compute(Rect::new(0, 0, 120, 24), f, heights);
        assert!(g.drawer.is_none());
    }

    #[test]
    fn narrow_layout_has_one_pane_and_drawers() {
        let mut f = flags(true, true);
        f.drawer = Some(Side::Left);
        let g = Regions::compute(Rect::new(0, 0, 80, 24), f, heights);
        assert!(g.narrow && g.left.is_none() && g.right.is_none());
        assert_eq!(g.centre.width, 80 - RAIL_WIDTH);
        let (side, rect) = g.drawer.unwrap();
        assert_eq!((side, rect.x), (Side::Left, RAIL_WIDTH));
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
            ..Default::default()
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
