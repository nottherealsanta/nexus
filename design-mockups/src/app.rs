//! Viewer state: screen/state selection, theme, size, keys and mouse.
use crate::ctx::Ctx;
use crate::fixture::*;
use crate::screens::{registry, ScreenDef};
use crate::{sessions, settings};
use nexus_widgets::focus::{escape, Escape, EscapeState};
use nexus_widgets::hit::Part;
use nexus_widgets::keys::{intent, Intent};
use nexus_widgets::theme::Level;
use nexus_widgets::*;
use ratatui::{
    buffer::Buffer,
    crossterm::event::{KeyCode, KeyEvent, KeyModifiers},
    layout::Rect,
    style::Style,
};
use std::time::Instant;

pub const SIZES: [(u16, u16); 4] = [(0, 0), (80, 24), (120, 36), (200, 50)];

pub struct App {
    pub w: World,
    pub v: View,
    pub toasts: ToastStack,
    pub screens: Vec<ScreenDef>,
    pub screen: usize,
    pub state: usize,
    pub theme_i: usize,
    pub size_i: usize,
    pub ascii: bool,
    pub debug: bool,
    pub focus: FocusRing,
    pub hits: HitMap,
    pub leader: bool,
    pub quit: bool,
    pub undo: Option<(String, usize, ModelRef)>,
    pub hovered_toast: Option<u64>,
    pub frame_area: Rect,
}

fn theme_of(i: usize) -> Theme {
    match i {
        0 => Theme::dark(),
        1 => Theme::light(),
        _ => Theme::mono(),
    }
}

impl App {
    pub fn new() -> Self {
        let mut a = Self {
            w: World::new(),
            v: View::new(),
            toasts: ToastStack::default(),
            screens: registry(),
            screen: 0,
            state: 0,
            theme_i: 0,
            size_i: 0,
            ascii: false,
            debug: false,
            focus: FocusRing::new(),
            hits: HitMap::default(),
            leader: false,
            quit: false,
            undo: None,
            hovered_toast: None,
            frame_area: Rect::default(),
        };
        a.apply_state();
        a
    }
    pub fn key_of(&self) -> &'static str {
        self.screens[self.screen].key
    }
    pub fn select(&mut self, key: &str, state: usize) -> bool {
        match self.screens.iter().position(|s| s.key == key) {
            Some(i) => {
                self.screen = i;
                self.state = state.min(self.screens[i].states.len() - 1);
                self.apply_state();
                true
            }
            None => false,
        }
    }
    /// Reset world and view to the preset of the current screen and state.
    pub fn apply_state(&mut self) {
        self.w = World::new();
        self.v = View::new();
        self.toasts = ToastStack::default();
        self.focus = FocusRing::new();
        self.undo = None;
        let key = self.key_of();
        let st = self.state;
        self.v.chat_state = 0;
        match key {
            "chat" => self.v.chat_state = st,
            "toasts" => {
                let s = &mut self.toasts;
                match st {
                    0 => {
                        s.push(Level::Success, "Saved", "~/.nexus/nexus.toml · [voice]", "", "");
                        s.push(Level::Info, "Copied 1,204 characters", "", "", "");
                        s.push(Level::Warning, "UI background actions busy", "Try again in a moment.", "", "");
                        s.push(Level::Error, "Could not reach OpenAI", "HTTP 503 · retrying in 8 s", "", "Details");
                    }
                    1 => {
                        for i in 0..6 {
                            s.push(Level::Info, &format!("Notification {}", i + 1), "", "", "");
                        }
                    }
                    2 => {
                        for _ in 0..2 {
                            s.push(Level::Info, "Copied 120 characters", "", "copy", "");
                        }
                    }
                    _ => {
                        s.push(Level::Warning, "Removed anthropic/claude-haiku-4-5", "from the Low tier", "", "Undo");
                    }
                }
            }
            "sessions" | "sessions-drawer" => {
                self.v.sidebar = true;
                match st {
                    1 => {
                        self.v.sess_search = TextState::new("refr");
                        self.focus.set("sessions:search");
                    }
                    2 => self.v.sess_filter = 2,
                    3 => {
                        self.v.rename = Some(TextState::new("Refresh docs for models"));
                        self.v.sess_sel = 1;
                    }
                    4 => {
                        self.v.selected.insert("s2".into());
                        self.v.selected.insert("s3".into());
                    }
                    5 => {
                        self.v.sess_filter = 2;
                        self.v.sess_search = TextState::new("zzz");
                    }
                    _ => {}
                }
                if self.focus.current().is_none() {
                    self.focus.set(&format!("session:{}", self.w.sessions[self.v.sess_sel].id));
                }
            }
            "model-picker" => {
                if st == 1 {
                    self.v.picker_q = TextState::new("son");
                }
                self.focus.set("picker:search");
            }
            "context-header" => {
                self.v.ctx_mode = match st { 1 => 2, 2 => 3, _ => 0 };
                self.focus.set("ctx:tools");
            }
            "palette" => self.focus.set("palette:search"),
            "confirm" => self.focus.set("confirm:cancel"),
            "settings-search" => {
                self.v.settings_search = TextState::new(["voice", "model"][st]);
                self.focus.set("settings:search");
            }
            k if k.starts_with("settings-") => {
                let area = &k["settings-".len()..];
                self.v.area = AREAS.iter().position(|a| a.0 == area).unwrap_or(0);
                self.focus.set(&format!("area:{area}"));
                match (area, st) {
                    ("providers", 1) => {
                        self.v.signing_in = Some("google");
                        self.v.open.insert("prov:google".into());
                    }
                    ("providers", 2) => {
                        self.v.key_entry = Some(TextState::new("sk-live-example"));
                        self.v.open.insert("prov:google".into());
                    }
                    ("models", s) if s < 3 => self.v.tier_tab = s,
                    ("models", _) => self.v.refreshing = true,
                    ("agents", 1) => self.w.agents[0].tier_mode = false,
                    ("agents", 2) => self.v.agent_sel = 5,
                    ("keyboard", 1) => self.v.kb_filter = TextState::new("tab"),
                    _ => {}
                }
            }
            _ => {}
        }
    }

    pub fn theme(&self) -> Theme {
        theme_of(self.theme_i)
    }
    pub fn glyphs(&self) -> Glyphs {
        if self.ascii || self.w.ascii {
            Glyphs::ascii()
        } else {
            Glyphs::unicode()
        }
    }

    /// Draw the current screen into `buf` (full terminal area `full`).
    pub fn render(&mut self, buf: &mut Buffer, full: Rect, now: Instant) {
        let theme = theme_of(self.theme_i);
        let glyphs = self.glyphs();
        let (sw, sh) = SIZES[self.size_i];
        let area = if sw == 0 {
            Rect::new(full.x, full.y, full.width, full.height.saturating_sub(1))
        } else {
            Rect::new(full.x, full.y, sw.min(full.width), sh.min(full.height.saturating_sub(1)))
        };
        self.frame_area = area;
        fill(buf, full, Style::default().bg(ratatui::style::Color::Reset));
        let mut ui = Ui::new(&theme, &glyphs);
        ui.now = now;
        ui.still = self.w.reduce_motion;
        ui.focus = std::mem::take(&mut self.focus);
        ui.begin_frame();
        let def_draw = self.screens[self.screen].draw;
        {
            let mut c = Ctx { buf, ui: &mut ui, area, w: &self.w, v: &mut self.v, toasts: &self.toasts, state: self.state };
            def_draw(&mut c);
        }
        if let Some(p) = &self.v.popup {
            if let Some(anchor) = ui.hits.rect_of(&p.id) {
                let opts: Vec<&str> = p.options.iter().map(|s| s.as_str()).collect();
                select_popup(buf, &mut ui, area, anchor, &format!("popup:{}", p.id), &opts, p.current, p.highlighted, 8);
            }
        }
        ui.end_frame();
        if self.debug {
            let ids = ui.focus.order().len();
            let cur = ui.focus.current().unwrap_or("-").to_string();
            let line = format!(" focus: {cur}  · {ids} stops · {} hit rects ", ui.hits.len());
            put(buf, area.x, area.y + area.height.saturating_sub(1), &line, Style::default().fg(theme.bg).bg(theme.warning), area.width);
        }
        self.focus = std::mem::take(&mut ui.focus);
        self.hits = std::mem::take(&mut ui.hits);
        // Viewer chrome: the last row of the terminal belongs to the viewer, not the design.
        let def = &self.screens[self.screen];
        let (fw, fh) = (area.width, area.height);
        let bar = format!(
            " {}/{} {} · {} ({}/{}) · {} · {}×{} · {}{}  F1 help ",
            self.screen + 1,
            self.screens.len(),
            def.key,
            def.states[self.state],
            self.state + 1,
            def.states.len(),
            theme.name(),
            fw,
            fh,
            if glyphs.ascii { "ascii" } else { "unicode" },
            if self.leader { " · Ctrl+X…" } else { "" },
        );
        let y = full.y + full.height.saturating_sub(1);
        fill(buf, Rect::new(full.x, y, full.width, 1), Style::default().fg(ratatui::style::Color::Black).bg(ratatui::style::Color::Gray));
        put(buf, full.x, y, &bar, Style::default().fg(ratatui::style::Color::Black).bg(ratatui::style::Color::Gray), full.width);
    }

    pub fn tick(&mut self, now: Instant) {
        if self.key_of() != "toasts" {
            self.toasts.tick(now, self.hovered_toast);
        }
    }

    fn toast(&mut self, level: Level, title: &str, body: &str) {
        self.toasts.push(level, title, body, "", "");
    }
    fn saved(&mut self) {
        let project = self.w.scope == 1 && matches!(self.key_of(), "settings-skills" | "settings-mcp");
        let path = if project { "<workspace>/.agents/" } else { "~/.nexus/nexus.toml" };
        self.toasts.push(Level::Success, "Saved", &format!("{path} (mock)"), "saved", "");
    }

    fn text_target(&mut self) -> Option<&mut TextState> {
        match self.focus.current()? {
            "sessions:search" => Some(&mut self.v.sess_search),
            "settings:search" => Some(&mut self.v.settings_search),
            "kb:search" => Some(&mut self.v.kb_filter),
            "picker:search" => Some(&mut self.v.picker_q),
            "palette:search" => Some(&mut self.v.palette_q),
            "google:key" => self.v.key_entry.as_mut(),
            _ => None,
        }
    }

    // ------------------------------------------------------------- input

    pub fn key(&mut self, k: KeyEvent) {
        let ctrl = k.modifiers.contains(KeyModifiers::CONTROL);
        match k.code {
            KeyCode::Char('q') if ctrl => return self.quit = true,
            KeyCode::F(1) => {
                self.toasts.push(Level::Info, "Viewer keys", "F2 screen · F3 state · F4 theme · F5 size · F7 ascii · F8 ids · Ctrl+Q quit", "help", "");
                return;
            }
            KeyCode::F(2) => return self.step_screen(1),
            KeyCode::BackTab if k.modifiers.contains(KeyModifiers::ALT) => return self.step_screen(-1),
            KeyCode::F(3) => {
                self.state = (self.state + 1) % self.screens[self.screen].states.len();
                return self.apply_state();
            }
            KeyCode::F(4) => return self.theme_i = (self.theme_i + 1) % 3,
            KeyCode::F(5) => return self.size_i = (self.size_i + 1) % SIZES.len(),
            KeyCode::F(7) => return self.ascii = !self.ascii,
            KeyCode::F(8) => return self.debug = !self.debug,
            _ => {}
        }
        if k.code == KeyCode::Char('x') && ctrl {
            self.leader = true;
            return;
        }
        if self.leader {
            self.leader = false;
            match k.code {
                KeyCode::Char('x') => self.toasts.dismiss_all(),
                KeyCode::Char('n') => {
                    self.select("notifications", 0);
                }
                KeyCode::Char('t') => self.run_undo(),
                _ => {}
            }
            return;
        }
        if self.v.popup.is_some() {
            return self.popup_key(k);
        }
        if self.text_target().is_some() {
            match k.code {
                KeyCode::Backspace => return self.text_target().unwrap().backspace(),
                KeyCode::Left => return self.text_target().unwrap().left(),
                KeyCode::Right => return self.text_target().unwrap().right(),
                KeyCode::Char(c) if !ctrl && !k.modifiers.contains(KeyModifiers::ALT) => {
                    self.text_target().unwrap().insert(c);
                    self.after_text();
                    return;
                }
                _ => {}
            }
        }
        let typing = self.text_target().is_some();
        let Some(i) = intent(k, typing) else { return };
        self.intent(i);
    }

    fn after_text(&mut self) {
        if self.focus.current() == Some("sessions:search") {
            self.v.sess_sel = 0;
        }
    }

    fn step_screen(&mut self, d: i32) {
        let n = self.screens.len() as i32;
        self.screen = (self.screen as i32 + d).rem_euclid(n) as usize;
        self.state = 0;
        self.apply_state();
    }

    fn cur(&self) -> String {
        self.focus.current().unwrap_or("").to_string()
    }

    fn intent(&mut self, i: Intent) {
        let cur = self.cur();
        match i {
            Intent::Up | Intent::Down => {
                let d = if i == Intent::Up { -1 } else { 1 };
                if cur == "sessions:search" && d > 0 {
                    if let Some(first) = visible_first(&self.w, &self.v) {
                        self.focus.set(&first);
                    }
                    return;
                }
                if cur.starts_with("session:") && d < 0 && self.v.sess_sel == 0 {
                    self.focus.set("sessions:search");
                    return;
                }
                self.focus.move_by(d);
                self.after_move();
            }
            Intent::Next => self.focus.cycle(1),
            Intent::Prev => self.focus.cycle(-1),
            Intent::Home => {
                self.focus.first();
                self.after_move();
            }
            Intent::End => {
                self.focus.last();
                self.after_move();
            }
            Intent::PageDown | Intent::PageUp => {
                let d = if i == Intent::PageDown { 8 } else { -8 };
                self.focus.move_by(d);
                self.after_move();
            }
            Intent::Left => self.adjust(&cur, -1),
            Intent::Right => self.adjust(&cur, 1),
            Intent::Activate | Intent::Toggle => self.activate(&cur, None),
            Intent::NextTab => self.tab_step(1),
            Intent::PrevTab => self.tab_step(-1),
            Intent::MoveUp => self.reorder(&cur, -1),
            Intent::MoveDown => self.reorder(&cur, 1),
            Intent::Remove => self.remove(&cur),
            Intent::Search => {
                let id = if self.key_of().starts_with("settings") { "settings:search" } else if self.key_of().starts_with("sessions") || self.v.sidebar { "sessions:search" } else { "" };
                if !id.is_empty() {
                    self.focus.set(id);
                }
            }
            Intent::Help => {
                self.toasts.push(Level::Info, "Keys", "Move ↑↓ · Space toggle · Alt+↑↓ reorder · / search · Esc back", "help", "");
            }
            Intent::Escape => self.escape(),
            Intent::NextRegion | Intent::PrevRegion => {
                let target = if cur.starts_with("session") { "composer" } else { "sessions:search" };
                self.v.sidebar = true;
                self.focus.set(target);
            }
            Intent::Char(c) => self.char_key(c, &cur),
            Intent::DismissToasts => self.toasts.dismiss_all(),
        }
    }

    fn after_move(&mut self) {
        let cur = self.cur();
        if let Some(key) = cur.strip_prefix("area:") {
            if let Some(i) = AREAS.iter().position(|a| a.0 == key) {
                if i != self.v.area {
                    self.v.area = i;
                    self.v.page_scroll = Scroll::default();
                }
            }
        }
    }

    fn escape(&mut self) {
        let cur = self.cur();
        let s = EscapeState {
            popup_open: self.v.popup.is_some(),
            search_nonempty: self.text_target().map(|t| !t.value.is_empty()).unwrap_or(false),
            search_focused: self.text_target().is_some(),
            inner_focused: cur.contains(":select") || cur.starts_with("agent:") && cur.matches(':').count() > 1,
            overlay_open: self.key_of().starts_with("settings") || self.key_of() == "model-picker" || self.key_of() == "palette",
            in_sidebar: cur.starts_with("session"),
        };
        match escape(s) {
            Escape::ClosePopup => self.v.popup = None,
            Escape::ClearSearch => self.text_target().unwrap().clear(),
            Escape::LeaveSearch => {
                let next = if self.key_of().starts_with("settings") { format!("area:{}", AREAS[self.v.area].0) } else { "composer".into() };
                self.focus.set(&next);
            }
            Escape::LeaveInner => {
                let base = cur.split(":select").next().unwrap_or(&cur).to_string();
                self.focus.set(&base);
            }
            Escape::CloseOverlay => {
                let was = self.key_of();
                self.select("chat", 0);
                self.toast(Level::Info, "Closed", &format!("{was} closed; focus returns to the opener"));
            }
            Escape::SidebarToComposer => self.focus.set("composer"),
            Escape::Composer => self.toast(Level::Info, "Esc in the composer", "Existing behaviour: stop hint or interrupt"),
        }
    }

    fn char_key(&mut self, c: char, cur: &str) {
        if cur.starts_with("session:") {
            let id = cur["session:".len()..].to_string();
            match c {
                'r' => self.v.rename = Some(TextState::new(&self.w.sessions.iter().find(|s| s.id == id).map(|s| s.title.clone()).unwrap_or_default())),
                'a' => {
                    if let Some(s) = self.w.sessions.iter_mut().find(|s| s.id == id) {
                        s.archived = !s.archived;
                        let msg = if s.archived { "Archived" } else { "Unarchived" };
                        let t = s.title.clone();
                        self.toasts.push(Level::Success, msg, &t, "", "");
                    }
                }
                'o' => self.toast(Level::Info, "Opened in a new tab", "(mock)"),
                'f' => self.toast(Level::Info, "Forked", "(mock)"),
                ' ' => {}
                _ => {}
            }
        } else if cur.starts_with("area:") || cur.starts_with("set:") || cur.starts_with("prov:") {
            // type-ahead jumps to the next area/row whose id suffix starts with the letter
            let order: Vec<String> = self.focus.order().to_vec();
            let from = order.iter().position(|i| i == cur).unwrap_or(0);
            let n = order.len();
            if let Some(next) = (1..=n).map(|k| &order[(from + k) % n]).find(|i| i.split(':').nth(1).map(|s| s.starts_with(c)).unwrap_or(false)) {
                let next = next.clone();
                self.focus.set(&next);
                self.after_move();
            }
        }
    }

    fn tab_step(&mut self, d: i32) {
        match self.key_of() {
            "settings-models" => self.v.tier_tab = (self.v.tier_tab as i32 + d).rem_euclid(3) as usize,
            k if k.starts_with("sessions") || self.v.sidebar => self.v.sess_filter = (self.v.sess_filter as i32 + d).rem_euclid(3) as usize,
            _ => {}
        }
    }

    fn adjust(&mut self, id: &str, d: i32) {
        let n = |v: usize, len: usize| (v as i32 + d).rem_euclid(len as i32) as usize;
        let w = &mut self.w;
        match id {
            "set:theme" => {
                w.theme = n(w.theme, 3);
                self.theme_i = [0, 1, 0][w.theme];
                return self.saved();
            }
            "set:glyphs" => w.ascii = !w.ascii,
            "set:effort" => w.effort = n(w.effort, 3),
            "set:toastpos" => w.toast_top = n(w.toast_top, 2),
            "tabs:tier" => return self.tab_step(d),
            "sessions:filter" => return self.tab_step(d),
            "settings:scope" => w.scope = n(w.scope, 2),
            "set:limit" => w.limit = (w.limit as i32 + d * 10).clamp(10, 300) as u32,
            "set:speed" => w.speech_speed = (w.speech_speed as i32 + d).clamp(5, 20) as u32,
            "agent:effort" => {
                let a = &mut w.agents[self.v.agent_sel];
                a.effort = n(a.effort, 3);
            }
            _ if id.starts_with("mcp:") && id.ends_with(":load") => {
                let name = id.split(':').nth(1).unwrap();
                if let Some(m) = w.mcp.iter_mut().find(|m| m.name == name) {
                    m.eager = !m.eager;
                }
            }
            _ if id.starts_with("area:") => {
                if d > 0 {
                    // → moves focus into the page (first stop after the nav entries)
                    let first = self.focus.order().iter().find(|i| !i.starts_with("area:") && !i.starts_with("settings:")).cloned();
                    if let Some(f) = first {
                        self.focus.set(&f);
                    }
                }
                return;
            }
            _ if self.key_of().starts_with("settings") && !id.starts_with("settings:") && !id.is_empty() => {
                // ← from a plain page row returns to the area list (plan §9.3.2)
                if d < 0 {
                    let a = format!("area:{}", AREAS[self.v.area].0);
                    self.focus.set(&a);
                }
                return;
            }
            _ => return,
        }
        self.saved();
    }

    fn open_select(&mut self, id: &str) {
        let (options, current): (Vec<String>, usize) = match id {
            "set:device" => (DEVICES.iter().map(|s| s.to_string()).collect(), self.w.device),
            "set:title-model" => (vec!["quick tier".into(), "low tier".into(), "anthropic/claude-haiku-4-5".into()], 0),
            "set:lang" => (vec!["English (US)".into(), "English (UK)".into(), "Français".into()], 0),
            "set:voice-name" => (vec!["af_heart".into(), "am_michael".into(), "bf_emma".into()], self.w.speech_voice),
            "set:default-agent" => (self.w.agents.iter().map(|a| a.name.to_string()).collect(), 0),
            _ => return,
        };
        self.v.popup = Some(Popup { id: format!("{id}:select"), options, current, highlighted: current });
    }

    fn popup_key(&mut self, k: KeyEvent) {
        let Some(p) = self.v.popup.as_mut() else { return };
        match k.code {
            KeyCode::Up => p.highlighted = p.highlighted.saturating_sub(1),
            KeyCode::Down => p.highlighted = (p.highlighted + 1).min(p.options.len() - 1),
            KeyCode::Esc => self.v.popup = None,
            KeyCode::Enter => {
                let p = self.v.popup.take().unwrap();
                let base = p.id.trim_end_matches(":select").to_string();
                match base.as_str() {
                    "set:device" => self.w.device = p.highlighted,
                    "set:voice-name" => self.w.speech_voice = p.highlighted,
                    _ => {}
                }
                self.saved();
            }
            KeyCode::Char(c) => {
                let lc = c.to_ascii_lowercase();
                if let Some(i) = p.options.iter().position(|o| o.to_lowercase().starts_with(lc)) {
                    p.highlighted = i;
                }
            }
            _ => {}
        }
    }

    fn list_mut(&mut self, id: &str) -> Option<(&mut Vec<ModelRef>, usize, String)> {
        let mut parts = id.split(':');
        match parts.next()? {
            "chain" => {
                let i: usize = parts.next()?.parse().ok()?;
                Some((&mut self.w.default_chain, i, "Default chain".into()))
            }
            "tier" => {
                let t: usize = parts.next()?.parse().ok()?;
                let i: usize = parts.next()?.parse().ok()?;
                Some((&mut self.w.tiers[t], i, format!("{} tier", TIERS[t])))
            }
            _ => None,
        }
    }

    fn reorder(&mut self, id: &str, d: i32) {
        let Some((list, i, name)) = self.list_mut(id) else { return };
        let j = i as i32 + d;
        if j < 0 || j as usize >= list.len() {
            return;
        }
        list.swap(i, j as usize);
        let prefix = id.rsplit_once(':').unwrap().0.to_string();
        self.focus.set(&format!("{prefix}:{j}"));
        self.toasts.push(Level::Success, &format!("{name} order saved"), "The first connected model runs", "order", "");
    }

    fn remove(&mut self, id: &str) {
        let key = id.to_string();
        let Some((list, i, name)) = self.list_mut(id) else { return };
        if i >= list.len() {
            return;
        }
        let m = list.remove(i);
        let label = m.label.clone();
        self.undo = Some((key, i, m));
        self.toasts.push(Level::Warning, &format!("Removed {label}"), &format!("from the {name}"), "", "Undo");
    }

    fn run_undo(&mut self) {
        if let Some((key, i, m)) = self.undo.take() {
            if let Some((list, _, _)) = self.list_mut(&key) {
                let at = i.min(list.len());
                list.insert(at, m);
                self.toasts.push(Level::Success, "Restored", "", "", "");
            }
        }
    }

    pub fn activate(&mut self, id: &str, part: Option<&str>) {
        let w = &mut self.w;
        match id {
            "set:voice" => w.voice_on = !w.voice_on,
            "set:autosend" => w.auto_send = !w.auto_send,
            "set:motion" => w.reduce_motion = !w.reduce_motion,
            "set:dense" => w.dense = !w.dense,
            "set:sess-start" => w.sessions_on_start = !w.sessions_on_start,
            "set:det-start" => w.details_on_start = !w.details_on_start,
            "set:hints" => w.key_hints = !w.key_hints,
            "set:titles" => w.title_on = !w.title_on,
            "set:theme" | "set:glyphs" | "set:effort" | "set:toastpos" => return self.adjust(id, 1),
            "set:device" | "set:title-model" | "set:lang" | "set:voice-name" | "set:default-agent" => return self.open_select(id),
            _ if id.ends_with(":select") => return self.open_select(id.trim_end_matches(":select")),
            "catalogue:refresh" => {
                self.v.refreshing = !self.v.refreshing;
                return self.toast(Level::Info, "Refreshing the model catalogue", "412 models");
            }
            "speech:model" | "speech:model:btn" => {
                self.w.speech_downloaded = true;
                return self.toast(Level::Success, "Speech model ready", "kokoro-82m · 330 MB (mock download)");
            }
            _ if id.starts_with("popup:") => {
                // mouse click on a popup row: "popup:<select id>:<index>"
                return self.toast(Level::Info, "Picked", id);
            }
            _ if id.starts_with("tool:") => {
                let name = &id[5..];
                for f in w.families.iter_mut() {
                    for t in f.tools.iter_mut().filter(|t| t.name == name) {
                        if t.locked {
                            return self.toast(Level::Warning, "bash is locked", "Locked after the first turn of this session.");
                        }
                        t.on = !t.on;
                    }
                }
            }
            _ if id.starts_with("fam:") => {
                let name = &id[4..];
                let open = format!("closed:{id}");
                if part.is_some() || true {
                    if let Some(f) = w.families.iter_mut().find(|f| f.name == name) {
                        let all = f.tools.iter().all(|t| t.on || t.locked);
                        for t in f.tools.iter_mut().filter(|t| !t.locked) {
                            t.on = !all;
                        }
                    }
                }
                let _ = open;
            }
            _ if id.starts_with("skill:") && !id.ends_with("new") => {
                let name = &id[6..];
                if let Some(s) = w.skills.iter_mut().find(|s| s.name == name) {
                    s.on = !s.on;
                }
            }
            _ if id.starts_with("mcp:") && id.ends_with(":on") => {
                let name = id.split(':').nth(1).unwrap();
                if let Some(m) = w.mcp.iter_mut().find(|m| m.name == name) {
                    m.enabled = !m.enabled;
                }
            }
            _ if id.starts_with("mcp:") && id.matches(':').count() == 1 && id != "mcp:add" => {
                if !self.v.open.remove(id) {
                    self.v.open.insert(id.into());
                }
                return;
            }
            _ if id.starts_with("prov:") && id.matches(':').count() == 1 => {
                if !self.v.open.remove(id) {
                    self.v.open.insert(id.into());
                }
                return;
            }
            _ if id.starts_with("prov:") && id.ends_with(":cancel") => {
                self.v.signing_in = None;
                return;
            }
            _ if id.starts_with("prov:") && id.ends_with(":m0") && id.contains("google") => {
                self.v.signing_in = Some("google");
                return;
            }
            _ if id.starts_with("prov:") && id.ends_with(":m2") && id.contains("google") => {
                self.v.key_entry = Some(TextState::default());
                self.focus.set("google:key");
                return;
            }
            "google:key" => {
                self.v.key_entry = None;
                return self.toast(Level::Success, "Google connected", "API key sent to the daemon (never echoed)");
            }
            _ if id.starts_with("agent:") && id.matches(':').count() == 1 && id != "agent:new" => {
                if let Some(i) = w.agents.iter().position(|a| a.name == &id[6..]) {
                    self.v.agent_sel = i;
                }
                return;
            }
            "agent:mode" | "agent:mode:0" | "agent:mode:1" => {
                let a = &mut w.agents[self.v.agent_sel];
                a.tier_mode = !a.tier_mode;
            }
            _ if id.ends_with(":add") => {
                self.select_model_picker(id);
                return;
            }
            _ if id.starts_with("session:") => {
                let sid = &id[8..];
                if let Some(r) = self.v.rename.take() {
                    if let Some(s) = self.w.sessions.iter_mut().find(|s| s.id == sid) {
                        s.title = r.value.clone();
                    }
                    return self.toast(Level::Success, "Renamed", &r.value);
                }
                if matches!(part, Some(p) if p.starts_with("action:")) {
                    return self.toast(Level::Info, "Action", &format!("{} (mock)", part.unwrap()));
                }
                return self.toast(Level::Info, "Opened session", &format!("{sid} in this tab (mock)"));
            }
            _ if id.starts_with("ctx:") && id != "ctx:retry" => {
                return self.toast(Level::Info, "Inspect", &format!("{} opens the full dialog (mock)", &id[4..]));
            }
            "ctx:retry" => return self.toast(Level::Info, "Retrying context preview", "(mock)"),
            "sessions:new" => return self.toast(Level::Success, "New session", "started in ~/repos/nexus"),
            "sessions:search" => {
                self.focus.set("session:s1");
                return;
            }
            "sessions:empty" => {
                self.v.sess_filter = 0;
                self.v.sess_search = TextState::default();
                return;
            }
            "tabs:tier" | "sessions:filter" => return,
            "composer:ctl1" => {
                self.select_model_picker("");
                return;
            }
            _ => return self.toast(Level::Info, id, "(mock: no action wired)"),
        }
        self.saved();
    }

    fn select_model_picker(&mut self, from: &str) {
        let _ = from;
        self.select("model-picker", 0);
        self.toast(Level::Info, "Model picker", "Pick a model to add; focus returns to the list afterwards");
    }

    pub fn click(&mut self, x: u16, y: u16) {
        let Some((id, part)) = self.hits.at(x, y).map(|(i, p)| (i.to_string(), p.clone())) else { return };
        if let Some(n) = id.strip_prefix("toast:") {
            if let Ok(tid) = n.parse::<u64>() {
                match part {
                    Part::Named(ref p) if p == "close" => self.toasts.dismiss(tid),
                    Part::Named(ref p) if p == "action" => {
                        self.run_undo();
                        self.toasts.dismiss(tid);
                    }
                    _ => {}
                }
            }
            return;
        }
        self.focus.set(&id);
        let named = match &part {
            Part::Named(n) => Some(n.clone()),
            Part::Body => None,
        };
        if let Some(n) = &named {
            if let Some(i) = n.strip_prefix("tab:").and_then(|s| s.parse::<usize>().ok()) {
                if id == "tabs:tier" {
                    self.v.tier_tab = i;
                } else if id == "sessions:filter" {
                    self.v.sess_filter = i;
                }
                return;
            }
            if let (Some(i), true) = (n.strip_prefix("segment:").and_then(|s| s.parse::<usize>().ok()), true) {
                match id.as_str() {
                    "set:theme" => {
                        self.w.theme = i;
                        self.theme_i = [0, 1, 0][i];
                    }
                    "set:effort" => self.w.effort = i,
                    "set:toastpos" => self.w.toast_top = i,
                    "set:glyphs" => self.w.ascii = i == 1,
                    "settings:scope" => self.w.scope = i,
                    _ => {}
                }
                return;
            }
            if n == "up" || n == "down" {
                return self.reorder(&id, if n == "up" { -1 } else { 1 });
            }
            if n == "remove" {
                return self.remove(&id);
            }
            if n == "dec" || n == "inc" {
                let d = if n == "inc" { 1 } else { -1 };
                let base = id.trim_end_matches(":step").to_string();
                return self.adjust(&base, d);
            }
        }
        self.after_move();
        self.activate(&id, named.as_deref());
    }
}

fn visible_ids(w: &World, v: &View) -> Vec<String> {
    sessions::visible(w, v).iter().map(|s| s.id.clone()).collect()
}
fn visible_first(w: &World, v: &View) -> Option<String> {
    visible_ids(w, v).first().map(|i| format!("session:{i}"))
}

pub fn _unused() {
    let _ = settings::KEYMAP.len();
}
