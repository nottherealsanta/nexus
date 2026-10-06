//! Settings: one page per area, never pages inside pages (plan §9.3).
use crate::ctx::*;
use crate::fixture::*;
use nexus_widgets::lists::*;
use nexus_widgets::theme::Level;
use nexus_widgets::*;
use ratatui::{layout::Rect, style::Style};

/// Cursor over a page's rows inside the scratch buffer.
/// Only these pages can differ per project; every other page is always global, so
/// it shows neither the Scope control nor scope badges (plan revision 2).
pub fn scoped(area: &str) -> bool {
    matches!(area, "skills" | "mcp")
}

pub struct Flow {
    pub x: u16,
    pub w: u16,
    pub y: u16,
}
impl Flow {
    pub fn new(r: Rect) -> Self {
        Self { x: r.x + 2, w: r.width.saturating_sub(3), y: r.y }
    }
    pub fn rect(&mut self, h: u16) -> Rect {
        let r = Rect::new(self.x, self.y, self.w, h);
        self.y += h;
        r
    }
    pub fn gap(&mut self) {
        self.y += 1;
    }
}


/// Row: label + select control. Opens a popup through `app` when activated.
fn select_row(buf: &mut ratatui::buffer::Buffer, ui: &mut Ui, f: &mut Flow, id: &str, label: &str, desc: &str, scope: &str, value: &str) {
    let r = f.rect(1 + (!desc.is_empty()) as u16);
    setting_row(buf, ui, r, id, label, desc, scope, 24, |b, ui, cr, _| {
        select(b, ui, cr.x, cr.y, cr.width, &format!("{id}:select"), value);
    });
}
fn toggle_row(buf: &mut ratatui::buffer::Buffer, ui: &mut Ui, f: &mut Flow, id: &str, label: &str, desc: &str, scope: &str, on: bool, locked: bool) {
    let r = f.rect(1 + (!desc.is_empty()) as u16);
    setting_row(buf, ui, r, id, label, desc, scope, TOGGLE_W, |b, ui, cr, foc| toggle_view(b, ui, cr.x + cr.width - TOGGLE_W, cr.y, on, locked, foc));
}
fn seg_row(buf: &mut ratatui::buffer::Buffer, ui: &mut Ui, f: &mut Flow, id: &str, label: &str, desc: &str, scope: &str, opts: &[&str], active: usize) {
    let r = f.rect(1 + (!desc.is_empty()) as u16);
    let w = segmented_width(opts);
    setting_row(buf, ui, r, id, label, desc, scope, w, |b, ui, cr, foc| {
        segmented_view(b, ui, cr.x + cr.width - w, cr.y, id, opts, active, if foc { Some(active) } else { None });
    });
}
fn step_row(buf: &mut ratatui::buffer::Buffer, ui: &mut Ui, f: &mut Flow, id: &str, label: &str, desc: &str, scope: &str, value: &str) {
    let r = f.rect(1 + (!desc.is_empty()) as u16);
    let w = stepper_width(value);
    setting_row(buf, ui, r, id, label, desc, scope, w, |b, ui, cr, _| {
        stepper(b, ui, cr.x + cr.width - w, cr.y, &format!("{id}:step"), value);
    });
}
fn kv_row(buf: &mut ratatui::buffer::Buffer, ui: &mut Ui, f: &mut Flow, id: &str, label: &str, value: &str, button_label: &str) {
    let r = f.rect(1);
    let bw = if button_label.is_empty() { 0 } else { button_width(button_label) };
    setting_row(buf, ui, r, id, label, "", "", bw.max(1), |b, ui, cr, _| {
        if bw > 0 {
            button(b, ui, cr.x + cr.width - bw, cr.y, &Button::new(&format!("{id}:btn"), button_label, ButtonKind::Secondary));
        }
    });
    let vx = 16u16.min(f.w / 3);
    put(buf, f.x + vx, r.y, &truncate(value, f.w.saturating_sub(vx + bw + 14) as usize, ui.glyphs.ellipsis), ui.theme.dim(), f.w);
}

pub fn draw(c: &mut Ctx, area: Rect) {
    let t = c.theme();
    let narrow = area.width < 90;
    let (inner, hint_row) = modal(c.buf, c.ui, area, &Modal { title: "Settings", id: "settings:close" });
    // header: search + scope
    let scope_w = segmented_width(&["Global", "Project"]);
    let sf = c.v.settings_search.clone();
    let sw = inner.width.saturating_sub(scope_w + 9).min(48);
    let r = search_field(c.buf, c.ui, Rect::new(inner.x + 1, inner.y, sw, 1), "settings:search", &sf, None);
    c.v.search_focus = r.focused;
    let has_scope = scoped(AREAS[c.v.area].0);
    if has_scope {
        put(c.buf, inner.x + inner.width - scope_w - 8, inner.y, "Scope", t.dim(), 6);
        segmented(c.buf, c.ui, inner.x + inner.width - scope_w - 1, inner.y, "settings:scope", &["Global", "Project"], c.w.scope);
    }
    let body = Rect::new(inner.x, inner.y + 2, inner.width, inner.height.saturating_sub(4));
    let sep = Style::default().fg(t.border);
    put(c.buf, inner.x, inner.y + 1, &c.ui.glyphs.rule.repeat(inner.width as usize), sep, inner.width);
    // description footer (focused element's full text) above the hint row
    let desc_row = Rect::new(inner.x + 1, inner.y + inner.height - 2, inner.width - 2, 1);
    let page_area;
    if narrow {
        // Area becomes a select at the top of the page (plan §9.11).
        put(c.buf, body.x + 2, body.y, "Area", t.dim(), 5);
        select(c.buf, c.ui, body.x + 8, body.y, 26, "settings:area-select", AREAS[c.v.area].1);
        page_area = Rect::new(body.x, body.y + 2, body.width, body.height.saturating_sub(2));
    } else {
        let nav_w = 24;
        draw_nav(c, Rect::new(body.x, body.y, nav_w, body.height));
        for y in body.y..body.y + body.height {
            put(c.buf, body.x + nav_w, y, if c.ui.glyphs.ascii { "|" } else { "│" }, sep, 1);
        }
        page_area = Rect::new(body.x + nav_w + 1, body.y, body.width - nav_w - 1, body.height);
    }
    if !c.v.settings_search.value.is_empty() {
        search_page(c, page_area);
        return key_hints(c.buf, c.ui, hint_row, &[("↑↓", "move"), ("Enter", "jump to setting"), ("Esc", "clear search")]);
    }
    let mut sc = c.v.page_scroll;
    scrolled(c.buf, c.ui, page_area, &mut sc, |buf, ui, r| {
        let mut f = Flow::new(r);
        page(buf, ui, c.w, c.v, &mut f, AREAS[c.v.area].0, r.width, c.state);
        f.y + 1
    });
    c.v.page_scroll = sc;
    // footer: where it's saved
    let saved = match AREAS[c.v.area].0 {
        "keyboard" => "Read-only: keys are defined in code.".to_string(),
        "agents" => "Saved in ~/.nexus/agents/ · global only".to_string(),
        "mcp" => "Saved in mcp.json".to_string(),
        a if scoped(a) && c.w.scope == 1 => "Saved in <workspace>/.agents/".to_string(),
        _ => "Saved in ~/.nexus/nexus.toml".to_string(),
    };
    put(c.buf, desc_row.x, desc_row.y, &truncate(&saved, desc_row.width as usize, c.ui.glyphs.ellipsis), t.dim(), desc_row.width);
    key_hints(c.buf, c.ui, hint_row, &[("↑↓", "move"), ("←→", "pane/value"), ("Space", "toggle"), ("Enter", "open"), ("Alt+↑↓", "reorder"), ("/", "search"), ("Ctrl+PgUp/Dn", "tab"), ("?", "keys"), ("Esc", "close")]);
}

fn draw_nav(c: &mut Ctx, r: Rect) {
    let t = c.theme();
    let mut y = r.y;
    let group = |c: &mut Ctx, y: &mut u16, title: &str| {
        put(c.buf, r.x + 2, *y, title, t.strong(Style::default().fg(t.muted).add_modifier(ratatui::style::Modifier::BOLD)), r.width - 2);
        *y += 1;
    };
    for (i, (key, label)) in AREAS.iter().enumerate() {
        if i == 0 {
            group(c, &mut y, "GENERAL");
        }
        if i == 3 {
            y += 1;
            group(c, &mut y, "CONFIGURE");
        }
        let id = format!("area:{key}");
        let rr = Rect::new(r.x, y, r.width, 1);
        let resp = c.ui.stop(&id, rr);
        let selected = i == c.v.area;
        let mut st = row_style(c.ui, &id, resp.focused, t.bg);
        if selected && !resp.focused {
            st = st.bg(t.element);
        }
        fill(c.buf, rr, st);
        if resp.focused {
            focus_bar(c.buf, c.ui, r.x, y, 1);
        }
        let lab = if selected { st.add_modifier(ratatui::style::Modifier::BOLD) } else { st };
        put(c.buf, r.x + 2, y, label, lab, r.width - 8);
        let count = match *key {
            "providers" => format!("{}/{}", c.w.providers.iter().filter(|p| p.conn == Conn::Connected).count(), c.w.providers.len()),
            "agents" => c.w.agents.len().to_string(),
            "tools" => c.w.families.iter().map(|f| f.tools.len()).sum::<usize>().to_string(),
            "mcp" => c.w.mcp.len().to_string(),
            "skills" => c.w.skills.len().to_string(),
            _ => String::new(),
        };
        if !count.is_empty() {
            put_right(c.buf, r.x + r.width - 1, y, &count, t.dim().bg(st.bg.unwrap_or(t.bg)));
        }
        y += 1;
    }
}

fn page(buf: &mut ratatui::buffer::Buffer, ui: &mut Ui, w: &World, v: &mut View, f: &mut Flow, area: &str, _width: u16, state: usize) {
    let _ = state;
    let title_st = ui.theme.strong(Style::default().fg(ui.theme.text).add_modifier(ratatui::style::Modifier::BOLD));
    let name = AREAS.iter().find(|a| a.0 == area).map(|a| a.1).unwrap_or("");
    put(buf, f.x, f.y, name, title_st, f.w);
    f.y += 1;
    if matches!(area, "voice" | "agents" | "mcp" | "tools") {
        f.y += 1;
    }
    match area {
        "appearance" => {
            intro_s(buf, ui, f, "How Nexus looks in this terminal.");
            seg_row(buf, ui, f, "set:theme", "Theme", "Dark, light, or follow the terminal.", "", &["Dark", "Light", "System"], w.theme);
            seg_row(buf, ui, f, "set:glyphs", "Glyphs", "ASCII is used automatically when TERM=linux.", "", &["Unicode", "ASCII"], w.ascii as usize);
            toggle_row(buf, ui, f, "set:motion", "Reduce motion", "No spinners, hover blend or toast hairline.", "", w.reduce_motion, false);
            toggle_row(buf, ui, f, "set:dense", "Dense transcript", "Smaller gaps between rows.", "", w.dense, false);
        }
        "layout" => {
            intro_s(buf, ui, f, "Which panels open on start and how toasts appear.");
            toggle_row(buf, ui, f, "set:sess-start", "Sessions sidebar on start", "Open the sessions sidebar when Nexus starts.", "", w.sessions_on_start, false);
            toggle_row(buf, ui, f, "set:det-start", "Details sidebar on start", "", "", w.details_on_start, false);
            toggle_row(buf, ui, f, "set:hints", "Show key hints", "A one-row hint bar under the composer.", "", w.key_hints, false);
            seg_row(buf, ui, f, "set:toastpos", "Toast position", "Toasts float and never move the transcript.", "", &["Top right", "Bottom right"], w.toast_top);
            step_row(buf, ui, f, "set:sidebar-w", "Sidebar width", "30–60 columns.", "", "44");
        }
        "keyboard" => keyboard(buf, ui, v, f),
        "providers" => providers(buf, ui, w, v, f),
        "models" => models(buf, ui, w, v, f),
        "agents" => agents(buf, ui, w, v, f),
        "tools" => tools(buf, ui, w, v, f),
        "mcp" => mcp(buf, ui, w, v, f),
        "skills" => skills(buf, ui, w, f),
        "voice" => voice(buf, ui, w, f),
        _ => {}
    }
}
fn intro_s(buf: &mut ratatui::buffer::Buffer, ui: &Ui, f: &mut Flow, text: &str) {
    put(buf, f.x, f.y, &truncate(text, f.w as usize, ui.glyphs.ellipsis), ui.theme.dim(), f.w);
    f.y += 2;
}
fn head(buf: &mut ratatui::buffer::Buffer, ui: &Ui, f: &mut Flow, text: &str) {
    let t = ui.theme;
    put(buf, f.x, f.y, text, t.strong(Style::default().fg(t.muted).add_modifier(ratatui::style::Modifier::BOLD)), f.w);
    f.y += 1;
}

fn models(buf: &mut ratatui::buffer::Buffer, ui: &mut Ui, w: &World, v: &mut View, f: &mut Flow) {
    intro_s(buf, ui, f, "The first connected model in each list runs; the rest are fallbacks, tried in order.");
    head(buf, ui, f, "DEFAULT");
    let r = f.rect(1);
    put(buf, r.x + 2, r.y, "Default model chain", ui.theme.dim(), r.width);
        let items: Vec<OrderedItem> = w.default_chain.iter().map(|m| OrderedItem { label: &m.label, note: if m.connected { "" } else { "not connected" } }).collect();
    let h = ordered_list_height(items.len());
    let r = f.rect(h);
    ordered_list(buf, ui, r, "chain", &items, "Add model…");
    seg_row(buf, ui, f, "set:effort", "Default reasoning effort", "", "", &EFFORTS, w.effort);
    toggle_row(buf, ui, f, "set:titles", "Session titles", "Name new sessions automatically.", "", w.title_on, false);
    select_row(buf, ui, f, "set:title-model", "Title model", "A tier or a specific model.", "", "quick tier");
    f.gap();
    head(buf, ui, f, "TIERS");
    let tabs_v: Vec<Tab> = TIERS.iter().enumerate().map(|(i, n)| Tab { label: n, badge: if w.tiers[i].iter().all(|m| !m.connected) { "!" } else { "" } }).collect();
    let r = f.rect(2);
    tabs(buf, ui, r, "tabs:tier", &tabs_v, v.tier_tab);
    let tier = &w.tiers[v.tier_tab];
    let items: Vec<OrderedItem> = tier.iter().map(|m| OrderedItem { label: &m.label, note: if m.connected { "" } else { "not connected" } }).collect();
    let r = f.rect(ordered_list_height(items.len()));
    ordered_list(buf, ui, r, &format!("tier:{}", v.tier_tab), &items, "Add model…");
    let users = ["quick, explore", "build, task, reviewer", "orchestrator, advisor"][v.tier_tab];
    let r = f.rect(1);
    put(buf, r.x + 4, r.y, &format!("Used by: {users} (agents)"), ui.theme.dim(), r.width);
    f.gap();
    head(buf, ui, f, "CATALOGUE");
    let r = f.rect(1);
    let label = if v.refreshing { "Refreshing…" } else { "Refresh" };
    let bw = button_width(label);
    put(buf, r.x + 2, r.y, "Model catalogue", ui.theme.text_style(), r.width);
    put(buf, r.x + 22, r.y, &format!("{} models · refreshed 3h ago", 412), ui.theme.dim(), r.width);
    let mut b = Button::new("catalogue:refresh", label, ButtonKind::Secondary);
    b.loading = v.refreshing;
    button(buf, ui, r.x + r.width - bw - 1, r.y, &b);
}

fn providers(buf: &mut ratatui::buffer::Buffer, ui: &mut Ui, w: &World, v: &mut View, f: &mut Flow) {
    intro_s(buf, ui, f, "Credentials stay in the daemon (~/.nexus/credentials.json). Connect more than one to fall back.");
    let r = f.rect(1);
    put(buf, r.x + r.width - 30, r.y, "Show", ui.theme.dim(), 5);
    segmented(buf, ui, r.x + r.width - 24, r.y, "seg:prov-show", &["All", "Connected"], 0);
    f.gap();
    let mut order: Vec<&Provider> = w.providers.iter().collect();
    order.sort_by_key(|p| (p.conn != Conn::Connected) as u8);
    for p in order {
        let id = format!("prov:{}", p.id);
        let open = v.open.contains(&id);
        let (summary, color) = match p.conn {
            Conn::Connected => (format!("{} connected · {} · {} models", ui.glyphs.dot_ok, p.auth, p.models), ui.theme.success),
            Conn::Error => (format!("{} error · {}", ui.glyphs.dot_err, p.auth), ui.theme.error),
            Conn::Off => (format!("{} not connected", ui.glyphs.dot_idle), ui.theme.muted),
            Conn::Down => (format!("{} {}", ui.glyphs.dot_idle, p.detail), ui.theme.muted),
        };
        let r = f.rect(1);
        section(buf, ui, r, &id, p.name, &summary, Some(color), open);
        if !open {
            continue;
        }
        if v.signing_in == Some(p.id) {
            let r = f.rect(1);
            spinner_row(buf, ui, Rect::new(r.x + 4, r.y, r.width - 20, 1), "Waiting for the browser…");
            button(buf, ui, r.x + r.width - 12, r.y, &Button::new(&format!("{id}:cancel"), "Cancel", ButtonKind::Secondary));
            continue;
        }
        if p.conn == Conn::Connected {
            if !p.account.is_empty() {
                kv_row(buf, ui, f, &format!("{id}:account"), "Account", p.account, "");
            }
            if !p.usage.is_empty() {
                kv_row(buf, ui, f, &format!("{id}:usage"), "Usage", p.usage, "Usage");
            }
            let r = f.rect(1);
            let mut x = r.x + 4;
            put(buf, r.x + 2, r.y, "Methods", ui.theme.dim(), 8);
            x += 9;
            for (k, (lab, kind)) in [("Sign in again", ButtonKind::Secondary), ("Use API key", ButtonKind::Secondary), ("Disconnect", ButtonKind::Danger)].iter().enumerate() {
                button(buf, ui, x, r.y, &Button::new(&format!("{id}:m{k}"), lab, *kind));
                x += button_width(lab) + 1;
            }
        } else {
            if !p.detail.is_empty() && p.conn == Conn::Error {
                let r = f.rect(1);
                callout(buf, ui, Rect::new(r.x + 3, r.y, r.width - 3, 1), &format!("{id}:fix"), Level::Warning, p.detail, "Sign in");
            }
            if let Some(entry) = v.key_entry.as_ref().filter(|_| p.id == "google") {
                let r = f.rect(1);
                put(buf, r.x + 2, r.y, "API key", ui.theme.dim(), 9);
                text_input(buf, ui, r.x + 12, r.y, 40, &TextInput { id: "google:key", state: entry, placeholder: "paste key, Enter saves", secret: true, error: "", editing: true });
            } else if p.conn != Conn::Down {
                let r = f.rect(1);
                put(buf, r.x + 2, r.y, "Methods", ui.theme.dim(), 8);
                let mut x = r.x + 13;
                for (k, (lab, kind)) in [("Sign in with browser", ButtonKind::Primary), ("Use a device code", ButtonKind::Secondary), ("Use API key", ButtonKind::Secondary)].iter().enumerate() {
                    button(buf, ui, x, r.y, &Button::new(&format!("{id}:m{k}"), lab, *kind));
                    x += button_width(lab) + 1;
                }
            }
        }
        f.gap();
    }
}

fn agents(buf: &mut ratatui::buffer::Buffer, ui: &mut Ui, w: &World, v: &mut View, f: &mut Flow) {
    select_row(buf, ui, f, "set:default-agent", "New sessions start with…", "", "", "build");
    f.gap();
    let top = f.y;
    let list_w = 24.min(f.w / 3);
    for (i, a) in w.agents.iter().enumerate() {
        let id = format!("agent:{}", a.name);
        let rr = Rect::new(f.x, f.y, list_w, 1);
        let resp = ui.stop(&id, rr);
        let st = row_style(ui, &id, resp.focused, ui.theme.bg);
        fill(buf, rr, if i == v.agent_sel && !resp.focused { st.bg(ui.theme.element) } else { st });
        if resp.focused {
            focus_bar(buf, ui, rr.x, rr.y, 1);
        }
        put(buf, rr.x + 2, rr.y, a.name, if i == v.agent_sel { st.add_modifier(ratatui::style::Modifier::BOLD) } else { st }, list_w - 2);
        f.y += 1;
    }
    f.y += 1;
    button(buf, ui, f.x + 1, f.y, &Button::new("agent:new", "+ New agent", ButtonKind::Secondary));
    let end = f.y + 1;
    let a = &w.agents[v.agent_sel];
    let dx = f.x + list_w + 3;
    let dw = f.w.saturating_sub(list_w + 3);
    let mut df = Flow { x: dx, w: dw, y: top };
    put(buf, dx, df.y, a.name, ui.theme.text_style().add_modifier(ratatui::style::Modifier::BOLD), dw);
    put_right(buf, dx + dw - 1, df.y, a.tag, ui.theme.dim());
    df.y += 2;
    let opts = [RadioOption { label: "Tier", detail: "picks a model from connected providers" }, RadioOption { label: "Specific model", detail: "ordered list with fallbacks" }];
    put(buf, dx + 2, df.y, "Runs on", ui.theme.dim(), 9);
    radio_group(buf, ui, Rect::new(dx + 10, df.y, dw.saturating_sub(10), 2), "agent:mode", &opts, if a.tier_mode { 0 } else { 1 });
    df.y += 2;
    if a.tier_mode {
        let r = df.rect(1);
        put(buf, r.x + 2, r.y, "Tier", ui.theme.dim(), 9);
        select(buf, ui, r.x + 10, r.y, 16, "agent:tier", TIERS[a.tier]);
    } else {
        let items = [OrderedItem { label: "anthropic/claude-sonnet-5-5", note: "" }, OrderedItem { label: "openai/gpt-6-mini", note: "" }];
        let r = df.rect(ordered_list_height(2));
        ordered_list(buf, ui, r, "agent:models", &items, "Add model…");
    }
    let r = df.rect(1);
    put(buf, r.x + 2, r.y, "Effort", ui.theme.dim(), 9);
    segmented(buf, ui, r.x + 10, r.y, "agent:effort", &EFFORTS, a.effort);
    let r = df.rect(1);
    put(buf, r.x + 2, r.y, "Tools", ui.theme.dim(), 9);
    put(buf, r.x + 10, r.y, &format!("{} of 24 on", a.tools_on), ui.theme.text_style(), 14);
    button(buf, ui, r.x + 26, r.y, &Button::new("agent:tools", "Choose…", ButtonKind::Secondary));
    let r = df.rect(1);
    put(buf, r.x + 2, r.y, "Prompt", ui.theme.dim(), 9);
    put(buf, r.x + 10, r.y, &truncate(&format!("{} · {} tok", a.prompt, a.tokens), dw.saturating_sub(24) as usize, ui.glyphs.ellipsis), ui.theme.text_style(), dw);
    button(buf, ui, r.x + dw.saturating_sub(8), r.y, &Button::new("agent:edit", "Edit", ButtonKind::Secondary));
    let r = df.rect(1);
    if a.tag.starts_with("built-in") {
        button(buf, ui, r.x + 10, r.y, &Button::new("agent:reset", "Reset to built-in", ButtonKind::Danger));
    }
    f.y = end.max(df.y + 1);
}

fn tools(buf: &mut ratatui::buffer::Buffer, ui: &mut Ui, w: &World, v: &mut View, f: &mut Flow) {
    intro_s(buf, ui, f, "Choose which tools this session can call. Locked after the first turn.");
    for fam in &w.families {
        let on = fam.tools.iter().filter(|t| t.on).count();
        let state = if on == fam.tools.len() { Check::On } else if on == 0 { Check::Off } else { Check::Mixed };
        let fid = format!("fam:{}", fam.name);
        let open = !v.open.contains(&format!("closed:{fid}"));
        let tok: u32 = fam.tools.iter().filter(|t| t.on).map(|t| t.tokens).sum();
        let r = f.rect(1);
        checkbox(buf, ui, Rect::new(r.x, r.y, r.width.saturating_sub(24), 1), &fid, &format!("{} {}", if open { ui.glyphs.open } else { ui.glyphs.closed }, fam.name), state);
        put_right(buf, r.x + r.width - 1, r.y, &format!("{on}/{} on · ~{tok} tok", fam.tools.len()), ui.theme.dim());
        if open {
            for t in &fam.tools {
                let id = format!("tool:{}", t.name);
                let r = f.rect(1);
                let resp = ui.stop(&id, r);
                let st = row_style(ui, &id, resp.focused, ui.theme.bg);
                fill(buf, r, st);
                if resp.focused {
                    focus_bar(buf, ui, r.x, r.y, 1);
                }
                put(buf, r.x + 5, r.y, t.name, if resp.focused { st.add_modifier(ratatui::style::Modifier::BOLD) } else { st }, 22);
                put(buf, r.x + 28, r.y, &format!("~{} tok", t.tokens), ui.theme.dim().bg(st.bg.unwrap()), 12);
                if t.locked {
                    put(buf, r.x + 42, r.y, "locked after the first turn", ui.theme.dim().bg(st.bg.unwrap()), 30);
                }
                put_right(buf, r.x + r.width - 11, r.y, "session", ui.theme.dim().fg(scope_color(ui, "session")).bg(st.bg.unwrap()));
                toggle_view(buf, ui, r.x + r.width - TOGGLE_W - 1, r.y, t.on, t.locked, resp.focused);
            }
        }
    }
}

fn mcp(buf: &mut ratatui::buffer::Buffer, ui: &mut Ui, w: &World, v: &mut View, f: &mut Flow) {
    for m in &w.mcp {
        let id = format!("mcp:{}", m.name);
        let open = v.open.contains(&id);
        let (g, c) = dot(ui, m.dot);
        let summary = format!("{g} {} · {} tools · ~{} tok · {}", m.status, m.tools, m.tokens, m.scope);
        let color = match m.dot { Dot::Ok => ui.theme.success, Dot::Err => ui.theme.error, _ => ui.theme.muted };
        let _ = c;
        let r = f.rect(1);
        section(buf, ui, r, &id, m.name, &summary, Some(color), open);
        if open {
            toggle_row(buf, ui, f, &format!("{id}:on"), "Enabled", "", "", m.enabled, false);
            seg_row(buf, ui, f, &format!("{id}:load"), "Tool loading", if m.eager { "eager: every tool schema is in the prompt" } else { "search: tools are found on demand" }, "", &["search", "eager"], m.eager as usize);
            let r = f.rect(1);
            put(buf, r.x + 2, r.y, "Command", ui.theme.dim(), 9);
            put(buf, r.x + 12, r.y, &truncate(m.cmd, r.width.saturating_sub(14) as usize, ui.glyphs.ellipsis), ui.theme.text_style(), r.width);
            let r = f.rect(1);
            let mut x = r.x + 12;
            for (k, (l, kind)) in [("Restart", ButtonKind::Secondary), ("Edit config", ButtonKind::Secondary), ("Remove", ButtonKind::Danger)].iter().enumerate() {
                button(buf, ui, x, r.y, &Button::new(&format!("{id}:a{k}"), l, *kind));
                x += button_width(l) + 1;
            }
            f.gap();
        }
    }
    let r = f.rect(1);
    button(buf, ui, r.x + 1, r.y, &Button::new("mcp:add", "+ Add server", ButtonKind::Primary));
}

fn skills(buf: &mut ratatui::buffer::Buffer, ui: &mut Ui, w: &World, f: &mut Flow) {
    intro_s(buf, ui, f, "Enter opens a read-only preview of SKILL.md.");
    for s in &w.skills {
        let r = f.rect(2);
        let id = format!("skill:{}", s.name);
        setting_row(buf, ui, r, &id, s.name, s.desc, if w.scope == 0 { "global" } else { "project" }, TOGGLE_W + 12, |b, ui, cr, foc| {
            put(b, cr.x, cr.y, &format!("~{} tok", s.tokens), ui.theme.dim(), 10);
            toggle_view(b, ui, cr.x + cr.width - TOGGLE_W, cr.y, s.on, false, foc);
        });
    }
    let r = f.rect(1);
    button(buf, ui, r.x + 1, r.y, &Button::new("skill:new", "+ New skill", ButtonKind::Secondary));
}

fn voice(buf: &mut ratatui::buffer::Buffer, ui: &mut Ui, w: &World, f: &mut Flow) {
    head(buf, ui, f, "VOICE INPUT · local dictation");
    toggle_row(buf, ui, f, "set:voice", "Voice input", "", "", w.voice_on, false);
    toggle_row(buf, ui, f, "set:autosend", "Send transcript automatically", "Off lets you review the transcript in the composer first.", "", w.auto_send, false);
    select_row(buf, ui, f, "set:device", "Processing device", "", "", DEVICES[w.device]);
    step_row(buf, ui, f, "set:limit", "Recording limit", "10–300 seconds.", "", &format!("{} s", w.limit));
    kv_row(buf, ui, f, "voice:model", "Model", &format!("{} ready · whisper-small · 179 MB", ui.glyphs.dot_ok), "Re-check");
    f.gap();
    head(buf, ui, f, "SPEECH · /speak, local Kokoro");
    select_row(buf, ui, f, "set:lang", "Language", "", "", "English (US)");
    select_row(buf, ui, f, "set:voice-name", "Voice", "", "", ["af_heart", "am_michael", "bf_emma"][w.speech_voice]);
    step_row(buf, ui, f, "set:speed", "Speed", "", "", &format!("{:.1}×", w.speech_speed as f32 / 10.0));
    if w.speech_downloaded {
        kv_row(buf, ui, f, "speech:model", "Model", &format!("{} ready · kokoro-82m", ui.glyphs.dot_ok), "Re-check");
    } else {
        kv_row(buf, ui, f, "speech:model", "Model", &format!("{} not downloaded · ~330 MB", ui.glyphs.dot_idle), "Download…");
    }
    let r = f.rect(1);
    button(buf, ui, r.x + 1, r.y, &Button::new("speech:reset", "Reset speech settings", ButtonKind::Ghost));
}

pub const KEYMAP: &[(&str, &str, &str)] = &[
    ("Sessions sidebar", "Ctrl+B", "Global"),
    ("Details sidebar", "Ctrl+L", "Global"),
    ("Next / previous pane", "F6 / Shift+F6", "Global"),
    ("Command palette", "Ctrl+P", "Global"),
    ("Context inspection", "Ctrl+I", "Global"),
    ("Usage", "Ctrl+U", "Global"),
    ("Model / agent / effort", "Ctrl+X M / A / E", "Global"),
    ("Dismiss all toasts", "Ctrl+X X", "Global"),
    ("Notifications", "Ctrl+X N", "Global"),
    ("Send", "Enter", "Composer"),
    ("Newline", "Shift+Enter", "Composer"),
    ("Queue message", "Ctrl+Enter", "Composer"),
    ("Dictation", "Ctrl+Space", "Composer"),
    ("Move / select", "↑ ↓ Home End", "Lists"),
    ("Search", "/", "Lists"),
    ("Type-ahead", "a–z", "Lists"),
    ("Move item", "Alt+↑ / Alt+↓", "Ordered lists"),
    ("Remove item", "Delete", "Ordered lists"),
    ("Switch tab", "Ctrl+PgUp / Ctrl+PgDn", "Tabs"),
    ("Jump to area", "Ctrl+1 … 9", "Settings"),
    ("Open in new tab", "o", "Sessions"),
    ("Rename / fork / archive", "r / f / a", "Sessions"),
    ("Close / back", "Esc", "Overlays"),
];

fn keyboard(buf: &mut ratatui::buffer::Buffer, ui: &mut Ui, v: &mut View, f: &mut Flow) {
    intro_s(buf, ui, f, "Read-only. Every key here comes from the same table the app uses.");
    let q = v.kb_filter.value.to_lowercase();
    let r = f.rect(1);
    let st = v.kb_filter.clone();
    search_field(buf, ui, Rect::new(r.x, r.y, 40.min(r.width), 1), "kb:search", &st, None);
    f.gap();
    let rows: Vec<Vec<String>> = KEYMAP.iter().filter(|k| q.is_empty() || format!("{} {} {}", k.0, k.1, k.2).to_lowercase().contains(&q)).map(|k| vec![k.0.into(), k.1.into(), k.2.into()]).collect();
    let r = f.rect(rows.len() as u16 + 2);
    table(buf, ui, r, "kb", &[("Action", 0), ("Keys", 24), ("Where", 14)], &rows, usize::MAX, 0);
}

/// Cross-area search results (plan §9.3.1).
pub fn search_page(c: &mut Ctx, area: Rect) {
    let q = c.v.settings_search.value.to_lowercase();
    let mut hits: Vec<(String, String, String)> = vec![];
    let add = |hits: &mut Vec<(String, String, String)>, area: &str, section: &str, row: &str| {
        if row.to_lowercase().contains(&q) || section.to_lowercase().contains(&q) {
            hits.push((area.into(), section.into(), row.into()));
        }
    };
    for (area_n, section, rows) in [
        ("Voice & speech", "Voice input", vec!["Voice input", "Send transcript automatically", "Processing device", "Recording limit"]),
        ("Voice & speech", "Speech", vec!["Language", "Voice", "Speed"]),
        ("Models", "Default", vec!["Default model chain", "Default reasoning effort", "Session titles", "Title model"]),
        ("Models", "Tiers", vec!["Low tier", "Medium tier", "High tier"]),
        ("Appearance", "Theme", vec!["Theme", "Glyphs", "Reduce motion"]),
        ("Layout", "Panels", vec!["Sessions sidebar on start", "Show key hints", "Toast position"]),
        ("Providers", "Connections", vec!["Anthropic", "OpenAI", "Google"]),
    ] {
        for row in rows {
            add(&mut hits, area_n, section, row);
        }
    }
    let rows: Vec<Vec<String>> = hits.iter().map(|h| vec![format!("{} › {}", h.0, h.1), h.2.clone()]).collect();
    put(c.buf, area.x + 2, area.y, &format!("{} results for “{}”", rows.len(), c.v.settings_search.value), c.theme().dim(), area.width);
    table(c.buf, c.ui, Rect::new(area.x, area.y + 2, area.width, area.height.saturating_sub(2)), "sresult", &[("Where", 30), ("Setting", 0)], &rows, 0, 0);
}
