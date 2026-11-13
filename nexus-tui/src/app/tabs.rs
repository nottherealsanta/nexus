//! Tabs and splits in the shell (plan 45, docs/layout.md).
//!
//! `groups` holds the pure tree; this is the glue. The tree is authoritative
//! for placement. What is *live* (the viewed session, the open file or diff,
//! the attached terminal) follows the focused group's active tab: a click or a
//! layout command changes the tree, and `groups_sync_live` makes the live state
//! match. The reverse notices (`groups_note_open`) come from the places that
//! open things (sidebar rows, the new-session screen, terminal creation).
//!
//! Only the focused group's tab is live. Another group's file shows the last
//! text this client saw, its terminal shows the screen when that terminal is
//! the attached one, and its session shows its title: one timeline, composer
//! and Core attachment exist at a time.

use std::collections::VecDeque;

use super::*;
use crate::groups::Tab as GTab;
use crate::groups::{self, Divider, GroupId, Kind, Layout, LayoutError, Side, Zone};

/// A tab or divider being dragged.
#[derive(Debug, Clone, PartialEq)]
pub enum Drag {
    Tab {
        from: GroupId,
        index: usize,
        start: (u16, u16),
        moved: bool,
        over: Option<Over>,
    },
    Divider {
        divider: Divider,
    },
}

/// Where a dragged tab would land.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Over {
    Strip { group: GroupId, at: usize },
    Body { group: GroupId, zone: Zone },
}

/// Where a terminal being created goes.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Placement {
    /// A new tab in this group.
    Here(GroupId),
    /// A new group beside the focused one.
    Split(Side),
}

#[derive(Default)]
pub struct GroupsUi {
    pub layout: Layout,
    /// Layouts of other projects/worktrees (and every saved one at start).
    pub saved: BTreeMap<String, Layout>,
    pub key: Option<String>,
    pub project: Option<String>,
    /// The area the tree filled at the last draw, with each visible group.
    pub area: Rect,
    pub rects: Vec<(GroupId, Rect)>,
    pub dividers: Vec<Divider>,
    /// Where each strip tab was drawn, for drops: (group, index, rect).
    pub tab_rects: Vec<(GroupId, usize, Rect)>,
    /// Groups that do not fit and are not drawn (narrow collapse).
    pub hidden: usize,
    /// The tab the live state was last made to match.
    pub synced: Option<GTab>,
    /// The live state must be made to match even when the tab is the same
    /// (a closed tab, a new project).
    pub stale: bool,
    pub drag: Option<Drag>,
    /// Last text seen per file path, for tabs whose group is not focused.
    pub texts: HashMap<String, String>,
    /// Unsaved editor buffers of files that are not live: one per file.
    pub buffers: HashMap<String, FileEdit>,
    pub recent_terminal: Option<String>,
    pub placements: VecDeque<Placement>,
    /// `Ctrl`-click asked the next open to go to the side.
    pub side_open: bool,
    /// Save was chosen in the unsaved prompt: close this tab once saved.
    pub close_after_save: Option<(GroupId, GTab)>,
    /// Save all was chosen for a group close: save each dirty file, then close the group.
    pub close_group_saving: Option<GroupId>,
    /// A group close is closing a group's terminals; keep going as each one goes.
    pub close_group_resume: Option<GroupId>,
    /// Terminals a group close already asked to close (not to ask twice).
    pub close_group_asked: std::collections::HashSet<String>,
    /// Sessions a drawn, unfocused group shows: their views are pinned and
    /// their history watched.
    pub watching: Vec<String>,
    /// Timelines built for those groups, by session and content width.
    pub bg: HashMap<(String, u16), (u64, std::rc::Rc<crate::timeline::Timeline>)>,
    /// The terminal list of this project has been read.
    pub terminals_loaded: bool,
}

/// The diff tab id for a Git row.
pub fn diff_tab(path: &str, group: GitGroup) -> GTab {
    GTab::new(Kind::Diff, format!("{}\u{1f}{path}", group.label()))
}

fn diff_parts(tab: &GTab) -> Option<(GitGroup, String)> {
    let (label, path) = tab.id.split_once('\u{1f}')?;
    let group = [GitGroup::Staged, GitGroup::Unstaged, GitGroup::Untracked]
        .into_iter()
        .find(|g| g.label() == label)?;
    Some((group, path.to_owned()))
}

fn base_name(path: &str) -> &str {
    path.rsplit('/').next().unwrap_or(path)
}

impl App {
    // ---- reading the tree ---------------------------------------------------------------

    pub fn groups(&self) -> &Layout {
        &self.ui.groups.layout
    }

    /// The area the tree fills: last drawn, else the centre column.
    pub fn tree_area(&self) -> Rect {
        let a = self.ui.groups.area;
        if a.width > 0 && a.height > 0 {
            a
        } else {
            self.regions.centre
        }
    }

    /// The focused group's active tab, when there is one.
    pub fn active_tab(&self) -> Option<GTab> {
        self.ui.groups.layout.active_tab().cloned()
    }

    /// The focused group shows the session page (a session tab, or no tab at
    /// all: the new-session screen).
    pub fn session_focused(&self) -> bool {
        self.ui
            .groups
            .layout
            .active_tab()
            .is_none_or(|t| t.kind == Kind::Session)
    }

    pub fn terminal_live(&self) -> bool {
        self.active_tab().is_some_and(|t| {
            t.kind == Kind::Terminal && self.terminal_panel.selected.as_deref() == Some(&t.id)
        })
    }

    /// Whether the focused tab is a terminal (attached or still attaching).
    pub fn terminal_tab_focused(&self) -> bool {
        self.active_tab().is_some_and(|t| t.kind == Kind::Terminal)
    }

    pub fn tab_title(&self, tab: &GTab) -> String {
        match tab.kind {
            Kind::Session if tab.id.is_empty() => "New session".to_owned(),
            Kind::Session => self
                .state
                .model
                .sessions
                .get(&tab.id)
                .map_or_else(|| tab.id.clone(), |s| s.title.clone()),
            Kind::File => base_name(&tab.id).to_owned(),
            Kind::Diff => diff_parts(tab)
                .map_or_else(|| tab.id.clone(), |(_, path)| base_name(&path).to_owned()),
            Kind::Terminal => self
                .terminal_panel
                .terminals
                .iter()
                .find(|t| t.id == tab.id)
                .map_or_else(|| "terminal".to_owned(), |t| t.name.clone()),
            Kind::Browser => "Browser".to_owned(),
        }
    }

    /// Unsaved text for a file: the live editor, or the buffer kept aside.
    pub fn file_dirty(&self, path: &str) -> bool {
        self.ui
            .groups
            .buffers
            .get(path)
            .is_some_and(FileEdit::dirty)
            || (self.live_file_path().as_deref() == Some(path)
                && self.ui.file_edit.as_ref().is_some_and(FileEdit::dirty))
    }

    fn live_file_path(&self) -> Option<String> {
        if !self.ui.file_open {
            return None;
        }
        self.state.file.as_ref().map(|(_, _, p, _)| p.clone())
    }

    /// What a file tab shows when its group is not focused: the editor's text,
    /// else the last read.
    pub fn file_tab_text(&self, path: &str) -> Option<String> {
        if let Some(edit) = self.ui.groups.buffers.get(path) {
            return Some(edit.buf.text());
        }
        if self.live_file_path().as_deref() == Some(path) {
            if let Some(edit) = &self.ui.file_edit {
                return Some(edit.buf.text());
            }
        }
        self.ui.groups.texts.get(path).cloned()
    }

    // ---- keeping the tree and the live state together --------------------------------------

    /// Called every tick and before every draw: enter the project, mirror the
    /// open sessions, and make the live state follow the focused tab.
    pub fn groups_reconcile(&mut self) {
        self.groups_enter_project();
        self.groups_sync_sessions();
        self.groups_remember_file();
        self.groups_sync_live();
        self.groups_sync_terminals();
        self.groups_sync_watch();
        self.groups_continue_close();
    }

    /// Sessions shown by drawn groups other than the focused one.
    fn background_sessions(&self) -> Vec<String> {
        let drawn = &self.ui.groups.rects;
        let focused = self.ui.groups.layout.focused;
        let viewed = self.viewed_id().map(str::to_owned);
        let mut ids: Vec<String> = self
            .ui
            .groups
            .layout
            .groups()
            .into_iter()
            .filter(|g| g.id != focused)
            .filter(|g| drawn.is_empty() || drawn.iter().any(|(d, _)| *d == g.id))
            .filter_map(|g| g.active_tab())
            .filter(|t| t.kind == Kind::Session && !t.id.is_empty())
            .map(|t| t.id.clone())
            .filter(|id| Some(id) != viewed.as_ref())
            .collect();
        ids.sort();
        ids.dedup();
        ids
    }

    /// Every drawn session tab keeps a live view: pin it and have its history
    /// watched; a tab that is no longer drawn lets go.
    fn groups_sync_watch(&mut self) {
        let ids = self.background_sessions();
        if ids == self.ui.groups.watching {
            return;
        }
        self.state.model.pin(&ids);
        self.ui.groups.watching = ids.clone();
        self.timeline_revision
            .set(self.timeline_revision.get().wrapping_add(1));
        self.effects.push(Effect::Sync(SyncCommand::Watch(ids)));
    }

    /// The timeline of a session shown by an unfocused group, built from its
    /// own view (swapped in for the build) and cached by width.
    pub fn background_timeline(
        &mut self,
        id: &str,
        width: usize,
    ) -> Option<std::rc::Rc<crate::timeline::Timeline>> {
        let token = self.state.model.swap_in(id);
        if token.is_none() && self.viewed_id() != Some(id) {
            return None;
        }
        let key = (id.to_owned(), width as u16);
        let mut map = std::mem::take(&mut self.ui.groups.bg);
        let hit = map.get(&key).and_then(|(cached, tl)| {
            (*cached == self.timeline_cache_key(width, tl.animated, tl.clocked))
                .then(|| std::rc::Rc::clone(tl))
        });
        let tl = hit.unwrap_or_else(|| {
            let tl = std::rc::Rc::new(crate::timeline::build(self, &self.theme, self.frame, width));
            let k = self.timeline_cache_key(width, tl.animated, tl.clocked);
            map.insert(key, (k, std::rc::Rc::clone(&tl)));
            tl
        });
        self.ui.groups.bg = map;
        if let Some(token) = token {
            self.state.model.swap_out(token);
        }
        Some(tl)
    }

    fn tab_exists(&self, project: &str, worktree: Option<&str>, tab: &GTab) -> bool {
        match tab.kind {
            // The new-session screen is a tab too; it is kept so a project that
            // was left on it comes back to it.
            Kind::Session if tab.id.is_empty() => true,
            Kind::Session => {
                self.state.model.sessions.get(&tab.id).is_some_and(|s| {
                    s.project_id == project && s.parent_id.is_none() && !s.archived
                })
            }
            Kind::File | Kind::Diff => {
                let rel = match tab.kind {
                    Kind::Diff => diff_parts(tab).map(|(_, p)| p),
                    _ => Some(tab.id.clone()),
                };
                let Some(rel) = rel else { return false };
                let roots = [
                    worktree.map(str::to_owned),
                    self.state
                        .model
                        .projects
                        .get(project)
                        .map(|p| p.path.clone()),
                ];
                tab.kind == Kind::Diff
                    || roots
                        .iter()
                        .flatten()
                        .any(|root| std::path::Path::new(root).join(&rel).exists())
            }
            Kind::Terminal => {
                !self.ui.groups.terminals_loaded
                    || self.terminal_panel.terminals.iter().any(|t| t.id == tab.id)
            }
            Kind::Browser => false,
        }
    }

    /// Load the layout saved for the project (and its worktree) the first time
    /// it is current, saving the one it replaces.
    pub(super) fn groups_enter_project(&mut self) {
        let Some(project) = self.current_project() else {
            return;
        };
        // One layout per project and worktree: the viewed session's worktree
        // decides, so moving to a session in another worktree swaps layouts as
        // a project switch does.
        let worktree = self.git_worktree();
        let key = groups::key(&project, worktree.as_deref());
        if self.ui.groups.key.as_deref() == Some(key.as_str()) {
            return;
        }
        let project_changed = self.ui.groups.project.as_deref() != Some(project.as_str());
        // The worktree listing arriving late turns "no worktree" into one:
        // that is the same layout learning its name, not a new one.
        let was_unnamed =
            self.ui.groups.key.as_deref() == Some(groups::key(&project, None).as_str());
        if !project_changed
            && was_unnamed
            && worktree.is_some()
            && !self.ui.groups.saved.contains_key(&key)
        {
            self.ui.groups.key = Some(key);
            return;
        }
        if let Some(old) = self.ui.groups.key.take() {
            let layout = self.ui.groups.layout.clone();
            self.ui.groups.saved.insert(old, layout);
            if project_changed {
                self.detach_terminal();
            }
        }
        let layout = match self.ui.groups.saved.get(&key).cloned() {
            Some(saved) => {
                let (layout, _) = Layout::restore(saved, &|t| {
                    self.tab_exists(&project, worktree.as_deref(), t)
                });
                layout
            }
            None => Layout::new(),
        };
        for tab in layout.tabs() {
            if tab.kind == Kind::Session && !self.ui.tabs.contains(&tab.id) {
                self.ui.tabs.push(tab.id.clone());
            }
        }
        self.ui.groups.layout = layout;
        self.ui.groups.key = Some(key);
        self.ui.groups.project = Some(project.clone());
        self.ui.groups.synced = None;
        self.ui.groups.stale = true;
        self.ui.groups.drag = None;
        // Terminals are read only when the layout has a tab to check.
        let has_terminals = self
            .ui
            .groups
            .layout
            .tabs()
            .iter()
            .any(|t| t.kind == Kind::Terminal);
        if project_changed {
            self.ui.groups.terminals_loaded = !has_terminals;
        }
        self.terminal_open = true;
        if has_terminals && project_changed {
            self.terminal_panel.loading = true;
            self.api(ApiCall::TerminalList { project });
        }
    }

    /// Leave the attached terminal (another project's layout is coming in).
    fn detach_terminal(&mut self) {
        if self.terminal_panel.selected.is_some() {
            self.effects
                .push(Effect::TerminalStop(self.terminal_panel.generation));
            self.terminal_panel.generation = self.terminal_panel.next_generation();
            self.terminal_panel.attachment = None;
            self.terminal_panel.selected = None;
            self.terminal_panel.input_queue.clear();
            self.terminal_panel.control_busy = false;
            self.terminal_selected = false;
            self.terminal_focused = false;
            self.terminal_controlled = false;
        }
        for p in std::mem::take(&mut self.terminal_panel.parked) {
            self.effects.push(Effect::TerminalStop(p.generation));
        }
        self.terminal_panel.terminals.clear();
    }

    /// Every open session has a tab somewhere; a closed one has none.
    fn groups_sync_sessions(&mut self) {
        let desired = self.tab_ids();
        let stale: Vec<GTab> = self
            .ui
            .groups
            .layout
            .tabs()
            .into_iter()
            .filter(|t| {
                t.kind == Kind::Session
                    && !t.id.is_empty()
                    && !desired.contains(&t.id)
                    // A subagent view keeps its root's tab.
                    && self.viewed_root().as_deref() != Some(t.id.as_str())
            })
            .cloned()
            .collect();
        for tab in stale {
            self.ui.groups.layout.remove_resource(&tab);
        }
        let focused = self.ui.groups.layout.focused;
        // A fresh layout opens on the session in view, not the first tab.
        let was_empty = self.ui.groups.layout.focused_group().tabs.is_empty();
        for id in desired {
            let tab = GTab::session(id);
            if !self.ui.groups.layout.contains(&tab) {
                self.ui.groups.layout.append(focused, tab);
            }
        }
        if was_empty {
            if let Some(viewed) = self.viewed_root() {
                let tab = GTab::session(viewed);
                if let Some((g, i)) = self.ui.groups.layout.find(&tab).first().copied() {
                    self.ui.groups.layout.activate(g, i);
                }
            }
        }
        if self.viewed_id().is_some() {
            self.ui
                .groups
                .layout
                .remove_resource(&GTab::session(String::new()));
        }
    }

    /// Keep the text of the live file for tabs that are not focused, and give
    /// a returning file its unsaved buffer back.
    fn groups_remember_file(&mut self) {
        let Some((_, _, path, data)) = self.state.file.clone() else {
            return;
        };
        if let Ok(data) = &data {
            if let Some(text) = data["content"].as_str() {
                if self.ui.groups.texts.len() > 48 && !self.ui.groups.texts.contains_key(&path) {
                    let keep: Vec<String> = self
                        .ui
                        .groups
                        .layout
                        .tabs()
                        .iter()
                        .filter(|t| t.kind == Kind::File)
                        .map(|t| t.id.clone())
                        .collect();
                    self.ui.groups.texts.retain(|p, _| keep.contains(p));
                }
                if self.ui.groups.texts.get(&path).map(String::as_str) != Some(text) {
                    self.ui.groups.texts.insert(path.clone(), text.to_owned());
                }
            }
        }
        if self.ui.file_open && self.ui.file_edit.is_none() {
            if let Some(edit) = self.ui.groups.buffers.remove(&path) {
                self.ui.file_edit = Some(edit);
                self.ui.file_source = true;
            }
        }
    }

    /// Put the live editor aside when its file stops being live: unsaved text
    /// survives a switch of tab.
    pub(super) fn stash_edit(&mut self) {
        let path = self.state.file.as_ref().map(|(_, _, p, _)| p.clone());
        if let (Some(edit), Some(path)) = (self.ui.file_edit.take(), path) {
            if edit.dirty() {
                self.ui.groups.buffers.insert(path, edit);
            }
        }
    }

    /// Give the open file the unsaved buffer kept aside for it. When the file
    /// moved on while it was away, the editor says so (`stale`) and keeps the text.
    pub(super) fn restore_stashed_edit(&mut self, revision: Option<&str>) -> bool {
        let Some(path) = self.state.file.as_ref().map(|(_, _, p, _)| p.clone()) else {
            return false;
        };
        let Some(mut edit) = self.ui.groups.buffers.remove(&path) else {
            return false;
        };
        if revision.is_some_and(|r| r != edit.revision) {
            edit.stale = true;
        }
        self.ui.file_edit = Some(edit);
        self.ui.file_source = true;
        true
    }

    /// Drop a file's unsaved text everywhere.
    fn discard_file(&mut self, path: &str) {
        self.ui.groups.buffers.remove(path);
        if self.live_file_path().as_deref() == Some(path) {
            self.ui.file_edit = None;
        }
    }

    /// Make the live state match the focused group's active tab.
    pub fn groups_sync_live(&mut self) {
        let active = self.active_tab();
        if !self.ui.groups.stale && self.ui.groups.synced == active {
            return;
        }
        let Some(tab) = active.clone() else {
            // No tab in the focused group: the session page shows whatever is
            // viewed (closing a session's last tab has already moved off it).
            self.deselect_panes();
            self.ui.groups.synced = None;
            self.ui.groups.stale = false;
            return;
        };
        let done = match tab.kind {
            Kind::Session => {
                self.stash_edit();
                self.deselect_panes();
                if tab.id.is_empty() {
                    if self.viewed_id().is_some() {
                        self.show_new_session_live();
                    }
                } else if self.viewed_root().as_deref() != Some(tab.id.as_str()) {
                    self.open_session_live(&tab.id);
                }
                true
            }
            Kind::File => {
                self.stash_edit();
                self.terminal_focused = false;
                if self.live_file_path().as_deref() != Some(tab.id.as_str()) {
                    self.ui.diff_open = None;
                    self.ui.file_open = true;
                    self.ui.file_scroll = 0;
                    self.ui.file_source = false;
                    self.ui.file_search = None;
                    self.request_files(tab.id.clone(), true);
                }
                self.ui.focus = Focus::Timeline;
                true
            }
            Kind::Diff => {
                self.stash_edit();
                self.terminal_focused = false;
                if let Some((group, path)) = diff_parts(&tab) {
                    if self.ui.diff_open.as_ref() != Some(&(path.clone(), group)) {
                        self.open_diff_live(path, group);
                    }
                }
                true
            }
            Kind::Terminal => {
                self.stash_edit();
                self.ui.file_open = false;
                self.ui.diff_open = None;
                if self.terminal_panel.selected.as_deref() == Some(tab.id.as_str()) {
                    self.terminal_selected = true;
                    self.terminal_focused = true;
                    true
                } else {
                    self.terminal_select_id(&tab.id)
                }
            }
            Kind::Browser => true,
        };
        if done {
            if tab.kind == Kind::Terminal {
                self.ui.groups.recent_terminal = Some(tab.id.clone());
            }
            self.ui.groups.synced = active;
            self.ui.groups.stale = false;
        }
    }

    /// What the focused tab is not: no file, diff or terminal screen has the keys.
    fn deselect_panes(&mut self) {
        self.ui.file_open = false;
        self.ui.diff_open = None;
        self.terminal_focused = false;
    }

    /// The file panel's diff, without touching the layout.
    fn open_diff_live(&mut self, path: String, group: GitGroup) {
        let Some(project) = self.current_project() else {
            return;
        };
        self.ui.file_open = false;
        self.ui.diff_scroll = 0;
        self.state.diff = None;
        if group != GitGroup::Untracked {
            self.api(ApiCall::ProjectGitDiff {
                project,
                worktree: self.git_worktree(),
                path: path.clone(),
            });
        }
        self.ui.diff_open = Some((path, group));
        self.ui.focus = Focus::Timeline;
    }

    /// Something was opened by the older paths (a sidebar row, the new-session
    /// screen): show it as a tab in the focused group, or the group to the
    /// right after a Ctrl+click.
    pub fn groups_note_open(&mut self, tab: GTab) {
        if tab.kind == Kind::Session && !tab.id.is_empty() {
            let blank = GTab::session(String::new());
            let layout = &mut self.ui.groups.layout;
            if layout.active_tab() == Some(&blank) {
                layout.replace_tab(&blank, tab.clone());
            } else {
                layout.remove_resource(&blank);
            }
        }
        let side = std::mem::take(&mut self.ui.groups.side_open);
        let area = self.tree_area();
        let layout = &mut self.ui.groups.layout;
        if side {
            if layout.open_to_side(tab.clone(), area).is_err() {
                self.toast_too_small();
                self.ui.groups.layout.open(tab.clone());
            }
        } else if let Some((g, i)) = layout
            .find(&tab)
            .first()
            .copied()
            .filter(|_| tab.kind == Kind::Session && !layout.focused_group().tabs.contains(&tab))
        {
            // A session already shown by another group: go there (and move the highlight), as NSH does.
            layout.activate(g, i);
        } else {
            layout.open(tab.clone());
        }
        self.ui.groups.synced = Some(tab);
    }

    // ---- commands ---------------------------------------------------------------------

    pub fn toast_too_small(&mut self) {
        let text = self.manifest.ui_text(
            "layout.too_small",
            &[
                ("cols", &groups::MIN_W.to_string()),
                ("rows", &groups::MIN_H.to_string()),
            ],
        );
        self.toast(Level::Warning, text);
    }

    fn toast_no_neighbour(&mut self, side: Side) {
        let text = self
            .manifest
            .ui_text("layout.no_neighbour", &[("direction", side.name())]);
        self.toast(Level::Info, text);
    }

    pub fn layout_split(&mut self, side: Side) {
        let Some(active) = self.active_tab() else {
            return;
        };
        match active.kind {
            Kind::Terminal => self.terminal_create(Placement::Split(side)),
            Kind::Browser => {}
            _ => {
                let area = self.tree_area();
                match self.ui.groups.layout.split(side, active.clone(), area) {
                    Ok(_) => self.ui.groups.synced = Some(active),
                    Err(LayoutError::TooSmall) => self.toast_too_small(),
                    Err(_) => {}
                }
            }
        }
        self.groups_sync_live();
    }

    pub fn layout_join(&mut self, side: Side) {
        let area = self.tree_area();
        match self.ui.groups.layout.join(side, area) {
            Ok(()) => {}
            Err(_) => self.toast_no_neighbour(side),
        }
        self.groups_sync_live();
        self.groups_focus_effects();
    }

    pub fn layout_focus(&mut self, side: Side) {
        let area = self.tree_area();
        if self.ui.groups.layout.focus_side(side, area).is_err() {
            self.toast_no_neighbour(side);
        }
        self.groups_sync_live();
        self.groups_focus_effects();
    }

    pub fn layout_move(&mut self, side: Side) {
        let area = self.tree_area();
        if self.ui.groups.layout.move_tab_side(side, area).is_err() {
            self.toast_no_neighbour(side);
        }
        self.groups_sync_live();
        self.groups_focus_effects();
    }

    /// Focus lands where the new group's tab wants it.
    fn groups_focus_effects(&mut self) {
        if self.ui.overlay.is_some() || self.ui.dialog.is_some() {
            return;
        }
        match self.active_tab().map(|t| t.kind) {
            None | Some(Kind::Session) => {
                if matches!(
                    self.ui.focus,
                    Focus::Timeline | Focus::Sidebar | Focus::Rail
                ) {
                    self.ui.focus = Focus::Composer;
                }
            }
            Some(Kind::File | Kind::Diff) => self.ui.focus = Focus::Timeline,
            _ => {}
        }
    }

    /// Step the focused group's tabs, whatever their kind.
    pub fn layout_step(&mut self, step: isize) {
        self.ui.groups.layout.step(step);
        self.groups_sync_live();
    }

    pub fn layout_open_to_side(&mut self) {
        // The selected sidebar row, in the group to the right.
        self.ui.groups.side_open = true;
        match self.ui.focus {
            Focus::Sidebar => {
                let i = self.cursor(self.ui.tab);
                self.sidebar_activate(i);
            }
            _ => {
                if let Some(tab) = self.active_tab() {
                    let area = self.tree_area();
                    if self.ui.groups.layout.open_to_side(tab, area).is_err() {
                        self.toast_too_small();
                    }
                }
            }
        }
        self.ui.groups.side_open = false;
        self.groups_sync_live();
    }

    /// Whether a layout term (`layout.can_join_right`, ...) holds.
    pub fn layout_term(&self, name: &str) -> Option<bool> {
        let area = self.tree_area();
        let side = |s: Side| self.ui.groups.layout.has_neighbour(s, area);
        Some(match name {
            "layout.tab_active" => self.active_tab().is_some(),
            "layout.split_multiple" => self.ui.groups.layout.group_count() > 1,
            "layout.can_join_right" | "layout.has_right" => side(Side::Right),
            "layout.can_join_down" | "layout.has_down" => side(Side::Down),
            "layout.has_left" => side(Side::Left),
            "layout.has_up" => side(Side::Up),
            _ => return None,
        })
    }

    /// The Ctrl+X leader keys of the layout commands.
    pub fn layout_leader(&self, key: &str) -> Option<String> {
        self.manifest
            .command
            .iter()
            .find(|c| {
                c.id.starts_with("layout.") && c.leader.iter().any(|l| l == key) && c.for_tui()
            })
            .map(|c| c.id.clone())
    }

    pub fn execute_layout(&mut self, id: &str) -> bool {
        match id {
            "layout.split_right" => self.layout_split(Side::Right),
            "layout.split_down" => self.layout_split(Side::Down),
            "layout.join_right" => self.layout_join(Side::Right),
            "layout.join_down" => self.layout_join(Side::Down),
            "layout.focus_left" => self.layout_focus(Side::Left),
            "layout.focus_right" => self.layout_focus(Side::Right),
            "layout.focus_up" => self.layout_focus(Side::Up),
            "layout.focus_down" => self.layout_focus(Side::Down),
            "layout.move_left" => self.layout_move(Side::Left),
            "layout.move_right" => self.layout_move(Side::Right),
            "layout.move_up" => self.layout_move(Side::Up),
            "layout.move_down" => self.layout_move(Side::Down),
            "layout.tab_close" => self.layout_close_active(),
            "layout.group_close" => self.layout_close_group(),
            "layout.open_to_side" => self.layout_open_to_side(),
            "layout.group_menu" => self.open_group_menu(self.ui.groups.layout.focused),
            _ => return false,
        }
        true
    }

    // ---- group menu -------------------------------------------------------------------

    pub fn open_group_menu(&mut self, group: GroupId) {
        self.ui.groups.layout.focus(group);
        self.groups_sync_live();
        self.open_palette(PaletteMode::GroupMenu);
    }

    /// The menu's rows: label, command. Join is absent when there is no neighbour.
    pub fn group_menu_items(&self) -> Vec<(String, String)> {
        let area = self.tree_area();
        let l = &self.ui.groups.layout;
        let text = |k: &str| self.manifest.ui_text(k, &[]);
        let mut items = vec![
            (text("layout.menu.split_right"), "layout.split_right"),
            (text("layout.menu.split_down"), "layout.split_down"),
        ];
        if l.has_neighbour(Side::Right, area) {
            items.push((text("layout.menu.join_right"), "layout.join_right"));
        }
        if l.has_neighbour(Side::Down, area) {
            items.push((text("layout.menu.join_down"), "layout.join_down"));
        }
        items.push((text("layout.terminal.split"), "terminal.new"));
        items.push((text("layout.menu.close_tab"), "layout.tab_close"));
        if l.group_count() > 1 {
            items.push((text("layout.menu.close_group"), "layout.group_close"));
        }
        items
            .into_iter()
            .map(|(label, id)| (label, id.to_owned()))
            .collect()
    }

    // ---- closing ------------------------------------------------------------------------

    pub fn layout_close_active(&mut self) {
        let g = self.ui.groups.layout.focused;
        let index = self.ui.groups.layout.focused_group().active;
        self.close_group_tab(g, index);
    }

    /// Close the focused group: its tabs go and its sibling takes the space. Unsaved files in it are asked
    /// about once for the group; a running terminal still asks before it is terminated.
    pub fn layout_close_group(&mut self) {
        let group = self.ui.groups.layout.focused;
        if self.ui.groups.layout.group_count() < 2 {
            return;
        }
        let Some(g) = self.ui.groups.layout.group(group).cloned() else {
            return;
        };
        let dirty: Vec<GTab> = g
            .tabs
            .iter()
            .filter(|t| self.tab_dirty_and_alone(group, t))
            .cloned()
            .collect();
        if dirty.is_empty() {
            self.layout_close_group_now(group);
            return;
        }
        let restore = self.restore_point();
        let save = self.manifest.ui_text("layout.unsaved.save", &[]);
        let title = if dirty.len() == 1 {
            self.manifest
                .ui_text("layout.unsaved.title", &[("name", base_name(&dirty[0].id))])
        } else {
            self.manifest
                .ui_text("layout.unsaved.group", &[("count", &dirty.len().to_string())])
        };
        self.ui.dialog = Some(Dialog {
            title,
            body: vec![self.manifest.ui_text("layout.unsaved.body", &[])],
            cancel: self.manifest.ui_text("layout.unsaved.cancel", &[]),
            confirm: save,
            danger: false,
            action: Confirmed::CloseDirtyGroup { group },
            button: 1,
            restore,
        });
    }

    /// A tab that a close would lose unsaved text for: a dirty file with no other tab of it.
    fn tab_dirty_and_alone(&self, group: GroupId, tab: &GTab) -> bool {
        tab.kind == Kind::File
            && self.file_dirty(&tab.id)
            && !self
                .ui
                .groups
                .layout
                .find(tab)
                .into_iter()
                .any(|(g, _)| g != group)
    }

    /// The group prompt's answer: 1 saves every dirty file then closes the group, 2 discards and closes.
    pub fn answer_close_group_dirty(&mut self, group: GroupId, button: usize) {
        match button {
            1 => {
                self.ui.groups.close_group_saving = Some(group);
                self.groups_continue_close();
            }
            2 => {
                let tabs = self
                    .ui
                    .groups
                    .layout
                    .group(group)
                    .map(|g| g.tabs.clone())
                    .unwrap_or_default();
                for tab in &tabs {
                    if self.tab_dirty_and_alone(group, tab) {
                        self.discard_file(&tab.id);
                    }
                }
                self.layout_close_group_now(group);
            }
            _ => {}
        }
    }

    /// Carry a group close forward: save its dirty files one at a time (each must be the live editor to be
    /// written), then close its terminals one at a time and its tabs.
    pub(super) fn groups_continue_close(&mut self) {
        if let Some(group) = self.ui.groups.close_group_saving {
            if self.ui.dialog.is_some() {
                return;
            }
            let Some(g) = self.ui.groups.layout.group(group).cloned() else {
                self.ui.groups.close_group_saving = None;
                return;
            };
            let next = g
                .tabs
                .iter()
                .find(|t| self.tab_dirty_and_alone(group, t))
                .cloned();
            let Some(tab) = next else {
                self.ui.groups.close_group_saving = None;
                self.layout_close_group_now(group);
                return;
            };
            if self.live_file_path().as_deref() != Some(tab.id.as_str()) {
                if let Some(index) = g.position(&tab) {
                    self.ui.groups.layout.activate(group, index);
                    self.groups_sync_live();
                }
                return; // the read lands, and the next pass saves it
            }
            if self.ui.file_edit.as_ref().is_some_and(|e| e.saving) {
                return;
            }
            if self.ui.file_edit.is_none() {
                return;
            }
            self.save_file();
            return;
        }
        let Some(group) = self.ui.groups.close_group_resume else {
            return;
        };
        if self.ui.dialog.is_some() {
            return;
        }
        let live: std::collections::HashSet<String> = self
            .ui
            .groups
            .layout
            .group(group)
            .map(|g| g.tabs.iter().map(|t| t.id.clone()).collect())
            .unwrap_or_default();
        self.ui.groups.close_group_asked.retain(|id| live.contains(id));
        self.layout_close_group_now(group);
    }

    /// Close the group's tabs, asking about a running terminal first and waiting for each terminal to go.
    fn layout_close_group_now(&mut self, group: GroupId) {
        if self.ui.groups.layout.group_count() < 2 {
            self.ui.groups.close_group_resume = None;
            self.ui.groups.close_group_asked.clear();
            return;
        }
        let Some(g) = self.ui.groups.layout.group(group).cloned() else {
            self.ui.groups.close_group_resume = None;
            self.ui.groups.close_group_asked.clear();
            return;
        };
        for tab in g.tabs.clone() {
            if !self.ui.groups.layout.contains(&tab) {
                continue;
            }
            if tab.kind == Kind::Terminal {
                if self.ui.groups.close_group_asked.contains(&tab.id) {
                    self.ui.groups.close_group_resume = Some(group);
                    return;
                }
                let running = self
                    .terminal_panel
                    .terminals
                    .iter()
                    .find(|t| t.id == tab.id)
                    .is_some_and(|t| t.status == "running");
                self.ui.groups.close_group_asked.insert(tab.id.clone());
                self.ui.groups.close_group_resume = Some(group);
                if running {
                    let restore = self.restore_point();
                    self.ui.dialog = Some(Dialog {
                        title: "Close running terminal?".into(),
                        body: vec![],
                        cancel: "Cancel".into(),
                        confirm: "Close terminal".into(),
                        danger: true,
                        action: Confirmed::CloseTerminal(tab.id.clone()),
                        button: 0,
                        restore,
                    });
                } else {
                    self.api(ApiCall::TerminalClose { id: tab.id.clone() });
                }
                return;
            }
            self.finish_close(group, &tab);
        }
        self.ui.groups.close_group_resume = None;
        self.ui.groups.close_group_asked.clear();
    }

    /// Close one tab, asking first for unsaved text or a running terminal.
    pub fn close_group_tab(&mut self, group: GroupId, index: usize) {
        let Some(tab) = self
            .ui
            .groups
            .layout
            .group(group)
            .and_then(|g| g.tabs.get(index))
            .cloned()
        else {
            return;
        };
        match tab.kind {
            Kind::File if self.file_dirty(&tab.id) => {
                let others = self
                    .ui
                    .groups
                    .layout
                    .find(&tab)
                    .into_iter()
                    .any(|(g, i)| (g, i) != (group, index));
                if !others {
                    // Bring the file to the front so Save writes it, then ask.
                    self.ui.groups.layout.activate(group, index);
                    self.groups_sync_live();
                    self.ask_close_dirty(group, tab);
                    return;
                }
            }
            Kind::Terminal => {
                let running = self
                    .terminal_panel
                    .terminals
                    .iter()
                    .find(|t| t.id == tab.id)
                    .is_some_and(|t| t.status == "running");
                if running {
                    let restore = self.restore_point();
                    self.ui.dialog = Some(Dialog {
                        title: "Close running terminal?".into(),
                        body: vec![],
                        cancel: "Cancel".into(),
                        confirm: "Close terminal".into(),
                        danger: true,
                        action: Confirmed::CloseTerminal(tab.id.clone()),
                        button: 0,
                        restore,
                    });
                } else {
                    self.api(ApiCall::TerminalClose { id: tab.id.clone() });
                }
                return;
            }
            _ => {}
        }
        self.finish_close(group, &tab);
    }

    /// Remove one tab; the resource goes with its last tab.
    pub fn finish_close(&mut self, group: GroupId, tab: &GTab) {
        let Some(index) = self
            .ui
            .groups
            .layout
            .group(group)
            .and_then(|g| g.position(tab))
        else {
            return;
        };
        self.ui.groups.layout.close_tab(group, index);
        let remains = self.ui.groups.layout.contains(tab);
        if !remains {
            match tab.kind {
                Kind::Session => {
                    let viewed = self.viewed_root();
                    self.ui.tabs.retain(|t| t != &tab.id);
                    // The viewed session closing leaves the next tab to show.
                    self.move_off_closed(viewed.as_deref().filter(|v| *v == tab.id));
                    self.ui.groups.synced = None;
                    self.ui.groups.stale = true;
                }
                Kind::File => {
                    if self.live_file_path().as_deref() == Some(tab.id.as_str()) {
                        self.ui.file_open = false;
                        self.ui.file_edit = None;
                    }
                    self.ui.groups.buffers.remove(&tab.id);
                }
                Kind::Diff if diff_parts(tab).map(|(g, p)| (p, g)) == self.ui.diff_open.clone() => {
                    self.ui.diff_open = None;
                }
                _ => {}
            }
        }
        // The focused group's active tab may have changed.
        self.groups_sync_live();
        self.groups_focus_effects();
    }

    fn ask_close_dirty(&mut self, group: GroupId, tab: GTab) {
        let restore = self.restore_point();
        let name = self
            .manifest
            .ui_text("layout.unsaved.title", &[("name", base_name(&tab.id))]);
        let body = self.manifest.ui_text("layout.unsaved.body", &[]);
        self.ui.dialog = Some(Dialog {
            title: name,
            body: vec![body],
            cancel: self.manifest.ui_text("layout.unsaved.cancel", &[]),
            confirm: self.manifest.ui_text("layout.unsaved.save", &[]),
            danger: false,
            action: Confirmed::CloseDirty { group, tab },
            button: 1,
            restore,
        });
    }

    /// The unsaved prompt's answer: 1 save, 2 discard, anything else cancel.
    pub fn answer_close_dirty(&mut self, group: GroupId, tab: GTab, button: usize) {
        match button {
            1 => {
                self.ui.groups.close_after_save = Some((group, tab));
                self.save_file();
            }
            2 => {
                self.discard_file(&tab.id);
                self.finish_close(group, &tab);
            }
            _ => {}
        }
    }

    /// A save finished: close the tab that asked for it.
    pub fn groups_saved(&mut self, path: &str) {
        let Some((group, tab)) = self.ui.groups.close_after_save.clone() else {
            return;
        };
        if tab.id == path && !self.file_dirty(path) {
            self.ui.groups.close_after_save = None;
            self.finish_close(group, &tab);
        }
    }

    /// Close every tab of a session (or several): used when sessions are
    /// archived or deleted. The next tab of the focused group takes over.
    pub fn groups_close_sessions(&mut self, doomed: &[String]) {
        let viewed = self.viewed_root();
        for id in doomed {
            self.ui
                .groups
                .layout
                .remove_resource(&GTab::session(id.clone()));
        }
        self.ui.tabs.retain(|t| !doomed.contains(t));
        self.move_off_closed(viewed.as_deref().filter(|v| doomed.iter().any(|d| d == v)));
        self.ui.groups.synced = None;
        self.ui.groups.stale = true;
    }

    /// The viewed session lost its last tab: show another session's tab (the
    /// focused group's first), or the new-session screen.
    fn move_off_closed(&mut self, closed: Option<&str>) {
        if closed.is_none() {
            return;
        }
        let next = self
            .ui
            .groups
            .layout
            .focused_group()
            .tabs
            .iter()
            .chain(self.ui.groups.layout.tabs())
            .find(|t| t.kind == Kind::Session && !t.id.is_empty())
            .map(|t| t.id.clone());
        match next {
            Some(next) => self.open_session_live(&next),
            // The new-session screen has its own tab, as it had in the header.
            None => self.show_new_session(),
        }
    }

    // ---- terminals ------------------------------------------------------------------------

    /// `terminal.open`: the most recent terminal tab, or a new one in a split below.
    pub fn terminal_open_command(&mut self) {
        let recent = self
            .ui
            .groups
            .recent_terminal
            .clone()
            .map(GTab::terminal)
            .filter(|t| self.ui.groups.layout.contains(t))
            .or_else(|| {
                self.ui
                    .groups
                    .layout
                    .tabs()
                    .into_iter()
                    .rev()
                    .find(|t| t.kind == Kind::Terminal)
                    .cloned()
            });
        match recent {
            Some(tab) => {
                if let Some((g, i)) = self.ui.groups.layout.find(&tab).first().copied() {
                    self.ui.groups.layout.activate(g, i);
                }
                self.groups_sync_live();
                self.terminal_focused = true;
            }
            None => {
                let focused = self.ui.groups.layout.focused;
                self.terminal_create(Placement::Split(Side::Down));
                let _ = focused;
            }
        }
    }

    /// `terminal.new`: a tab beside the terminals of the focused group, else a
    /// split below it.
    pub fn terminal_new_command(&mut self) {
        let has = self
            .ui
            .groups
            .layout
            .focused_group()
            .tabs
            .iter()
            .any(|t| t.kind == Kind::Terminal);
        let placement = if has {
            Placement::Here(self.ui.groups.layout.focused)
        } else {
            Placement::Split(Side::Down)
        };
        self.terminal_create(placement);
    }

    pub fn terminal_create(&mut self, placement: Placement) {
        if self.terminal_panel.creating {
            return;
        }
        let (Some(project), Some(workspace)) = (self.current_project(), self.files_root()) else {
            return;
        };
        self.terminal_panel.creating = true;
        self.ui.groups.placements.push_back(placement);
        self.api(ApiCall::TerminalCreate {
            project,
            workspace,
            session: self.files_session(),
            client: self.terminal_panel.client_id.clone(),
        });
    }

    /// A terminal was created: give it its tab where it was asked for.
    pub fn terminal_created(&mut self, id: String) {
        let tab = GTab::terminal(id);
        let placement = self.ui.groups.placements.pop_front();
        let area = self.tree_area();
        let layout = &mut self.ui.groups.layout;
        match placement {
            Some(Placement::Split(side)) => {
                if layout.split(side, tab.clone(), area).is_err() {
                    // No room to split: it opens as a tab here instead.
                    layout.open(tab.clone());
                    self.toast_too_small();
                }
            }
            Some(Placement::Here(group)) => layout.open_in(group, tab.clone()),
            None => layout.open(tab.clone()),
        }
        self.ui.groups.synced = None;
        self.ui.groups.stale = true;
        self.groups_sync_live();
    }

    /// The terminal list arrived: drop tabs of terminals that are gone.
    pub fn terminals_listed(&mut self) {
        self.ui.groups.terminals_loaded = true;
        let alive: Vec<String> = self
            .terminal_panel
            .terminals
            .iter()
            .map(|t| t.id.clone())
            .collect();
        let gone: Vec<GTab> = self
            .ui
            .groups
            .layout
            .tabs()
            .into_iter()
            .filter(|t| t.kind == Kind::Terminal && !alive.contains(&t.id))
            .cloned()
            .collect();
        for tab in gone {
            self.ui.groups.layout.remove_resource(&tab);
        }
        self.ui.groups.synced = None;
        self.ui.groups.stale = true;
        self.groups_sync_live();
    }

    /// Select by resource id; false while the list does not have it.
    pub(super) fn terminal_select_id(&mut self, id: &str) -> bool {
        if self.unpark(id) {
            return true;
        }
        let Some(index) = self
            .terminal_panel
            .terminals
            .iter()
            .position(|t| t.id == id)
        else {
            return false;
        };
        self.terminal_select(index);
        self.terminal_panel.selected.as_deref() == Some(id)
    }

    /// Whether a group that is drawn shows this terminal as its active tab.
    fn terminal_visible(&self, id: &str) -> bool {
        let tab = GTab::terminal(id);
        let drawn = &self.ui.groups.rects;
        self.ui
            .groups
            .layout
            .groups()
            .into_iter()
            .filter(|g| drawn.is_empty() || drawn.iter().any(|(d, _)| *d == g.id))
            .any(|g| g.active_tab() == Some(&tab))
    }

    /// The attached terminal loses the keys: it keeps streaming into its own
    /// screen while a group still shows it, else it is detached.
    pub(super) fn park_selected(&mut self) {
        let Some(id) = self.terminal_panel.selected.take() else {
            return;
        };
        let generation = self.terminal_panel.generation;
        if self.terminal_visible(&id) {
            let rows_cols = self
                .terminal_panel
                .terminals
                .iter()
                .find(|t| t.id == id)
                .map_or((24, 80), |t| (t.rows, t.cols));
            let screen = std::mem::replace(
                &mut self.terminal_panel.screen,
                crate::terminal_screen::TerminalScreen::new(rows_cols.0, rows_cols.1),
            );
            self.terminal_panel
                .parked
                .push(crate::terminal_panel::Parked {
                    id,
                    generation,
                    attachment: self.terminal_panel.attachment.take(),
                    screen,
                    next_input: self.terminal_panel.next_input,
                });
        } else {
            self.effects.push(Effect::TerminalStop(generation));
        }
        self.terminal_panel.generation = self.terminal_panel.next_generation();
        self.terminal_panel.attachment = None;
        self.terminal_panel.input_queue.clear();
        self.terminal_panel.input_busy = false;
        self.terminal_panel.control_busy = false;
        self.terminal_panel.resize_busy = false;
        self.terminal_panel.reconnect_at = None;
        self.terminal_panel.size = None;
        self.terminal_panel.selection = None;
        self.terminal_selected = false;
        self.terminal_focused = false;
        self.terminal_controlled = false;
    }

    /// Bring a parked terminal back as the attached one and take control.
    fn unpark(&mut self, id: &str) -> bool {
        let Some(at) = self.terminal_panel.parked.iter().position(|p| p.id == id) else {
            return false;
        };
        if self.terminal_panel.selected.is_some() {
            self.park_selected();
        }
        let p = self.terminal_panel.parked.remove(at);
        let panel = &mut self.terminal_panel;
        panel.generation = p.generation;
        panel.attachment = p.attachment;
        panel.screen = p.screen;
        panel.next_input = p.next_input;
        panel.selected = Some(p.id);
        panel.size = None;
        panel.input_queue.clear();
        self.terminal_selected = true;
        self.terminal_focused = true;
        self.terminal_controlled = false;
        panel.focus = crate::terminal_panel::TerminalFocus::Screen;
        if self.terminal_panel.attachment.is_some() {
            self.execute("terminal.acquire");
        } else {
            self.terminal_panel.acquire_on_attach = true;
        }
        true
    }

    /// Forget a parked terminal (closed, or no longer shown).
    pub(super) fn drop_parked(&mut self, id: &str) {
        if let Some(at) = self.terminal_panel.parked.iter().position(|p| p.id == id) {
            let p = self.terminal_panel.parked.remove(at);
            self.effects.push(Effect::TerminalStop(p.generation));
        }
    }

    /// Every drawn terminal tab streams: the focused one is the attached
    /// terminal with the keys, the others are parked read-only. A terminal
    /// that is no longer drawn is detached.
    fn groups_sync_terminals(&mut self) {
        let focused = self.active_tab().filter(|t| t.kind == Kind::Terminal);
        if let Some(sel) = self.terminal_panel.selected.clone() {
            if focused.as_ref().map(|t| t.id.as_str()) != Some(sel.as_str()) {
                self.park_selected();
            }
        }
        let gone: Vec<String> = self
            .terminal_panel
            .parked
            .iter()
            .filter(|p| !self.terminal_visible(&p.id))
            .map(|p| p.id.clone())
            .collect();
        for id in gone {
            self.drop_parked(&id);
        }
        if !self.ui.groups.terminals_loaded {
            return;
        }
        let visible: Vec<String> = self
            .ui
            .groups
            .layout
            .groups()
            .into_iter()
            .filter(|g| {
                let drawn = &self.ui.groups.rects;
                drawn.is_empty() || drawn.iter().any(|(d, _)| *d == g.id)
            })
            .filter_map(|g| g.active_tab())
            .filter(|t| t.kind == Kind::Terminal)
            .map(|t| t.id.clone())
            .collect();
        for id in visible {
            let attached = self.terminal_panel.selected.as_deref() == Some(id.as_str())
                || self.terminal_panel.parked.iter().any(|p| p.id == id);
            // The focused tab attaches itself through `groups_sync_live`.
            if attached || focused.as_ref().map(|t| t.id.as_str()) == Some(id.as_str()) {
                continue;
            }
            let Some(res) = self.terminal_panel.terminals.iter().find(|t| t.id == id) else {
                continue;
            };
            let screen = crate::terminal_screen::TerminalScreen::new(res.rows, res.cols);
            let generation = self.terminal_panel.next_generation();
            self.terminal_panel
                .parked
                .push(crate::terminal_panel::Parked {
                    id: id.clone(),
                    generation,
                    attachment: None,
                    screen,
                    next_input: 1,
                });
            self.effects.push(Effect::TerminalAttach {
                id,
                client: self.terminal_panel.client_id.clone(),
                generation,
            });
        }
    }

    /// A stream event for a parked terminal. Returns whether it was one.
    pub(super) fn parked_event(&mut self, event: &crate::terminal_panel::TerminalEvent) -> bool {
        use crate::terminal_panel::TerminalEvent;
        let generation = match event {
            TerminalEvent::Attached { generation, .. }
            | TerminalEvent::Frame { generation, .. }
            | TerminalEvent::Ended { generation, .. } => *generation,
        };
        let Some(at) = self
            .terminal_panel
            .parked
            .iter()
            .position(|p| p.generation == generation)
        else {
            return false;
        };
        match event {
            TerminalEvent::Attached { attachment, .. } => {
                self.terminal_panel.parked[at].attachment = Some(attachment.clone());
            }
            TerminalEvent::Frame { frame, .. } => {
                let id = self.terminal_panel.parked[at].id.clone();
                if self.terminal_panel.parked[at].screen.apply(frame).is_err() {
                    self.drop_parked(&id);
                } else if frame["type"] == "exit" {
                    if let Some(item) = self
                        .terminal_panel
                        .terminals
                        .iter_mut()
                        .find(|t| t.id == id)
                    {
                        item.status = match frame["status"].as_str() {
                            Some("closed") => "closed",
                            Some("ended") => "ended",
                            _ => "exited",
                        }
                        .into();
                        item.controller = None;
                    }
                }
            }
            // A lost stream: it attaches again the next time it is visible.
            TerminalEvent::Ended { .. } => {
                let p = self.terminal_panel.parked.remove(at);
                self.effects.push(Effect::TerminalStop(p.generation));
            }
        }
        true
    }

    /// A terminal closed (or was found closed): its tabs go.
    pub fn terminal_gone(&mut self, id: &str) {
        self.ui.groups.layout.remove_resource(&GTab::terminal(id));
        if self.ui.groups.recent_terminal.as_deref() == Some(id) {
            self.ui.groups.recent_terminal = None;
        }
        self.ui.groups.synced = None;
        self.ui.groups.stale = true;
        self.groups_sync_live();
    }

    // ---- prefs ---------------------------------------------------------------------------

    /// The layouts to save: every other project's, and the current one.
    pub fn layouts_to_save(&self) -> BTreeMap<String, Layout> {
        let mut all = self.ui.groups.saved.clone();
        if let Some(key) = &self.ui.groups.key {
            all.insert(key.clone(), self.ui.groups.layout.clone());
        }
        all
    }

    pub fn load_layouts(&mut self, saved: BTreeMap<String, Layout>) {
        self.ui.groups.saved = saved;
    }

    // ---- pointer --------------------------------------------------------------------------

    /// The strip index a pointer at column `x` of `group`'s strip would drop at.
    fn strip_slot(&self, group: GroupId, x: u16) -> usize {
        let tabs: Vec<_> = self
            .ui
            .groups
            .tab_rects
            .iter()
            .filter(|(g, _, _)| *g == group)
            .collect();
        tabs.iter()
            .find(|(_, _, r)| x < r.x + r.width / 2)
            .map(|(_, i, _)| *i)
            .unwrap_or_else(|| tabs.last().map_or(0, |(_, i, _)| i + 1))
    }

    fn over_at(&self, x: u16, y: u16) -> Option<Over> {
        let (group, rect) = self
            .ui
            .groups
            .rects
            .iter()
            .find(|(_, r)| contains(*r, x, y))?;
        if y == rect.y {
            return Some(Over::Strip {
                group: *group,
                at: self.strip_slot(*group, x),
            });
        }
        let body = crate::layout::group_body(*rect);
        Some(Over::Body {
            group: *group,
            zone: groups::zone_at(body, x, y),
        })
    }

    pub fn group_tab_click(&mut self, group: GroupId, index: usize) {
        let was_active = self.ui.groups.layout.focused == group
            && self
                .ui
                .groups
                .layout
                .group(group)
                .is_some_and(|g| g.active == index);
        let tab = self
            .ui
            .groups
            .layout
            .group(group)
            .and_then(|g| g.tabs.get(index))
            .cloned();
        self.ui.view = View::Session;
        self.ui.groups.layout.activate(group, index);
        self.groups_sync_live();
        self.groups_focus_effects();
        if was_active && tab.is_some_and(|t| t.kind == Kind::Session && !t.id.is_empty()) {
            // The viewed tab's title renames the session, as the header tab did.
            if self.viewed().is_some_and(|v| v.parent_id.is_none()) && self.ui.view == View::Session
            {
                self.run_command("session.rename");
            }
        }
    }

    /// A click on a group's body or strip focuses the group.
    pub fn group_focus_click(&mut self, group: GroupId) {
        if self.ui.groups.layout.focused != group {
            self.ui.view = View::Session;
            self.ui.groups.layout.focus(group);
            self.groups_sync_live();
            self.groups_focus_effects();
        }
    }

    pub fn group_tab_close_click(&mut self, group: GroupId, index: usize) {
        self.close_group_tab(group, index);
    }

    pub fn group_new_session(&mut self, group: GroupId) {
        self.ui.groups.layout.focus(group);
        self.run_command("session.new");
    }

    pub(super) fn cancel_drag(&mut self) -> bool {
        if self.ui.groups.drag.take().is_some() {
            let text = self.manifest.ui_text("layout.drop.cancelled", &[]);
            self.toast(Level::Info, text);
            return true;
        }
        false
    }

    /// Pointer handling for tab and divider drags. Returns whether the event
    /// was taken.
    pub(super) fn groups_mouse(&mut self, ev: &MouseEvent) -> bool {
        if self.ui.overlay.is_some() || self.ui.dialog.is_some() || self.ui.edit.is_some() {
            if self.ui.groups.drag.take().is_some() {
                return false;
            }
            return false;
        }
        let (x, y) = (ev.column, ev.row);
        match ev.kind {
            MouseEventKind::Down(MouseButton::Left) => match self.hits.at(x, y).cloned() {
                Some(Target::GroupTab(g, i)) => {
                    self.ui.groups.drag = Some(Drag::Tab {
                        from: g,
                        index: i,
                        start: (x, y),
                        moved: false,
                        over: None,
                    });
                    true
                }
                Some(Target::GroupDivider(i)) => {
                    if let Some(d) = self.ui.groups.dividers.get(i).cloned() {
                        self.ui.groups.drag = Some(Drag::Divider { divider: d });
                    }
                    true
                }
                Some(Target::GroupBody(g)) => {
                    // Focus first; the click then continues to what is under it.
                    self.group_focus_click(g);
                    false
                }
                _ => false,
            },
            MouseEventKind::Drag(MouseButton::Left) => {
                let Some(drag) = self.ui.groups.drag.clone() else {
                    return false;
                };
                match drag {
                    Drag::Tab {
                        from,
                        index,
                        start,
                        moved,
                        ..
                    } => {
                        let moved = moved || (x, y) != start;
                        let over = if moved { self.over_at(x, y) } else { None };
                        self.ui.groups.drag = Some(Drag::Tab {
                            from,
                            index,
                            start,
                            moved,
                            over,
                        });
                    }
                    Drag::Divider { divider } => {
                        let area = self.tree_area();
                        let pos = if divider.dir == groups::Dir::Row {
                            x
                        } else {
                            y
                        };
                        self.ui.groups.layout.drag_divider(&divider, pos, area);
                    }
                }
                true
            }
            MouseEventKind::Up(MouseButton::Left) => {
                let Some(drag) = self.ui.groups.drag.take() else {
                    return false;
                };
                if let Drag::Tab {
                    from,
                    index,
                    moved,
                    over,
                    ..
                } = drag
                {
                    if !moved {
                        self.group_tab_click(from, index);
                    } else if let Some(over) = over {
                        let copy = ev.modifiers.contains(KeyModifiers::ALT);
                        let area = self.tree_area();
                        let layout = &mut self.ui.groups.layout;
                        let result = match over {
                            Over::Strip { group, at } => {
                                layout.move_tab_to(from, index, group, Some(at), copy)
                            }
                            Over::Body { group, zone } => {
                                layout.drop_on_body(from, index, group, zone, copy, area)
                            }
                        };
                        if result == Err(LayoutError::TooSmall) {
                            self.toast_too_small();
                        }
                        self.groups_sync_live();
                        self.groups_focus_effects();
                    }
                }
                true
            }
            _ => false,
        }
    }
}
