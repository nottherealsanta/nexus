//! The tab layout tree (plan 45, docs/layout.md): pure, no drawing.
//!
//! The centre is a tree. A *split* has a direction, children and ratios; a
//! leaf is a *group* with an id, ordered tabs and an active tab. One group is
//! focused. Everything here is a function of the tree and a rectangle, so the
//! renderer, the input handler and the tests agree on geometry; the app
//! decides what a tab shows.
//!
//! Cells: every split puts a one-cell gutter between its children (the divider
//! a drag grabs). A group is its rect: row 0 is the tab strip, the rest the
//! body. A group never gets smaller than [`MIN_W`] x [`MIN_H`]; an operation
//! that would do that is refused with [`LayoutError::TooSmall`] and changes
//! nothing.

use std::collections::BTreeMap;

use ratatui::layout::Rect;
use serde::{Deserialize, Serialize};

/// The smallest pane (strip included), in cells.
pub const MIN_W: u16 = 20;
pub const MIN_H: u16 = 5;

pub type GroupId = u32;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Kind {
    Session,
    File,
    Diff,
    Terminal,
    Browser,
}

/// A tab is its kind and resource id: the same pair in two groups is the same
/// file, session or terminal.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub struct Tab {
    pub kind: Kind,
    pub id: String,
}

impl Tab {
    pub fn new(kind: Kind, id: impl Into<String>) -> Self {
        Self {
            kind,
            id: id.into(),
        }
    }
    pub fn session(id: impl Into<String>) -> Self {
        Self::new(Kind::Session, id)
    }
    pub fn file(path: impl Into<String>) -> Self {
        Self::new(Kind::File, path)
    }
    pub fn terminal(id: impl Into<String>) -> Self {
        Self::new(Kind::Terminal, id)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Dir {
    /// Children side by side.
    Row,
    /// Children stacked.
    Col,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Side {
    Left,
    Right,
    Up,
    Down,
}

impl Side {
    pub fn dir(self) -> Dir {
        match self {
            Side::Left | Side::Right => Dir::Row,
            Side::Up | Side::Down => Dir::Col,
        }
    }
    fn before(self) -> bool {
        matches!(self, Side::Left | Side::Up)
    }
    pub fn name(self) -> &'static str {
        match self {
            Side::Left => "left",
            Side::Right => "right",
            Side::Up => "above",
            Side::Down => "below",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LayoutError {
    /// The result would leave a pane under the minimum.
    TooSmall,
    /// No group that way.
    NoNeighbour,
    /// Nothing to do (no such tab or group).
    Nothing,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Group {
    pub id: GroupId,
    pub tabs: Vec<Tab>,
    pub active: usize,
}

impl Group {
    fn new(id: GroupId, tabs: Vec<Tab>) -> Self {
        Group {
            id,
            tabs,
            active: 0,
        }
    }
    pub fn active_tab(&self) -> Option<&Tab> {
        self.tabs.get(self.active)
    }
    pub fn position(&self, tab: &Tab) -> Option<usize> {
        self.tabs.iter().position(|t| t == tab)
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "node", rename_all = "lowercase")]
pub enum Node {
    Group(Group),
    Split {
        dir: Dir,
        children: Vec<Node>,
        ratios: Vec<f32>,
    },
}

/// A divider between two children of a split, as a drag target.
#[derive(Debug, Clone, PartialEq)]
pub struct Divider {
    /// Child indices from the root to the split.
    pub path: Vec<usize>,
    /// The divider sits between child `index` and `index + 1`.
    pub index: usize,
    pub dir: Dir,
    pub rect: Rect,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Zone {
    Left,
    Right,
    Top,
    Bottom,
    Centre,
}

/// Which drop zone of a group body `(x, y)` is in: the outer quarter on each
/// side splits, the middle moves into the group.
pub fn zone_at(body: Rect, x: u16, y: u16) -> Zone {
    if body.width == 0 || body.height == 0 {
        return Zone::Centre;
    }
    let fx = (x.saturating_sub(body.x)) as f32 / body.width as f32;
    let fy = (y.saturating_sub(body.y)) as f32 / body.height as f32;
    // The nearest edge wins when two bands overlap at a corner.
    let (dl, dr, dt, db) = (fx, 1.0 - fx, fy, 1.0 - fy);
    let nearest = dl.min(dr).min(dt).min(db);
    if nearest >= 0.25 {
        Zone::Centre
    } else if nearest == dl {
        Zone::Left
    } else if nearest == dr {
        Zone::Right
    } else if nearest == dt {
        Zone::Top
    } else {
        Zone::Bottom
    }
}

pub fn zone_side(zone: Zone) -> Option<Side> {
    match zone {
        Zone::Left => Some(Side::Left),
        Zone::Right => Some(Side::Right),
        Zone::Top => Some(Side::Up),
        Zone::Bottom => Some(Side::Down),
        Zone::Centre => None,
    }
}

/// The rect a drop zone highlights inside a group body.
pub fn zone_rect(body: Rect, zone: Zone) -> Rect {
    let (hw, hh) = (body.width / 2, body.height / 2);
    match zone {
        Zone::Left => Rect::new(body.x, body.y, hw, body.height),
        Zone::Right => Rect::new(body.x + body.width - hw, body.y, hw, body.height),
        Zone::Top => Rect::new(body.x, body.y, body.width, hh),
        Zone::Bottom => Rect::new(body.x, body.y + body.height - hh, body.width, hh),
        Zone::Centre => body,
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Layout {
    pub root: Node,
    pub focused: GroupId,
    next_id: GroupId,
}

impl Default for Layout {
    fn default() -> Self {
        Self::new()
    }
}

/// Cell lengths of `n` children in `total` cells (gutters already removed):
/// cumulative rounding, so the sizes always add up.
fn lengths(total: u16, ratios: &[f32]) -> Vec<u16> {
    let sum: f32 = ratios.iter().sum::<f32>().max(f32::EPSILON);
    let mut out = Vec::with_capacity(ratios.len());
    let (mut cum, mut prev) = (0.0f32, 0u16);
    for (i, r) in ratios.iter().enumerate() {
        cum += r / sum;
        let edge = if i + 1 == ratios.len() {
            total
        } else {
            ((cum * total as f32).round() as u16).clamp(prev, total)
        };
        out.push(edge - prev);
        prev = edge;
    }
    out
}

impl Node {
    fn first_group(&self) -> &Group {
        match self {
            Node::Group(g) => g,
            Node::Split { children, .. } => children[0].first_group(),
        }
    }

    fn groups<'a>(&'a self, out: &mut Vec<&'a Group>) {
        match self {
            Node::Group(g) => out.push(g),
            Node::Split { children, .. } => children.iter().for_each(|c| c.groups(out)),
        }
    }

    fn find_mut(&mut self, id: GroupId) -> Option<&mut Group> {
        match self {
            Node::Group(g) => (g.id == id).then_some(g),
            Node::Split { children, .. } => children.iter_mut().find_map(|c| c.find_mut(id)),
        }
    }

    fn path_to(&self, id: GroupId) -> Option<Vec<usize>> {
        match self {
            Node::Group(g) => (g.id == id).then(Vec::new),
            Node::Split { children, .. } => children.iter().enumerate().find_map(|(i, c)| {
                c.path_to(id).map(|mut p| {
                    p.insert(0, i);
                    p
                })
            }),
        }
    }

    fn at_mut(&mut self, path: &[usize]) -> Option<&mut Node> {
        match path.split_first() {
            None => Some(self),
            Some((i, rest)) => match self {
                Node::Split { children, .. } => children.get_mut(*i)?.at_mut(rest),
                Node::Group(_) => None,
            },
        }
    }

    /// Smallest length along `axis` this node can take.
    fn required(&self, axis: Dir) -> u16 {
        match self {
            Node::Group(_) => match axis {
                Dir::Row => MIN_W,
                Dir::Col => MIN_H,
            },
            Node::Split { dir, children, .. } => {
                let each = children.iter().map(|c| c.required(axis));
                if *dir == axis {
                    each.sum::<u16>() + (children.len() as u16).saturating_sub(1)
                } else {
                    each.max().unwrap_or(0)
                }
            }
        }
    }

    fn place(
        &self,
        area: Rect,
        out: &mut Vec<(GroupId, Rect)>,
        divs: &mut Vec<Divider>,
        path: &mut Vec<usize>,
    ) {
        match self {
            Node::Group(g) => out.push((g.id, area)),
            Node::Split {
                dir,
                children,
                ratios,
            } => {
                let n = children.len() as u16;
                let len = match dir {
                    Dir::Row => area.width,
                    Dir::Col => area.height,
                };
                let sizes = lengths(len.saturating_sub(n.saturating_sub(1)), ratios);
                let mut at = 0u16;
                for (i, (child, size)) in children.iter().zip(sizes).enumerate() {
                    let rect = match dir {
                        Dir::Row => Rect::new(area.x + at, area.y, size, area.height),
                        Dir::Col => Rect::new(area.x, area.y + at, area.width, size),
                    };
                    path.push(i);
                    child.place(rect, out, divs, path);
                    path.pop();
                    at += size;
                    if (i as u16) + 1 < n {
                        let rect = match dir {
                            Dir::Row => Rect::new(area.x + at, area.y, 1, area.height),
                            Dir::Col => Rect::new(area.x, area.y + at, area.width, 1),
                        };
                        divs.push(Divider {
                            path: path.clone(),
                            index: i,
                            dir: *dir,
                            rect,
                        });
                        at += 1;
                    }
                }
            }
        }
    }

    /// Merge a split into its parent when both go the same way, and replace a
    /// one-child split by the child.
    fn normalise(&mut self) {
        let Node::Split {
            dir,
            children,
            ratios,
        } = self
        else {
            return;
        };
        for c in children.iter_mut() {
            c.normalise();
        }
        let mut flat: Vec<Node> = Vec::new();
        let mut flat_r: Vec<f32> = Vec::new();
        for (child, r) in std::mem::take(children)
            .into_iter()
            .zip(ratios.iter().copied())
        {
            match child {
                Node::Split {
                    dir: d,
                    children: cc,
                    ratios: cr,
                } if d == *dir => {
                    let sum: f32 = cr.iter().sum::<f32>().max(f32::EPSILON);
                    for (c, x) in cc.into_iter().zip(cr) {
                        flat.push(c);
                        flat_r.push(r * x / sum);
                    }
                }
                other => {
                    flat.push(other);
                    flat_r.push(r);
                }
            }
        }
        *children = flat;
        *ratios = flat_r;
        if children.len() == 1 {
            *self = children.remove(0);
        }
    }
}

impl Layout {
    pub fn new() -> Self {
        Layout {
            root: Node::Group(Group::new(1, Vec::new())),
            focused: 1,
            next_id: 2,
        }
    }

    fn fresh(&mut self) -> GroupId {
        let id = self.next_id;
        self.next_id += 1;
        id
    }

    pub fn groups(&self) -> Vec<&Group> {
        let mut out = Vec::new();
        self.root.groups(&mut out);
        out
    }

    pub fn group(&self, id: GroupId) -> Option<&Group> {
        self.groups().into_iter().find(|g| g.id == id)
    }

    fn group_mut(&mut self, id: GroupId) -> Option<&mut Group> {
        self.root.find_mut(id)
    }

    pub fn group_count(&self) -> usize {
        self.groups().len()
    }

    pub fn focused_group(&self) -> &Group {
        self.group(self.focused)
            .unwrap_or_else(|| self.root.first_group())
    }

    pub fn active_tab(&self) -> Option<&Tab> {
        self.focused_group().active_tab()
    }

    /// Every tab in every group, in tree order.
    pub fn tabs(&self) -> Vec<&Tab> {
        self.groups()
            .into_iter()
            .flat_map(|g| g.tabs.iter())
            .collect()
    }

    pub fn contains(&self, tab: &Tab) -> bool {
        self.tabs().into_iter().any(|t| t == tab)
    }

    /// Groups (and positions) holding `tab`.
    pub fn find(&self, tab: &Tab) -> Vec<(GroupId, usize)> {
        self.groups()
            .into_iter()
            .filter_map(|g| g.position(tab).map(|i| (g.id, i)))
            .collect()
    }

    // ----- geometry -------------------------------------------------------

    pub fn rects(&self, area: Rect) -> Vec<(GroupId, Rect)> {
        let (mut out, mut divs) = (Vec::new(), Vec::new());
        self.root.place(area, &mut out, &mut divs, &mut Vec::new());
        out
    }

    pub fn dividers(&self, area: Rect) -> Vec<Divider> {
        let (mut out, mut divs) = (Vec::new(), Vec::new());
        self.root.place(area, &mut out, &mut divs, &mut Vec::new());
        divs
    }

    pub fn rect_of(&self, id: GroupId, area: Rect) -> Option<Rect> {
        self.rects(area)
            .into_iter()
            .find(|(g, _)| *g == id)
            .map(|(_, r)| r)
    }

    /// The smallest area that keeps every pane at its minimum.
    pub fn required(&self) -> (u16, u16) {
        (self.root.required(Dir::Row), self.root.required(Dir::Col))
    }

    /// Whether every group is at least the minimum in `area`.
    pub fn fits(&self, area: Rect) -> bool {
        self.rects(area)
            .iter()
            .all(|(_, r)| r.width >= MIN_W && r.height >= MIN_H)
    }

    /// Narrow collapse: more than one group and no room for their minimums, so
    /// only the focused group shows (the saved layout is untouched).
    pub fn collapsed(&self, area: Rect) -> bool {
        self.group_count() > 1 && !self.fits(area)
    }

    /// The groups shown in `area` with their rects: all, or the focused one
    /// alone when collapsed.
    pub fn visible(&self, area: Rect) -> Vec<(GroupId, Rect)> {
        if self.collapsed(area) {
            vec![(self.focused_group().id, area)]
        } else {
            self.rects(area)
        }
    }

    /// Groups hidden by the collapse.
    pub fn hidden(&self, area: Rect) -> usize {
        if self.collapsed(area) {
            self.group_count() - 1
        } else {
            0
        }
    }

    /// The group next to `from` in direction `side`: nearest edge, then the
    /// most overlap, then the nearest centre.
    pub fn neighbour(&self, from: GroupId, side: Side, area: Rect) -> Option<GroupId> {
        let rects = self.rects(area);
        let (_, me) = rects.iter().find(|(g, _)| *g == from)?;
        let horizontal = side.dir() == Dir::Row;
        let span = |r: &Rect| {
            if horizontal {
                (r.y as i32, (r.y + r.height) as i32)
            } else {
                (r.x as i32, (r.x + r.width) as i32)
            }
        };
        let centre = |r: &Rect| {
            if horizontal {
                r.y as i32 * 2 + r.height as i32
            } else {
                r.x as i32 * 2 + r.width as i32
            }
        };
        let mine = span(me);
        rects
            .iter()
            .filter(|(g, _)| *g != from)
            .filter_map(|(g, r)| {
                let gap = match side {
                    Side::Right => r.x as i32 - (me.x + me.width) as i32,
                    Side::Left => me.x as i32 - (r.x + r.width) as i32,
                    Side::Down => r.y as i32 - (me.y + me.height) as i32,
                    Side::Up => me.y as i32 - (r.y + r.height) as i32,
                };
                let theirs = span(r);
                let overlap = mine.1.min(theirs.1) - mine.0.max(theirs.0);
                (gap >= 0 && overlap > 0)
                    .then(|| (gap, -overlap, (centre(r) - centre(me)).abs(), *g))
            })
            .min()
            .map(|(_, _, _, g)| g)
    }

    pub fn has_neighbour(&self, side: Side, area: Rect) -> bool {
        self.neighbour(self.focused, side, area).is_some()
    }

    // ----- focus and tabs -------------------------------------------------

    pub fn focus(&mut self, id: GroupId) -> bool {
        if self.group(id).is_some() {
            self.focused = id;
            true
        } else {
            false
        }
    }

    pub fn focus_side(&mut self, side: Side, area: Rect) -> Result<(), LayoutError> {
        let to = self
            .neighbour(self.focused, side, area)
            .ok_or(LayoutError::NoNeighbour)?;
        self.focused = to;
        Ok(())
    }

    pub fn activate(&mut self, group: GroupId, index: usize) {
        if let Some(g) = self.group_mut(group) {
            if index < g.tabs.len() {
                g.active = index;
                self.focused = group;
            }
        }
    }

    /// Open in the focused group: activate when already there, else append.
    pub fn open(&mut self, tab: Tab) {
        let id = self.focused_group().id;
        self.open_in(id, tab);
    }

    pub fn open_in(&mut self, group: GroupId, tab: Tab) {
        let Some(g) = self.group_mut(group) else {
            return;
        };
        match g.position(&tab) {
            Some(i) => g.active = i,
            None => {
                g.tabs.push(tab);
                g.active = g.tabs.len() - 1;
            }
        }
        self.focused = group;
    }

    /// Add a tab to a group without activating or focusing it (unless the
    /// group was empty). Does nothing when the group already has it.
    pub fn append(&mut self, group: GroupId, tab: Tab) {
        if let Some(g) = self.group_mut(group) {
            if g.position(&tab).is_none() {
                g.tabs.push(tab);
            }
        }
    }

    /// Give a tab a new identity everywhere (the new-session tab becoming a
    /// session). A group that already holds `new` just loses `old`.
    pub fn replace_tab(&mut self, old: &Tab, new: Tab) {
        for (g, i) in self.find(old) {
            let has_new = self.group(g).is_some_and(|x| x.position(&new).is_some());
            if has_new {
                self.close_tab(g, i);
            } else if let Some(x) = self.group_mut(g) {
                x.tabs[i] = new.clone();
            }
        }
    }

    /// Open in the group to the right, creating it when there is none.
    pub fn open_to_side(&mut self, tab: Tab, area: Rect) -> Result<(), LayoutError> {
        match self.neighbour(self.focused, Side::Right, area) {
            Some(g) => {
                self.open_in(g, tab);
                Ok(())
            }
            None => self.split(Side::Right, tab, area).map(|_| ()),
        }
    }

    /// Step the focused group's active tab, wrapping.
    pub fn step(&mut self, step: isize) {
        let id = self.focused;
        if let Some(g) = self.group_mut(id) {
            if g.tabs.len() > 1 {
                g.active = (g.active as isize + step).rem_euclid(g.tabs.len() as isize) as usize;
            }
        }
    }

    /// Close one tab. An emptied group is removed unless it is the only one.
    /// Returns the tab.
    pub fn close_tab(&mut self, group: GroupId, index: usize) -> Option<Tab> {
        let g = self.group_mut(group)?;
        if index >= g.tabs.len() {
            return None;
        }
        let tab = g.tabs.remove(index);
        if g.active > index || g.active >= g.tabs.len() {
            g.active = g
                .active
                .saturating_sub(1)
                .min(g.tabs.len().saturating_sub(1));
        }
        if g.tabs.is_empty() && self.group_count() > 1 {
            self.remove_group(group);
        }
        Some(tab)
    }

    /// Remove every instance of a tab (its resource is gone).
    pub fn remove_resource(&mut self, tab: &Tab) {
        while let Some((g, i)) = self.find(tab).first().copied() {
            self.close_tab(g, i);
        }
    }

    /// Remove a group; the adjacent sibling takes its space.
    pub fn remove_group(&mut self, id: GroupId) {
        if self.group_count() <= 1 {
            return;
        }
        let Some(mut path) = self.root.path_to(id) else {
            return;
        };
        let Some(index) = path.pop() else { return };
        let was_focused = self.focused == id;
        let mut next_focus = None;
        if let Some(Node::Split {
            children, ratios, ..
        }) = self.root.at_mut(&path)
        {
            children.remove(index);
            let r = ratios.remove(index);
            let heir = index.min(children.len().saturating_sub(1));
            if let Some(x) = ratios.get_mut(heir) {
                *x += r;
            }
            next_focus = children.get(heir).map(|c| c.first_group().id);
        }
        self.root.normalise();
        if was_focused {
            if let Some(g) = next_focus {
                self.focused = g;
            }
        }
        if self.group(self.focused).is_none() {
            self.focused = self.root.first_group().id;
        }
    }

    // ----- splits ---------------------------------------------------------

    /// Show `tab` in a new group beside the focused one. Refused when a pane
    /// would fall under the minimum.
    pub fn split(&mut self, side: Side, tab: Tab, area: Rect) -> Result<GroupId, LayoutError> {
        let target = self.focused;
        self.split_group(target, side, vec![tab], area)
    }

    fn split_group(
        &mut self,
        target: GroupId,
        side: Side,
        tabs: Vec<Tab>,
        area: Rect,
    ) -> Result<GroupId, LayoutError> {
        let mut next = self.clone();
        let id = next.fresh();
        next.insert_beside(target, side, Group::new(id, tabs))?;
        if !next.fits(area) {
            return Err(LayoutError::TooSmall);
        }
        next.focused = id;
        *self = next;
        Ok(id)
    }

    fn insert_beside(
        &mut self,
        target: GroupId,
        side: Side,
        new: Group,
    ) -> Result<(), LayoutError> {
        let mut path = self.root.path_to(target).ok_or(LayoutError::Nothing)?;
        let dir = side.dir();
        let before = side.before();
        let index = path.pop();
        let parent_dir = match (index, self.root.at_mut(&path)) {
            (Some(_), Some(Node::Split { dir, .. })) => Some(*dir),
            _ => None,
        };
        if let (Some(i), Some(pd)) = (index, parent_dir) {
            if pd == dir {
                if let Some(Node::Split {
                    children, ratios, ..
                }) = self.root.at_mut(&path)
                {
                    let half = ratios[i] / 2.0;
                    ratios[i] = half;
                    let at = if before { i } else { i + 1 };
                    children.insert(at, Node::Group(new));
                    ratios.insert(at, half);
                }
                return Ok(());
            }
        }
        if let Some(i) = index {
            path.push(i);
        }
        let node = self.root.at_mut(&path).ok_or(LayoutError::Nothing)?;
        let old = std::mem::replace(node, Node::Group(Group::new(0, Vec::new())));
        let pair = if before {
            vec![Node::Group(new), old]
        } else {
            vec![old, Node::Group(new)]
        };
        *node = Node::Split {
            dir,
            children: pair,
            ratios: vec![0.5, 0.5],
        };
        Ok(())
    }

    /// Join: move the focused group's tabs into its neighbour on `side`
    /// (appended, the moved active tab stays active), close the split and
    /// focus the neighbour.
    pub fn join(&mut self, side: Side, area: Rect) -> Result<(), LayoutError> {
        let from = self.focused;
        let to = self
            .neighbour(from, side, area)
            .ok_or(LayoutError::NoNeighbour)?;
        let source = self.group(from).cloned().ok_or(LayoutError::Nothing)?;
        let active = source.active_tab().cloned();
        {
            let dest = self.group_mut(to).ok_or(LayoutError::Nothing)?;
            for t in source.tabs {
                if !dest.tabs.contains(&t) {
                    dest.tabs.push(t);
                }
            }
            if let Some(a) = active {
                dest.active = dest.position(&a).unwrap_or(0);
            }
        }
        self.remove_group(from);
        self.focused = to;
        Ok(())
    }

    /// Move the focused group's active tab to the neighbour on `side`.
    pub fn move_tab_side(&mut self, side: Side, area: Rect) -> Result<(), LayoutError> {
        let from = self.focused;
        let to = self
            .neighbour(from, side, area)
            .ok_or(LayoutError::NoNeighbour)?;
        let index = self.focused_group().active;
        self.move_tab_to(from, index, to, None, false)
    }

    /// Move (or copy) one tab into `to`, at `at` or the end. Inside one group
    /// this reorders. The target is focused and its tab active.
    pub fn move_tab_to(
        &mut self,
        from: GroupId,
        index: usize,
        to: GroupId,
        at: Option<usize>,
        copy: bool,
    ) -> Result<(), LayoutError> {
        let tab = self
            .group(from)
            .and_then(|g| g.tabs.get(index))
            .cloned()
            .ok_or(LayoutError::Nothing)?;
        if self.group(to).is_none() {
            return Err(LayoutError::Nothing);
        }
        if from == to {
            if copy {
                return Ok(());
            }
            let g = self.group_mut(to).ok_or(LayoutError::Nothing)?;
            let t = g.tabs.remove(index);
            let mut at = at.unwrap_or(g.tabs.len());
            if at > index {
                at -= 1;
            }
            let at = at.min(g.tabs.len());
            g.tabs.insert(at, t);
            g.active = at;
            self.focused = to;
            return Ok(());
        }
        {
            let dest = self.group_mut(to).ok_or(LayoutError::Nothing)?;
            match dest.position(&tab) {
                Some(i) => dest.active = i,
                None => {
                    let at = at.unwrap_or(dest.tabs.len()).min(dest.tabs.len());
                    dest.tabs.insert(at, tab);
                    dest.active = at;
                }
            }
        }
        if !copy {
            self.close_tab(from, index);
        }
        self.focused = to;
        Ok(())
    }

    /// Drop a tab on a group body: an edge zone splits the target and shows
    /// the tab in the new group; the centre moves it into the group.
    pub fn drop_on_body(
        &mut self,
        from: GroupId,
        index: usize,
        target: GroupId,
        zone: Zone,
        copy: bool,
        area: Rect,
    ) -> Result<(), LayoutError> {
        let tab = self
            .group(from)
            .and_then(|g| g.tabs.get(index))
            .cloned()
            .ok_or(LayoutError::Nothing)?;
        let Some(side) = zone_side(zone) else {
            return self.move_tab_to(from, index, target, None, copy);
        };
        let alone = self.group(from).is_some_and(|g| g.tabs.len() == 1);
        if from == target && alone && !copy {
            return Err(LayoutError::Nothing);
        }
        let mut next = self.clone();
        let id = next.fresh();
        next.insert_beside(target, side, Group::new(id, vec![tab]))?;
        if !copy {
            next.close_tab(from, index);
        }
        if !next.fits(area) {
            return Err(LayoutError::TooSmall);
        }
        next.focused = id;
        *self = next;
        Ok(())
    }

    // ----- resizing -------------------------------------------------------

    /// Put a divider at cell `pos` (absolute x for a row split, y for a
    /// column split), clamped so both neighbours keep their minimum.
    pub fn drag_divider(&mut self, d: &Divider, pos: u16, area: Rect) {
        // The split's own rect, from its path.
        let split_rect = {
            let mut rect = area;
            let mut node = &self.root;
            for i in &d.path {
                let Node::Split {
                    dir,
                    children,
                    ratios,
                } = node
                else {
                    return;
                };
                let n = children.len() as u16;
                let len = if *dir == Dir::Row {
                    rect.width
                } else {
                    rect.height
                };
                let sizes = lengths(len.saturating_sub(n - 1), ratios);
                let off: u16 = sizes[..*i].iter().sum::<u16>() + *i as u16;
                rect = if *dir == Dir::Row {
                    Rect::new(rect.x + off, rect.y, sizes[*i], rect.height)
                } else {
                    Rect::new(rect.x, rect.y + off, rect.width, sizes[*i])
                };
                node = &children[*i];
            }
            rect
        };
        let Some(Node::Split {
            dir,
            children,
            ratios,
        }) = self.root.at_mut(&d.path)
        else {
            return;
        };
        let n = children.len() as u16;
        let (start, len) = if *dir == Dir::Row {
            (split_rect.x, split_rect.width)
        } else {
            (split_rect.y, split_rect.height)
        };
        let total = len.saturating_sub(n - 1);
        if total == 0 || d.index + 1 >= children.len() {
            return;
        }
        let sizes = lengths(total, ratios);
        let before: u16 = sizes[..d.index].iter().sum::<u16>() + d.index as u16;
        let pair = sizes[d.index] + sizes[d.index + 1];
        let want = pos.saturating_sub(start).saturating_sub(before);
        let lo = children[d.index].required(*dir);
        let hi = pair.saturating_sub(children[d.index + 1].required(*dir));
        if lo > hi {
            return;
        }
        let left = want.clamp(lo, hi);
        let sum = ratios[d.index] + ratios[d.index + 1];
        let a = sum * left as f32 / pair.max(1) as f32;
        ratios[d.index] = a;
        ratios[d.index + 1] = sum - a;
    }

    // ----- persistence ----------------------------------------------------

    /// Rebuild a saved layout: tabs whose resource is gone are dropped,
    /// emptied groups collapse, ids and focus are repaired. Returns the
    /// layout and how many tabs were dropped.
    pub fn restore(saved: Layout, exists: &dyn Fn(&Tab) -> bool) -> (Layout, usize) {
        let mut layout = saved;
        let mut dropped = 0;
        // Sanitise ratios and drop dead tabs.
        fn clean(node: &mut Node, exists: &dyn Fn(&Tab) -> bool, dropped: &mut usize) -> bool {
            match node {
                Node::Group(g) => {
                    let before = g.tabs.len();
                    let active = g.active_tab().cloned();
                    let mut seen = std::collections::HashSet::new();
                    g.tabs.retain(|t| exists(t) && seen.insert(t.clone()));
                    *dropped += before - g.tabs.len();
                    g.active = active
                        .and_then(|a| g.position(&a))
                        .unwrap_or(0)
                        .min(g.tabs.len().saturating_sub(1));
                    true
                }
                Node::Split {
                    children, ratios, ..
                } => {
                    if ratios.len() != children.len()
                        || ratios.iter().any(|r| !r.is_finite() || *r <= 0.0)
                    {
                        *ratios = vec![1.0; children.len()];
                    }
                    let mut keep = Vec::new();
                    for c in children.iter_mut() {
                        keep.push(clean(c, exists, dropped));
                    }
                    let _ = keep;
                    true
                }
            }
        }
        clean(&mut layout.root, exists, &mut dropped);
        // Drop empty groups, keeping at least one.
        loop {
            let empty = layout
                .groups()
                .iter()
                .find(|g| g.tabs.is_empty())
                .map(|g| g.id);
            match empty {
                Some(id) if layout.group_count() > 1 => layout.remove_group(id),
                _ => break,
            }
        }
        layout.root.normalise();
        // Unique ids; `next_id` above every id.
        let mut seen = std::collections::HashSet::new();
        let mut max = 0;
        let mut next = layout.next_id.max(1);
        fn reid(
            node: &mut Node,
            seen: &mut std::collections::HashSet<GroupId>,
            max: &mut GroupId,
            next: &mut GroupId,
        ) {
            match node {
                Node::Group(g) => {
                    if g.id == 0 || !seen.insert(g.id) {
                        while !seen.insert(*next) {
                            *next += 1;
                        }
                        g.id = *next;
                    }
                    *max = (*max).max(g.id);
                }
                Node::Split { children, .. } => {
                    children.iter_mut().for_each(|c| reid(c, seen, max, next))
                }
            }
        }
        reid(&mut layout.root, &mut seen, &mut max, &mut next);
        layout.next_id = layout.next_id.max(max + 1);
        if layout.group(layout.focused).is_none() {
            layout.focused = layout.root.first_group().id;
        }
        (layout, dropped)
    }
}

/// The saved layouts of the TUI prefs, keyed by project and worktree.
pub type Saved = BTreeMap<String, Layout>;

/// The prefs key of a project's worktree (`None` is the main checkout).
pub fn key(project: &str, worktree: Option<&str>) -> String {
    format!("{project}\u{1f}{}", worktree.unwrap_or(""))
}

#[cfg(test)]
mod tests {
    use super::*;

    const AREA: Rect = Rect {
        x: 0,
        y: 0,
        width: 120,
        height: 40,
    };

    fn t(n: &str) -> Tab {
        Tab::file(n)
    }

    fn two() -> Layout {
        // a b | c   (row split, focused on the right group)
        let mut l = Layout::new();
        l.open(t("a"));
        l.open(t("b"));
        l.split(Side::Right, t("c"), AREA).unwrap();
        l
    }

    fn ids(l: &Layout) -> Vec<GroupId> {
        l.groups().iter().map(|g| g.id).collect()
    }

    #[test]
    fn open_activates_an_existing_tab_and_appends_a_new_one() {
        let mut l = Layout::new();
        l.open(t("a"));
        l.open(t("b"));
        l.open(t("a"));
        let g = l.focused_group();
        assert_eq!(g.tabs, vec![t("a"), t("b")]);
        assert_eq!(g.active, 0);
        l.open(t("c"));
        assert_eq!(l.focused_group().active, 2);
    }

    #[test]
    fn split_right_shows_the_tab_in_a_new_focused_group() {
        let l = two();
        assert_eq!(l.group_count(), 2);
        assert_eq!(l.focused_group().tabs, vec![t("c")]);
        let rects = l.rects(AREA);
        assert!(rects[0].1.x < rects[1].1.x);
        assert_eq!(rects[0].1.width + 1 + rects[1].1.width, AREA.width);
    }

    #[test]
    fn split_down_stacks_and_same_direction_splits_stay_flat() {
        let mut l = Layout::new();
        l.open(t("a"));
        l.split(Side::Down, t("a"), AREA).unwrap();
        l.split(Side::Down, t("a"), AREA).unwrap();
        let Node::Split {
            dir,
            children,
            ratios,
        } = &l.root
        else {
            panic!("not split")
        };
        assert_eq!(*dir, Dir::Col);
        assert_eq!(children.len(), 3);
        assert!((ratios.iter().sum::<f32>() - 1.0).abs() < 1e-4);
        let r = l.rects(AREA);
        let total: u16 = r.iter().map(|(_, r)| r.height).sum::<u16>() + 2;
        assert_eq!(total, AREA.height);
    }

    #[test]
    fn nested_splits_in_the_other_direction() {
        let mut l = two();
        l.split(Side::Down, t("d"), AREA).unwrap();
        assert_eq!(l.group_count(), 3);
        let Node::Split { children, .. } = &l.root else {
            panic!()
        };
        assert!(matches!(children[1], Node::Split { dir: Dir::Col, .. }));
        // Left of the two stacked groups is group 1.
        let bottom = l.focused;
        assert_eq!(l.neighbour(bottom, Side::Left, AREA), Some(1));
    }

    #[test]
    fn split_left_and_up_put_the_new_group_first() {
        let mut l = Layout::new();
        l.open(t("a"));
        let id = l.split(Side::Left, t("b"), AREA).unwrap();
        assert_eq!(ids(&l)[0], id);
        let id2 = l.split(Side::Up, t("c"), AREA).unwrap();
        let r = l.rect_of(id2, AREA).unwrap();
        let r1 = l.rect_of(id, AREA).unwrap();
        assert!(r.y < r1.y + r1.height);
    }

    #[test]
    fn split_is_refused_at_the_minimum_and_changes_nothing() {
        let small = Rect::new(0, 0, 41, 10);
        let mut l = Layout::new();
        l.open(t("a"));
        // 41 wide: 20 + gutter + 20 fits.
        l.split(Side::Right, t("b"), small).unwrap();
        let before = l.clone();
        assert_eq!(
            l.split(Side::Right, t("c"), small),
            Err(LayoutError::TooSmall)
        );
        assert_eq!(l, before);
        // Height: 5 + 1 + 5 = 11 needed; 10 is refused.
        let mut l = Layout::new();
        l.open(t("a"));
        assert_eq!(
            l.split(Side::Down, t("b"), small),
            Err(LayoutError::TooSmall)
        );
        assert_eq!(l.group_count(), 1);
        assert!(l.split(Side::Down, t("b"), Rect::new(0, 0, 40, 11)).is_ok());
    }

    #[test]
    fn focus_moves_by_geometry() {
        let mut l = two();
        l.split(Side::Down, t("d"), AREA).unwrap(); // right column: c over d
        let (a, c, d) = (1, 2, 3);
        assert_eq!(l.focused, d);
        assert_eq!(l.focus_side(Side::Up, AREA), Ok(()));
        assert_eq!(l.focused, c);
        assert_eq!(l.focus_side(Side::Left, AREA), Ok(()));
        assert_eq!(l.focused, a);
        assert_eq!(
            l.focus_side(Side::Left, AREA),
            Err(LayoutError::NoNeighbour)
        );
        assert_eq!(l.focus_side(Side::Right, AREA), Ok(()));
        // The nearest overlapping group on the right: the upper one (more overlap with a? equal) is deterministic.
        assert!(l.focused == c || l.focused == d);
    }

    #[test]
    fn neighbour_requires_overlap_not_just_direction() {
        // 2 x 2: a b / c d
        let mut l = Layout::new();
        l.open(t("a"));
        let b = l.split(Side::Right, t("b"), AREA).unwrap();
        l.focus(1);
        let c = l.split(Side::Down, t("c"), AREA).unwrap();
        l.focus(b);
        let d = l.split(Side::Down, t("d"), AREA).unwrap();
        assert_eq!(l.neighbour(c, Side::Right, AREA), Some(d));
        assert_eq!(l.neighbour(c, Side::Up, AREA), Some(1));
        assert_eq!(l.neighbour(b, Side::Down, AREA), Some(d));
        assert_eq!(l.neighbour(b, Side::Left, AREA), Some(1));
        assert_eq!(l.neighbour(d, Side::Right, AREA), None);
    }

    #[test]
    fn join_right_appends_and_keeps_the_moved_active_tab() {
        let mut l = Layout::new();
        l.open(t("a"));
        l.open(t("b"));
        l.split(Side::Right, t("c"), AREA).unwrap();
        l.open(t("d")); // right: c d (active d)
        l.focus(1); // left: a b (active b)
        l.join(Side::Right, AREA).unwrap();
        assert_eq!(l.group_count(), 1);
        let g = l.focused_group();
        assert_eq!(g.tabs, vec![t("c"), t("d"), t("a"), t("b")]);
        assert_eq!(g.active_tab(), Some(&t("b")));
    }

    #[test]
    fn join_down_and_unavailable_without_a_neighbour() {
        let mut l = Layout::new();
        l.open(t("a"));
        assert_eq!(l.join(Side::Down, AREA), Err(LayoutError::NoNeighbour));
        assert!(!l.has_neighbour(Side::Down, AREA));
        l.split(Side::Down, t("b"), AREA).unwrap();
        l.focus(1);
        assert!(l.has_neighbour(Side::Down, AREA));
        assert!(!l.has_neighbour(Side::Right, AREA));
        l.join(Side::Down, AREA).unwrap();
        assert_eq!(l.group_count(), 1);
        assert_eq!(l.focused_group().tabs, vec![t("b"), t("a")]);
    }

    #[test]
    fn join_does_not_duplicate_a_tab_both_groups_hold() {
        let mut l = Layout::new();
        l.open(t("a"));
        l.split(Side::Right, t("a"), AREA).unwrap();
        l.open(t("b"));
        l.focus(1);
        l.join(Side::Right, AREA).unwrap();
        assert_eq!(l.focused_group().tabs, vec![t("a"), t("b")]);
        assert_eq!(l.focused_group().active_tab(), Some(&t("a")));
    }

    #[test]
    fn move_tab_to_the_neighbour_removes_an_emptied_group() {
        let mut l = two(); // a b | c
        l.move_tab_side(Side::Left, AREA).unwrap();
        assert_eq!(l.group_count(), 1);
        assert_eq!(l.focused_group().tabs, vec![t("a"), t("b"), t("c")]);
        assert_eq!(l.focused_group().active_tab(), Some(&t("c")));
    }

    #[test]
    fn move_tab_keeps_both_groups_when_the_source_has_more() {
        let mut l = two();
        l.open(t("e")); // right: c e
        l.move_tab_side(Side::Left, AREA).unwrap();
        assert_eq!(l.group_count(), 2);
        assert_eq!(l.focused, 1);
        assert_eq!(l.group(2).unwrap().tabs, vec![t("c")]);
        assert_eq!(
            l.move_tab_side(Side::Left, AREA),
            Err(LayoutError::NoNeighbour)
        );
    }

    #[test]
    fn closing_the_last_tab_removes_the_group_and_the_sibling_takes_the_space() {
        let mut l = two();
        let r1 = l.rect_of(1, AREA).unwrap().width;
        l.close_tab(2, 0);
        assert_eq!(l.group_count(), 1);
        assert_eq!(l.rect_of(1, AREA).unwrap(), AREA);
        assert!(r1 < AREA.width);
        assert_eq!(l.focused, 1);
    }

    #[test]
    fn closing_the_last_group_keeps_one_empty_group() {
        let mut l = Layout::new();
        l.open(t("a"));
        l.close_tab(1, 0);
        assert_eq!(l.group_count(), 1);
        assert!(l.focused_group().tabs.is_empty());
        assert_eq!(l.active_tab(), None);
    }

    #[test]
    fn closing_a_tab_keeps_a_sensible_active_tab() {
        let mut l = Layout::new();
        for n in ["a", "b", "c"] {
            l.open(t(n));
        }
        l.close_tab(1, 2);
        assert_eq!(l.active_tab(), Some(&t("b")));
        l.activate(1, 0);
        l.close_tab(1, 1);
        assert_eq!(l.active_tab(), Some(&t("a")));
    }

    #[test]
    fn nested_removal_flattens_the_tree() {
        let mut l = two();
        l.split(Side::Down, t("d"), AREA).unwrap();
        l.remove_group(3);
        let Node::Split { children, .. } = &l.root else {
            panic!()
        };
        assert_eq!(children.len(), 2);
        assert!(children.iter().all(|c| matches!(c, Node::Group(_))));
        // Removing from a 3-way row keeps ratios summing to 1.
        l.split(Side::Right, t("e"), AREA).unwrap();
        let Node::Split { ratios, .. } = &l.root else {
            panic!()
        };
        assert_eq!(ratios.len(), 3);
        l.remove_group(2);
        let Node::Split { ratios, .. } = &l.root else {
            panic!()
        };
        assert!((ratios.iter().sum::<f32>() - 1.0).abs() < 1e-4);
    }

    #[test]
    fn move_within_a_strip_reorders() {
        let mut l = Layout::new();
        for n in ["a", "b", "c"] {
            l.open(t(n));
        }
        l.move_tab_to(1, 0, 1, Some(3), false).unwrap();
        assert_eq!(l.focused_group().tabs, vec![t("b"), t("c"), t("a")]);
        assert_eq!(l.focused_group().active_tab(), Some(&t("a")));
        l.move_tab_to(1, 2, 1, Some(0), false).unwrap();
        assert_eq!(l.focused_group().tabs, vec![t("a"), t("b"), t("c")]);
    }

    #[test]
    fn dragging_a_tab_to_another_strip_moves_and_alt_copies() {
        let mut l = two();
        l.open_in(1, t("x")); // left: a b x
        l.move_tab_to(1, 0, 2, Some(0), false).unwrap();
        assert_eq!(l.group(2).unwrap().tabs, vec![t("a"), t("c")]);
        assert_eq!(l.group(1).unwrap().tabs, vec![t("b"), t("x")]);
        l.move_tab_to(2, 1, 1, None, true).unwrap();
        assert_eq!(l.group(2).unwrap().tabs, vec![t("a"), t("c")]);
        assert_eq!(l.group(1).unwrap().tabs, vec![t("b"), t("x"), t("c")]);
    }

    #[test]
    fn drop_zones_pick_the_nearest_edge() {
        let body = Rect::new(0, 1, 40, 20);
        assert_eq!(zone_at(body, 1, 10), Zone::Left);
        assert_eq!(zone_at(body, 38, 10), Zone::Right);
        assert_eq!(zone_at(body, 20, 2), Zone::Top);
        assert_eq!(zone_at(body, 20, 20), Zone::Bottom);
        assert_eq!(zone_at(body, 20, 10), Zone::Centre);
        let r = zone_rect(body, Zone::Left);
        assert_eq!((r.x, r.width, r.height), (0, 20, 20));
    }

    #[test]
    fn dropping_on_an_edge_splits_and_moves_the_tab() {
        let mut l = Layout::new();
        l.open(t("a"));
        l.open(t("b"));
        l.drop_on_body(1, 1, 1, Zone::Right, false, AREA).unwrap();
        assert_eq!(l.group_count(), 2);
        assert_eq!(l.group(1).unwrap().tabs, vec![t("a")]);
        assert_eq!(l.focused_group().tabs, vec![t("b")]);
        // Dropping a group's only tab on its own edge does nothing.
        assert_eq!(
            l.drop_on_body(2, 0, 2, Zone::Left, false, AREA),
            Err(LayoutError::Nothing)
        );
        // The centre moves into the group.
        l.drop_on_body(2, 0, 1, Zone::Centre, false, AREA).unwrap();
        assert_eq!(l.group_count(), 1);
        assert_eq!(l.focused_group().tabs, vec![t("a"), t("b")]);
    }

    #[test]
    fn dropping_with_copy_keeps_the_source_and_refuses_when_too_small() {
        let mut l = Layout::new();
        l.open(t("a"));
        l.drop_on_body(1, 0, 1, Zone::Bottom, true, AREA).unwrap();
        assert_eq!(l.group_count(), 2);
        assert_eq!(l.group(1).unwrap().tabs, vec![t("a")]);
        let mut l = Layout::new();
        l.open(t("a"));
        l.open(t("b"));
        let before = l.clone();
        let tiny = Rect::new(0, 0, 30, 10);
        assert_eq!(
            l.drop_on_body(1, 0, 1, Zone::Right, false, tiny),
            Err(LayoutError::TooSmall)
        );
        assert_eq!(l, before);
    }

    #[test]
    fn open_to_side_reuses_or_creates_the_right_group() {
        let mut l = Layout::new();
        l.open(t("a"));
        l.open_to_side(t("b"), AREA).unwrap();
        assert_eq!(l.group_count(), 2);
        assert_eq!(l.focused_group().tabs, vec![t("b")]);
        l.focus(1);
        l.open_to_side(t("c"), AREA).unwrap();
        assert_eq!(l.group_count(), 2);
        assert_eq!(l.focused_group().tabs, vec![t("b"), t("c")]);
    }

    #[test]
    fn dividers_resize_within_the_minimums() {
        let mut l = two();
        let d = l.dividers(AREA).remove(0);
        assert_eq!(d.dir, Dir::Row);
        l.drag_divider(&d, 80, AREA);
        let w1 = l.rect_of(1, AREA).unwrap().width;
        assert!((78..=81).contains(&w1), "{w1}");
        // Far left: the left pane keeps its minimum.
        let d = l.dividers(AREA).remove(0);
        l.drag_divider(&d, 0, AREA);
        assert_eq!(l.rect_of(1, AREA).unwrap().width, MIN_W);
        let d = l.dividers(AREA).remove(0);
        l.drag_divider(&d, 500, AREA);
        assert_eq!(l.rect_of(2, AREA).unwrap().width, MIN_W);
        assert!(l.fits(AREA));
    }

    #[test]
    fn column_dividers_resize_vertically_and_nested_minimums_hold() {
        let mut l = Layout::new();
        l.open(t("a"));
        l.split(Side::Down, t("b"), AREA).unwrap();
        let d = l.dividers(AREA).remove(0);
        assert_eq!(d.dir, Dir::Col);
        l.drag_divider(&d, 10, AREA);
        assert_eq!(l.rect_of(1, AREA).unwrap().height, 10);
        l.drag_divider(&d, 0, AREA);
        assert_eq!(l.rect_of(1, AREA).unwrap().height, MIN_H);
        // A nested row inside the lower group keeps the width minimum for both of its panes.
        l.split(Side::Right, t("c"), AREA).unwrap();
        let divs = l.dividers(AREA);
        let row = divs.iter().find(|d| d.dir == Dir::Row).unwrap().clone();
        l.drag_divider(&row, 119, AREA);
        assert!(l.fits(AREA));
    }

    #[test]
    fn narrow_widths_collapse_to_the_focused_group_without_rewriting() {
        let mut l = two();
        let before = l.clone();
        let narrow = Rect::new(0, 0, 30, 20);
        assert!(l.collapsed(narrow));
        assert_eq!(l.hidden(narrow), 1);
        let v = l.visible(narrow);
        assert_eq!(v, vec![(2, narrow)]);
        assert_eq!(l, before);
        assert!(!l.collapsed(AREA));
        assert_eq!(l.hidden(AREA), 0);
        assert_eq!(l.visible(AREA).len(), 2);
        assert_eq!(l.required(), (41, 5));
        // One group is never "collapsed".
        l.join(Side::Left, AREA).unwrap();
        assert!(!l.collapsed(narrow));
    }

    #[test]
    fn serialises_and_restores_unchanged() {
        let mut l = two();
        l.split(Side::Down, t("d"), AREA).unwrap();
        let json = serde_json::to_string(&l).unwrap();
        let back: Layout = serde_json::from_str(&json).unwrap();
        assert_eq!(back, l);
        let (restored, dropped) = Layout::restore(back, &|_| true);
        assert_eq!(restored, l);
        assert_eq!(dropped, 0);
    }

    #[test]
    fn restore_drops_missing_resources_and_collapses_empty_groups() {
        let mut l = Layout::new();
        l.open(Tab::session("s1"));
        l.open(t("gone.txt"));
        l.split(Side::Right, Tab::terminal("term-9"), AREA).unwrap();
        l.focus(1);
        l.activate(1, 1); // gone.txt active
        let exists = |tab: &Tab| tab.id != "gone.txt" && tab.id != "term-9";
        let (r, dropped) = Layout::restore(l, &exists);
        assert_eq!(dropped, 2);
        assert_eq!(r.group_count(), 1);
        assert_eq!(r.focused_group().tabs, vec![Tab::session("s1")]);
        assert_eq!(r.focused_group().active_tab(), Some(&Tab::session("s1")));
    }

    #[test]
    fn restore_repairs_a_damaged_layout() {
        let mut l = two();
        // Duplicate ids, a bogus focus, bad ratios.
        if let Node::Split {
            children, ratios, ..
        } = &mut l.root
        {
            if let Node::Group(g) = &mut children[1] {
                g.id = 1;
            }
            *ratios = vec![f32::NAN, -1.0];
        }
        l.focused = 99;
        let (r, _) = Layout::restore(l, &|_| true);
        let ids = ids(&r);
        assert_eq!(ids.len(), 2);
        assert_ne!(ids[0], ids[1]);
        assert!(r.group(r.focused).is_some());
        assert!(r.fits(AREA));
        // A saved layout with only dead tabs is one empty group.
        let (r, _) = Layout::restore(two(), &|_| false);
        assert_eq!(r.group_count(), 1);
        assert!(r.focused_group().tabs.is_empty());
    }

    #[test]
    fn remove_resource_closes_every_instance() {
        let mut l = Layout::new();
        l.open(t("a"));
        l.split(Side::Right, t("a"), AREA).unwrap();
        l.open(t("b"));
        l.remove_resource(&t("a"));
        assert_eq!(l.group_count(), 1);
        assert_eq!(l.focused_group().tabs, vec![t("b")]);
    }

    #[test]
    fn append_does_not_activate_and_replace_retitles_in_place() {
        let mut l = Layout::new();
        l.open(Tab::session(""));
        l.append(1, t("a"));
        l.append(1, t("a"));
        assert_eq!(l.focused_group().tabs.len(), 2);
        assert_eq!(l.active_tab(), Some(&Tab::session("")));
        l.replace_tab(&Tab::session(""), Tab::session("s1"));
        assert_eq!(l.focused_group().tabs, vec![Tab::session("s1"), t("a")]);
        // A group that already has the new tab only loses the old one.
        l.append(1, Tab::session("x"));
        l.replace_tab(&Tab::session("x"), Tab::session("s1"));
        assert_eq!(l.focused_group().tabs, vec![Tab::session("s1"), t("a")]);
    }

    #[test]
    fn step_wraps_inside_the_focused_group() {
        let mut l = Layout::new();
        for n in ["a", "b", "c"] {
            l.open(t(n));
        }
        l.step(1);
        assert_eq!(l.active_tab(), Some(&t("a")));
        l.step(-1);
        assert_eq!(l.active_tab(), Some(&t("c")));
    }

    #[test]
    fn the_prefs_key_separates_projects_and_worktrees() {
        assert_ne!(key("p", None), key("p", Some("w")));
        assert_ne!(key("p", Some("w")), key("q", Some("w")));
    }
}
