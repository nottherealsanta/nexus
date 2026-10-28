//! The proposed shell (DESIGN_PLAN.md D02–D05, D11, D12): header band, left
//! sidebar, centre column with composer, right sidebar. Decision screens call
//! `Shell::draw` and paint their own centre content with the helpers below.
use crate::kit::{bold, italic, mid, segs_w, trunc, width, Seg, P};
use ratatui::style::Style;

#[derive(Clone, Copy, PartialEq)]
pub enum Tab {
    Sessions,
    Files,
    Projects,
}

#[derive(Clone, Copy, PartialEq)]
pub enum Busy {
    Idle,
    /// Running for N seconds.
    Running(u32),
    Waiting,
    Offline,
}

pub struct Shell {
    pub left: Option<Tab>,
    pub left_w: u16,
    pub right: bool,
    pub right_w: u16,
    pub crumbs: &'static [&'static str],
    pub busy: Busy,
    pub draft: &'static str,
    /// Draw the composer and location strip.
    pub composer: bool,
    pub composer_focus: bool,
    /// Index into SESSIONS of the open session (None: new-session view).
    pub open: Option<usize>,
    pub focus_row: Option<usize>,
    /// Child session title: the viewed tab reads `root › child`.
    pub child: Option<&'static str>,
}

impl Default for Shell {
    fn default() -> Self {
        Shell {
            left: Some(Tab::Sessions),
            left_w: 28,
            right: true,
            right_w: 30,
            crumbs: &["nexus-demo", "Refactor the loader"],
            busy: Busy::Idle,
            draft: "",
            composer: true,
            composer_focus: true,
            open: Some(0),
            focus_row: None,
            child: None,
        }
    }
}

/// The centre column's content box (inside the gutters) and the timeline's bottom.
#[derive(Clone, Copy)]
pub struct Centre {
    /// Column band (full centre width, used by full-bleed blocks like the user band).
    pub bx: u16,
    pub bw: u16,
    /// Text box inside the 2-cell gutters.
    pub x: u16,
    pub w: u16,
    pub top: u16,
    /// First row below the timeline (where the composer region starts).
    pub bottom: u16,
}

pub struct SessionRow {
    pub title: &'static str,
    pub agent: &'static str,
    pub place: &'static str,
    pub when: &'static str,
    pub mark: &'static str,
    pub children: u8,
}

pub const SESSIONS: &[SessionRow] = &[
    SessionRow { title: "Refactor the loader", agent: "build", place: "main", when: "now", mark: "", children: 0 },
    SessionRow { title: "Review the CJK width handling", agent: "orchestrate", place: "main", when: "3m", mark: "waiting", children: 2 },
    SessionRow { title: "Release notes for 0.10", agent: "plan", place: "docs/0.10", when: "1h", mark: "running", children: 0 },
    SessionRow { title: "Fix flaky test in sync", agent: "build", place: "main", when: "2h", mark: "error", children: 0 },
    SessionRow { title: "Spike: terminal harness", agent: "scout", place: "main", when: "1d", mark: "unread", children: 1 },
];

impl Shell {
    /// Paint the whole frame; returns the centre geometry for the caller's content.
    pub fn draw(&self, p: &mut P) -> Centre {
        let (w, h) = (p.w, p.h);
        let lw = if self.left.is_some() { self.left_w } else { 0 };
        let rw = if self.right { self.right_w } else { 0 };
        // Chrome surfaces: header band + sidebars form one frame around the page.
        p.fill(0, 0, w, 1, "surface.sidebar");
        if lw > 0 {
            p.fill(0, 1, lw, h - 1, "surface.sidebar");
        }
        if rw > 0 {
            p.fill(w - rw, 1, rw, h - 1, "surface.sidebar");
        }
        self.header(p, lw, rw);
        if let Some(tab) = self.left {
            match tab {
                Tab::Sessions => self.sessions(p, lw),
                Tab::Files => files(p, lw),
                Tab::Projects => projects(p, lw),
            }
            settings_entry(p, lw);
        }
        if rw > 0 {
            self.details(p, w - rw, rw);
        }
        let bx = lw;
        let bw = w - lw - rw;
        let bottom = if self.composer { h - 7 } else { h };
        let c = Centre { bx, bw, x: bx + 2, w: bw - 4, top: 1, bottom };
        if self.composer {
            self.composer_block(p, c);
        }
        c
    }

    fn header(&self, p: &mut P, lw: u16, rw: u16) {
        let t = p.t;
        let w = p.w;
        let toggle = if self.left.is_some() { t.g("left_open") } else { t.g("closed_box") };
        p.put(1, 0, &[(toggle, t.secondary())]);
        if let Some(tab) = self.left {
            // The three sidebar icons stay in the band over the sidebar; no label chip.
            let mut x = 4;
            for (k, g) in [(Tab::Sessions, "sessions"), (Tab::Files, "files"), (Tab::Projects, "projects")] {
                x = p.put(x, 0, &[(t.g(g), if k == tab { bold(t.primary()) } else { t.muted() })]);
                x += 2;
            }
        }
        // Session tabs start at the centre column's text edge: `{glyph} {title} ×`, one cell apart.
        let cx = lw + 2;
        let end = w - rw.max(4);
        let limit = end.saturating_sub(24).max(cx);
        if !self.crumbs.is_empty() {
            let vb = t.bg("surface.canvas");
            let mut x = cx;
            let tab_of = |i: usize| -> (&'static str, &'static str) { (SESSIONS[i].title, SESSIONS[i].mark) };
            let mut tabs: Vec<(&'static str, &'static str, bool)> = Vec::new();
            if let Some(o) = self.open {
                for i in 0..4 {
                    let (ti, mk) = tab_of(i);
                    tabs.push((ti, mk, i == o));
                }
            } else {
                for i in 0..3 {
                    let (ti, mk) = tab_of(i);
                    tabs.push((ti, mk, false));
                }
                tabs.push(("New session", "", true));
            }
            for (title, mark, viewed) in tabs {
                let (g, gs) = match mark {
                    "waiting" => (t.g("waiting"), t.accent()),
                    "running" => (t.g("running"), t.primary()),
                    "error" => (t.g("error"), t.fg("status.error")),
                    "unread" => (t.g("unread"), t.fg("status.info")),
                    _ => ("·", t.muted()),
                };
                let tt = trunc(title, if viewed { 26 } else { 18 });
                if viewed {
                    let mut segs: Vec<Seg> = vec![(" ", vb), (g, gs.patch(vb)), (" ", vb)];
                    match self.child {
                        Some(ch) => {
                            segs.push((title, t.muted().patch(vb)));
                            segs.push((" ", vb));
                            segs.push((t.g("crumb"), t.muted().patch(vb)));
                            segs.push((" ", vb));
                            segs.push((ch, bold(t.primary()).patch(vb)));
                        }
                        None => segs.push((&tt, bold(t.primary()).patch(vb))),
                    }
                    segs.push((" ×", t.muted().patch(vb)));
                    segs.push((" ", vb));
                    x = p.put_clip(x, 0, &segs, limit);
                } else {
                    x = p.put_clip(x, 0, &[(g, gs), (" ", t.muted()), (&tt, t.muted()), (" ×", t.muted())], limit);
                }
                x += 1;
            }
            p.put_clip(x, 0, &[("+", t.muted())], limit);
        }
        // Right block: run status over the centre's right edge, then bell and toggle at the far edge.
        let mut status: Vec<Seg> = Vec::new();
        let secs;
        match self.busy {
            Busy::Running(s) => {
                secs = format!(" {s}s");
                status.push((t.g("running"), t.primary()));
                status.push((" working", t.primary()));
                status.push((Box::leak(secs.into_boxed_str()), t.muted()));
            }
            Busy::Waiting => {
                status.push((t.g("waiting"), t.accent()));
                status.push((" waiting for you", t.accent()));
            }
            Busy::Offline => {
                status.push((t.g("error"), t.fg("status.error")));
                status.push((" offline", t.fg("status.error")));
            }
            Busy::Idle => {}
        }
        if !status.is_empty() {
            p.put_r(w - rw - 2, 0, &status);
        }
        let toggle_r = if self.right { t.g("right_open") } else { t.g("closed_box") };
        p.put_r(w - 1, 0, &[(t.g("unread"), t.fg("status.info")), (" 1", t.muted()), ("  ", t.muted()), (toggle_r, t.secondary())]);
    }

    fn sessions(&self, p: &mut P, lw: u16) {
        let t = p.t;
        p.put(2, 2, &[(t.g("search"), t.muted()), ("  Search sessions", t.muted())]);
        p.put_r(lw - 2, 2, &[("ctrl+k", t.muted())]);
        let mut y = 4;
        p.put(2, y, &[("NEXUS-DEMO", bold(t.muted()))]);
        y += 1;
        for (i, s) in SESSIONS.iter().enumerate() {
            if y + 2 >= p.h - 2 {
                break;
            }
            let open = self.open == Some(i);
            if open {
                p.fill(0, y, lw, 2, "interactive.selection");
            }
            if self.focus_row == Some(i) {
                p.put(0, y, &[(t.g("focus"), t.fg("interactive.focus"))]);
                p.put(0, y + 1, &[(t.g("focus"), t.fg("interactive.focus"))]);
            }
            let bgs = if open { t.bg("interactive.selection") } else { Style::default() };
            let (mark, ms) = match s.mark {
                "waiting" => (t.g("waiting"), t.accent()),
                "running" => (t.g("running"), t.primary()),
                "error" => (t.g("error"), t.fg("status.error")),
                "unread" => (t.g("unread"), t.fg("status.info")),
                _ => (" ", t.muted()),
            };
            p.put(2, y, &[(mark, ms.patch(bgs))]);
            let when_w = width(s.when);
            let title_max = lw - 4 - when_w - 3;
            let title = trunc(s.title, title_max);
            let ts = if open { bold(t.primary()) } else { t.primary() };
            p.put(4, y, &[(&title, ts.patch(bgs))]);
            p.put_r(lw - 2, y, &[(s.when, t.muted().patch(bgs))]);
            let mut l2: Vec<Seg> = vec![(s.agent, t.agent(s.agent).patch(bgs)), (" · ", t.muted().patch(bgs)), (t.g("branch"), t.muted().patch(bgs)), (" ", bgs), (s.place, t.muted().patch(bgs))];
            let kids;
            if s.children > 0 {
                kids = format!("{} {}", s.children, t.g("child"));
                p.put_r(lw - 2, y + 1, &[(&kids, t.muted().patch(bgs))]);
            }
            if segs_w(&l2) > lw - 8 {
                l2.truncate(1);
            }
            p.put(4, y + 1, &l2);
            y += 3;
        }
        p.put(4, y, &[("+ New session", t.muted())]);
    }

    fn details(&self, p: &mut P, x0: u16, rw: u16) {
        let t = p.t;
        let x = x0 + 2;
        let vx = x + 9;
        let end = x0 + rw - 2;
        let mut y = 2;
        let label = |p: &mut P, y: u16, s: &str| {
            p.put(x, y, &[(s, bold(t.muted()))]);
        };
        let kv = |p: &mut P, y: u16, k: &str, v: &[Seg]| {
            p.put(x, y, &[(k, t.muted())]);
            p.put_clip(vx, y, v, end);
        };
        if self.open.is_none() {
            label(p, y, "SESSION");
            p.put(x, y + 1, &[("Starts when you send", t.muted())]);
            label(p, y + 3, "GIT");
            p.put(x, y + 4, &[(t.g("branch"), t.muted()), (" main", t.primary()), ("  clean", t.muted())]);
            return;
        }
        label(p, y, "SESSION");
        y += 1;
        kv(p, y, "Agent", &[("Build", bold(t.agent("build")))]);
        kv(p, y + 1, "Model", &[("gpt-6.1-terra", t.primary())]);
        kv(p, y + 2, "Effort", &[("medium", t.primary())]);
        let (sg, ss, sw) = match self.busy {
            Busy::Running(_) => (t.g("running"), t.primary(), " working"),
            Busy::Waiting => (t.g("waiting"), t.accent(), " waiting"),
            Busy::Offline => (t.g("error"), t.fg("status.error"), " offline"),
            Busy::Idle => (t.g("done"), t.fg("status.success"), " idle"),
        };
        kv(p, y + 3, "Status", &[(sg, ss), (sw, t.primary())]);
        kv(p, y + 4, "Turns", &[("2", t.primary()), (" · 3 tools", t.muted())]);
        kv(p, y + 5, "Tools", &[("read 2 · bash 1", t.secondary())]);
        kv(p, y + 6, "Started", &[("3:40 PM", t.primary()), (" · 2m", t.muted())]);
        kv(p, y + 7, "Id", &[("ses_1a1…5a19", t.secondary())]);
        y += 9;
        label(p, y, "CONTEXT");
        y += 1;
        meter(p, x, y, end - x - 5, 0.41, "build");
        p.put_r(end, y, &[("41%", t.primary())]);
        kv(p, y + 1, "Used", &[("82k", t.primary()), (" of 200k", t.muted())]);
        kv(p, y + 2, "Tokens", &[("12k in · 2k out", t.secondary())]);
        kv(p, y + 3, "Cost", &[("$0.04", t.primary())]);
        y += 5;
        label(p, y, "GIT");
        y += 1;
        p.put(x, y, &[(t.g("branch"), t.muted()), (" main", t.primary()), ("  ↑1", t.muted())]);
        y += 2;
        p.put(x, y, &[("Unstaged", t.secondary())]);
        p.put_r(end, y, &[("2", t.muted())]);
        let files: &[(&str, &str, &str, &str)] = &[("M", "README.md", "+2", ""), ("M", "src/loader.py", "+12", "−3")];
        for (i, (l, f, a, d)) in files.iter().enumerate() {
            let yy = y + 1 + i as u16;
            p.put(x, yy, &[(l, t.fg("status.warning")), (" ", t.muted()), (&mid(f, 14), t.primary())]);
            p.put_r(end - 3, yy, &[(a, t.fg("diff.add"))]);
            p.put_r(end, yy, &[(d, t.fg("diff.del"))]);
        }
        y += 4;
        p.put(x, y, &[("Untracked", t.secondary())]);
        p.put_r(end, y, &[("1", t.muted())]);
        p.put(x, y + 1, &[("?", t.fg("status.success")), (" ", t.muted()), ("tests/test_cache.py", t.primary())]);
    }

    fn composer_block(&self, p: &mut P, c: Centre) {
        let t = p.t;
        let y0 = c.bottom + 1;
        let (x, w) = (c.x, c.w);
        // Card: half-row edges (▄ above, ▀ below) so the panel reads as a soft card.
        let pc = t.color("surface.panel");
        let edge = Style::default().fg(pc);
        if t.mode != crate::theme::Mode::Mono {
            p.hline(x, y0, w, if t.ascii { " " } else { "▄" }, edge);
            p.hline(x, y0 + 4, w, if t.ascii { " " } else { "▀" }, edge);
        } else {
            p.hline(x, y0, w, "─", t.hair());
            p.hline(x, y0 + 4, w, "─", t.hair());
        }
        p.fill(x, y0 + 1, w, 3, "surface.panel");
        let pb = t.bg("surface.panel");
        let placeholder = match self.busy {
            Busy::Offline => "Reconnecting to Core… your draft is kept",
            _ if self.open.is_none() => "Message to start the session…",
            _ => "Type a message…",
        };
        let ix = x + 3;
        if self.draft.is_empty() {
            // The cursor sits on the placeholder's first letter (old Nexus).
            let (first, rest) = placeholder.split_at(1);
            let cur = if self.composer_focus { Style::default().fg(t.color("surface.panel")).bg(t.color("text.primary")) } else { t.muted().patch(pb) };
            p.put(ix, y0 + 1, &[(first, cur), (rest, t.muted().patch(pb))]);
        } else {
            let cur = Style::default().fg(t.color("surface.panel")).bg(t.color("text.primary"));
            p.put(ix, y0 + 1, &[(self.draft, t.primary().patch(pb)), (" ", cur)]);
        }
        // Control row: Agent · model provider · effort | ╱ meter + context label.
        let cy = y0 + 3;
        p.put(ix, cy, &[("Build", bold(t.agent("build")).patch(pb)), (" · ", t.muted().patch(pb)), ("gpt-6.1-terra", t.primary().patch(pb)), (" codex", t.muted().patch(pb)), (" · ", t.muted().patch(pb)), ("medium", t.secondary().patch(pb))]);
        let right = x + w - 3;
        match self.busy {
            Busy::Running(_) => {
                p.put_r(right, cy, &[("esc", bold(t.secondary()).patch(pb)), (" stop", t.muted().patch(pb))]);
            }
            _ => {
                let lx = p.put_r(right, cy, &[("82k", t.secondary().patch(pb)), (" · 41%", t.muted().patch(pb))]);
                meter(p, lx - 12, cy, 10, 0.41, "build");
            }
        }
        // Project strip on the canvas: basename bold, path muted, branch, checkout kind.
        let ly = y0 + 5;
        p.put(x + 1, ly, &[("nexus-demo", bold(t.primary())), ("  ", t.muted()), ("~/repos/nexus-demo", t.muted()), ("  ", t.muted()), (t.g("branch"), t.muted()), (" main", t.secondary()), ("  ", t.muted()), ("checkout", t.muted())]);
        if self.busy == Busy::Idle && self.open.is_some() {
            p.put_r(x + w - 1, ly, &[("ctrl+p", t.secondary()), (" commands", t.muted())]);
        }
    }
}

/// Context meter (old Nexus): ten `╱` cells, filled in the agent colour over hairline cells.
pub fn meter(p: &mut P, x: u16, y: u16, w: u16, frac: f32, agent: &str) {
    let t = p.t;
    let filled = ((w as f32) * frac).ceil() as u16;
    let g = if t.ascii { "/" } else { "╱" };
    let bg = p.buf[(x, y)].bg;
    p.hline(x, y, filled, g, t.agent(agent).bg(bg));
    p.hline(x + filled, y, w - filled, g, t.hair().bg(bg));
}

fn settings_entry(p: &mut P, lw: u16) {
    let t = p.t;
    let y = p.h - 2;
    p.put(2, y, &[("Settings", t.secondary())]);
    p.put_r(lw - 2, y, &[("ctrl+,", t.muted())]);
}

fn files(p: &mut P, lw: u16) {
    let t = p.t;
    p.put(2, 2, &[(t.g("search"), t.muted()), ("  Find file", t.muted())]);
    p.put_r(lw - 2, 2, &[("ctrl+o", t.muted())]);
    p.put(2, 4, &[("SIDE-PROJECT", bold(t.muted()))]);
    p.put_r(lw - 2, 4, &[(t.g("branch"), t.muted()), (" main", t.muted())]);
    let rows: &[(u16, &str, &str, &str, bool)] = &[
        (0, "▾", "src", "", true),
        (1, " ", "main.rs", "", false),
        (1, " ", "new.rs", "A", false),
        (1, " ", "old.rs", "D", false),
        (0, "▸", "target", "", true),
        (0, " ", ".gitignore", "", false),
        (0, " ", "hello.txt", "", false),
        (0, " ", "README.md", "M", false),
    ];
    for (i, (depth, fold, name, git, dir)) in rows.iter().enumerate() {
        let y = 5 + i as u16;
        let sel = i == 7;
        if sel {
            p.fill(0, y, lw, 1, "interactive.selection");
            p.put(0, y, &[(t.g("focus"), t.fg("interactive.focus"))]);
        }
        let bgs = if sel { t.bg("interactive.selection") } else { Style::default() };
        let x = 2 + depth * 2;
        if *depth > 0 {
            p.put(2, y, &[(t.g("rail"), t.hair().patch(bgs))]);
        }
        let ignored = *name == "target";
        let ns = if ignored { t.muted() } else if *dir { bold(t.primary()) } else { t.primary() };
        p.put(x, y, &[(fold, t.muted().patch(bgs)), (" ", bgs), (name, ns.patch(bgs))]);
        let gs = match *git {
            "M" => t.fg("status.warning"),
            "A" => t.fg("status.success"),
            "D" => t.fg("status.error"),
            _ => t.muted(),
        };
        p.put_r(lw - 2, y, &[(git, gs.patch(bgs))]);
    }
}

fn projects(p: &mut P, lw: u16) {
    let t = p.t;
    p.put(2, 2, &[(t.g("search"), t.muted()), ("  Filter projects", t.muted())]);
    let rows: &[(&str, &str, &str, &str)] = &[("nexus-demo", "~/repos/nexus-demo", "4", "now"), ("side-project", "~/repos/side-project", "1", "1d"), ("nexus", "~/repos/nexus", "12", "3d")];
    let mut y = 4;
    for (i, (n, path, count, when)) in rows.iter().enumerate() {
        let cur = i == 0;
        if cur {
            p.fill(0, y, lw, 2, "interactive.selection");
        }
        let bgs = if cur { t.bg("interactive.selection") } else { Style::default() };
        p.put(2, y, &[(if i == 1 { t.g("waiting") } else { " " }, t.accent().patch(bgs)), (" ", bgs), (n, if cur { bold(t.primary()) } else { t.primary() }.patch(bgs))]);
        p.put_r(lw - 2, y, &[(when, t.muted().patch(bgs))]);
        p.put(4, y + 1, &[(&mid(path, lw - 12), t.muted().patch(bgs))]);
        let c = format!("{count} sessions");
        p.put_r(lw - 2, y + 1, &[(Box::leak(c.into_boxed_str()), t.muted().patch(bgs))]);
        y += 3;
    }
    p.put(4, y, &[("+ Add project", t.muted())]);
}

// ---------------------------------------------------------------- timeline helpers

/// User message band (D04): raised full-bleed band, rail, fold marker inside,
/// `#n · time` right. One padding row above and below. Returns rows used.
pub fn user_block(p: &mut P, c: Centre, y: u16, lines: &[&str], n: u8, time: &str, focused: bool, folded: bool) -> u16 {
    let t = p.t;
    let pad = if folded { 0 } else { 1 };
    let rows = lines.len() as u16 + pad * 2;
    let rb = t.bg("surface.raised");
    p.fill(c.bx, y, c.bw, rows, "surface.raised");
    let rail = if focused { t.fg("interactive.focus") } else { t.fg("text.muted") };
    p.vline(c.bx, y, rows, t.g("focus"), rail.patch(rb));
    let fy = y + pad;
    p.put(c.x, fy, &[(if folded { t.g("closed") } else { t.g("open") }, t.muted().patch(rb))]);
    let num = format!("#{n} · {time}");
    let right = c.bx + c.bw - 2;
    let numw = width(&num);
    for (i, l) in lines.iter().enumerate() {
        let limit = if i == 0 { right - numw - 2 } else { right };
        p.put_clip(c.x + 2, fy + i as u16, &[(l, t.primary().patch(rb))], limit);
    }
    p.put_r(right, fy, &[(Box::leak(num.into_boxed_str()), t.muted().patch(rb))]);
    if focused {
        p.put_r(right, fy + lines.len() as u16, &[("c", bold(t.secondary()).patch(rb)), (" copy", t.muted().patch(rb))]);
    }
    rows
}

/// Turn footer (D05): right-aligned, muted, agent in its colour.
pub fn footer(p: &mut P, c: Centre, y: u16, agent: &str, model: &str, segs: &[&str], status: &str) {
    let t = p.t;
    let mut v: Vec<Seg> = vec![(t.g("square"), t.agent(agent)), (" ", t.muted()), (agent_title(agent), bold(t.agent(agent))), (" · ", t.muted()), (model, t.secondary())];
    for s in segs {
        v.push((" · ", t.muted()));
        v.push((s, t.muted()));
    }
    let _ = status; // D05: no end-status word or glyph
    p.put_r(c.x + c.w, y, &v);
}

pub fn agent_title(a: &str) -> &'static str {
    match a {
        "build" => "Build",
        "orchestrate" => "Orchestrate",
        "plan" => "Plan",
        "scout" => "Scout",
        "quick" => "Quick",
        _ => "Agent",
    }
}

/// Agent prose, wrapped. Returns rows used.
pub fn prose(p: &mut P, c: Centre, y: u16, text: &str) -> u16 {
    let t = p.t;
    p.wrap(c.x, y, c.w, text, t.primary())
}

/// Prose line made of segments (inline code etc.).
pub fn prose_segs(p: &mut P, c: Centre, y: u16, segs: &[Seg]) {
    p.put_clip(c.x, y, segs, c.x + c.w);
}

/// Inline code style (D08): primary text on the panel surface; never accent.
pub fn code_style(p: &P) -> Style {
    p.t.primary().patch(p.t.bg("surface.panel"))
}

/// Reasoning row (D07): `▸ Thought for 2.1s` (first word bold muted) and an italic preview.
pub fn thought(p: &mut P, c: Centre, y: u16, summary: &str, secs: &str, open: bool) {
    let t = p.t;
    let fold = if open { t.g("open") } else { t.g("closed") };
    let x = p.put(c.x, y, &[(fold, t.muted()), (" ", t.muted()), ("Thought", bold(t.muted())), (" for ", t.muted()), (secs, t.muted())]);
    if !summary.is_empty() {
        p.put_clip(x, y, &[(" · ", t.muted()), (summary, italic(t.muted()))], c.x + c.w);
    }
}

/// Collapsed activity group header (D06).
pub fn group(p: &mut P, c: Centre, y: u16, summary: &[Seg], dur: &str, open: bool, focused: bool) {
    let t = p.t;
    if focused {
        p.put(c.x - 2, y, &[(t.g("focus"), t.fg("interactive.focus"))]);
    }
    let mut v: Vec<Seg> = vec![(if open { t.g("open") } else { t.g("closed") }, t.muted()), (" ", t.muted())];
    v.extend_from_slice(summary);
    p.put(c.x, y, &v);
    p.put_r(c.x + c.w, y, &[(dur, t.muted())]);
}

/// One tool member line inside an expanded or live group (D06).
pub fn tool(p: &mut P, c: Centre, y: u16, last: bool, verb: &str, arg: &str, summary: &[Seg], dur: &str, status: &str) {
    let t = p.t;
    let tree = if last { t.g("tree_end") } else { t.g("tree_mid") };
    let (g, gs) = match status {
        "done" => (tool_glyph(verb), t.secondary()),
        "error" => (t.g("error"), t.fg("status.error")),
        "denied" => (t.g("denied"), t.fg("status.error")),
        "waiting" => (t.g("waiting"), t.accent()),
        _ => (t.g("running"), t.primary()),
    };
    let mut v: Vec<Seg> = vec![(tree, t.hair()), (" ", t.muted()), (g, gs), (" ", t.muted()), (verb, bold(t.secondary())), (" ", t.muted()), (arg, t.secondary())];
    if !summary.is_empty() {
        v.push(("  ", t.muted()));
        v.extend_from_slice(summary);
    }
    p.put_clip(c.x + 2, y, &v, c.x + c.w - width(dur) - 1);
    p.put_r(c.x + c.w, y, &[(dur, t.muted())]);
}

/// Verb glyphs carried from old Nexus (`→ Read`, `← Edit`) plus the new tools.
pub fn tool_glyph(verb: &str) -> &'static str {
    match verb {
        "Read" => "→",
        "Edit" | "Write" | "Patch" => "←",
        "Shell" => "$",
        "Search" | "Glob" | "Grep" => "*",
        "Code" => "λ",
        "Ask" => "?",
        "Session" => "↳",
        "Fetch" | "Web" => "↗",
        _ => "·",
    }
}

// ---------------------------------------------------------------- modal, motion and rail helpers (appended)

/// Modal (D13): a borderless `surface.raised` block. Padding is 2 cells and one blank row; the bold title
/// is the first row inside with `esc` right-aligned on it. Hints go on the last row inside (`y + h - 2`).
pub fn modal(p: &mut P, x: u16, y: u16, w: u16, h: u16, title: &str, esc: bool) {
    let t = p.t;
    for yy in y..(y + h).min(p.h) {
        for xx in x..(x + w).min(p.w) {
            p.buf[(xx, yy)].reset();
        }
    }
    p.fill(x, y, w, h, "surface.raised");
    let rb = t.bg("surface.raised");
    if !title.is_empty() {
        p.put(x + 2, y + 1, &[(title, bold(t.primary()).patch(rb))]);
    }
    if esc {
        p.put_r(x + w - 2, y + 1, &[("esc", t.muted().patch(rb))]);
    }
}

/// Key hints on a modal's last row inside (row `y`), a count or note on the right.
pub fn hints(p: &mut P, x: u16, y: u16, w: u16, left: &[(&str, &str)], right: &str) {
    let t = p.t;
    let rb = t.bg("surface.raised");
    let mut v: Vec<Seg> = vec![];
    for (i, (k, wd)) in left.iter().enumerate() {
        if i > 0 {
            v.push((" · ", t.muted().patch(rb)));
        }
        v.push((k, bold(t.secondary()).patch(rb)));
        v.push((" ", rb));
        v.push((wd, t.muted().patch(rb)));
    }
    p.put(x + 2, y, &v);
    if !right.is_empty() {
        p.put_r(x + w - 2, y, &[(right, t.muted().patch(rb))]);
    }
}

/// Selection band with the focus bar at its left edge.
pub fn sel_row(p: &mut P, x: u16, y: u16, w: u16) {
    let t = p.t;
    p.fill(x, y, w, 1, "interactive.selection");
    if t.mode == crate::theme::Mode::Mono {
        p.tint(x, y, w, t.primary()); // an explicit fg keeps the SVG writer from merging reversed runs
    }
    p.put(x, y, &[(t.g("focus"), t.fg("interactive.focus").patch(t.bg("interactive.selection")))]);
}

/// Modal row (D13): bar at x+1, 2-cell mark slot at x+3, label at x+5, `right` right-aligned.
pub fn mrow(p: &mut P, x: u16, y: u16, w: u16, mark: &str, ms: Style, label: &[Seg], right: &[Seg], sel: bool) {
    let t = p.t;
    let bgs = if sel { t.bg("interactive.selection") } else { t.bg("surface.raised") };
    if sel {
        sel_row(p, x + 1, y, w - 2);
    }
    p.put(x + 3, y, &[(mark, ms.patch(bgs))]);
    let l: Vec<Seg> = label.iter().map(|(s, st)| (*s, st.patch(bgs))).collect();
    let r: Vec<Seg> = right.iter().map(|(s, st)| (*s, st.patch(bgs))).collect();
    p.put_clip(x + 5, y, &l, x + w - 4 - segs_w(&r));
    p.put_r(x + w - 3, y, &r);
}

/// A key chip: bold primary on `bg` (mono: `[y]`).
pub fn chip(p: &mut P, x: u16, y: u16, key: &str, bg: &str) -> u16 {
    let t = p.t;
    if t.mode == crate::theme::Mode::Mono {
        return p.put(x, y, &[("[", t.muted()), (key, bold(t.primary())), ("]", t.muted())]);
    }
    let b = t.bg(bg);
    p.put(x, y, &[(" ", b), (key, bold(t.primary()).patch(b)), (" ", b)])
}

/// Shimmer (D19): a sweep from muted to primary with smoothstep falloff, reach 4, 54 frames.
pub fn shimmer(p: &mut P, x: u16, y: u16, text: &str, frame: u32, extra: Style) -> u16 {
    let t = p.t;
    let n = text.chars().count() as f32;
    let pos = (frame % 54) as f32 / 54.0 * (n + 8.0) - 4.0;
    let mut cx = x;
    for (i, ch) in text.chars().enumerate() {
        let d = (i as f32 - pos).abs();
        let k = if d < 4.0 { let u = 1.0 - d / 4.0; u * u * (3.0 - 2.0 * u) } else { 0.0 };
        let st = Style::default().fg(t.mix("text.muted", "text.primary", k)).patch(extra);
        cx = p.put(cx, y, &[(&ch.to_string(), st)]);
    }
    cx
}

/// The shell with the fixture turn and the start of turn #2, as the base for modals and alerts.
pub fn backdrop(p: &mut P, s: &Shell) -> Centre {
    let c = s.draw(p);
    let y = crate::screens::common::turn_one(p, c, 2);
    user_block(p, c, y, &["Go ahead, but keep the change small.", "日本語のコメントも残してください。"], 2, "3:44 PM", false, false);
    c
}

impl Shell {
    /// Rail mode (D03): an 8-cell left rail of three-row items instead of the sessions list.
    pub fn draw_rail(&self, p: &mut P) -> Centre {
        let (w, h) = (p.w, p.h);
        let (lw, rw) = (8u16, if self.right { self.right_w } else { 0 });
        p.fill(0, 0, w, 1, "surface.sidebar");
        p.fill(0, 1, lw, h - 1, "surface.sidebar");
        if rw > 0 {
            p.fill(w - rw, 1, rw, h - 1, "surface.sidebar");
        }
        let bare = Shell { left: None, left_w: self.left_w, right: self.right, right_w: self.right_w, crumbs: self.crumbs, busy: self.busy, draft: self.draft, composer: self.composer, composer_focus: self.composer_focus, open: self.open, focus_row: self.focus_row, child: self.child };
        bare.header(p, lw, rw);
        let t = p.t;
        p.put(1, 0, &[(t.g("left_open"), t.secondary())]);
        p.put(3, 2, &[(t.g("search"), t.muted())]);
        let mut y = 4;
        for (i, s) in SESSIONS.iter().enumerate() {
            let open = self.open == Some(i);
            if open {
                p.fill(0, y, lw, 2, "interactive.selection");
            }
            if self.focus_row == Some(i) {
                p.vline(0, y, 2, t.g("focus"), t.fg("interactive.focus"));
            }
            let bgs = if open { t.bg("interactive.selection") } else { Style::default() };
            let (mark, ms) = match s.mark {
                "waiting" => (t.g("waiting"), t.accent()),
                "running" => (t.g("running"), t.primary()),
                "error" => (t.g("error"), t.fg("status.error")),
                "unread" => (t.g("unread"), t.fg("status.info")),
                _ => (" ", t.muted()),
            };
            let ini: String = s.title.split(' ').filter(|w| !["the", "for", "a", "of", "in"].contains(w)).take(2).filter_map(|w| w.chars().next()).collect::<String>().to_uppercase();
            p.put(1, y, &[(mark, ms.patch(bgs))]);
            p.put(3, y, &[(&ini, bold(t.agent(s.agent)).patch(bgs))]);
            p.put(3, y + 1, &[(s.when, t.muted().patch(bgs))]);
            y += 3;
        }
        p.put(3, y, &[("+", t.muted())]);
        p.put(2, h - 2, &[("Set", t.secondary())]);
        self.details_pub(p, w - rw, rw);
        let c = Centre { bx: lw, bw: w - lw - rw, x: lw + 2, w: w - lw - rw - 4, top: 1, bottom: if self.composer { h - 7 } else { h } };
        if self.composer {
            self.composer_block(p, c);
        }
        c
    }

    fn details_pub(&self, p: &mut P, x0: u16, rw: u16) {
        if rw > 0 {
            self.details(p, x0, rw);
        }
    }
}
