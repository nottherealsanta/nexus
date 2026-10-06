//! The context header that opens every conversation (plan §9.1, redesigned).
//!
//! Same look as today: `◈` in the block's colour, a bold title, a dot leader,
//! counts, right-aligned token estimates, one blank row between blocks, each block
//! one click/keyboard target. New: each block expands in place to show what it
//! actually holds (tools grid, skills with tokens, MCP servers with status).
//! Unavailable estimates show `—`, never an invented number.
use crate::ctx::*;
use crate::fixture::*;
use nexus_widgets::lists::*;
use nexus_widgets::*;
use ratatui::{layout::Rect, style::{Color, Modifier, Style}};

pub const BLOCKS: [&str; 5] = ["system", "agents", "tools", "skills", "mcp"];

pub fn id(b: &str) -> String {
    format!("ctx:{b}")
}

/// Tool grid columns by width (plan: 3/4/5, fewer when narrow), at most five rows.
pub fn tool_cols(width: u16) -> usize {
    if width < 70 { 2 } else if width < 90 { 3 } else if width < 130 { 4 } else { 5 }
}
const ROWS: usize = 5;

struct Block {
    key: &'static str,
    title: &'static str,
    color: Color,
    counts: String,
    tokens: Option<u32>,
}

fn fmt(n: u32) -> String {
    let s = n.to_string();
    let mut out = String::new();
    for (i, ch) in s.chars().enumerate() {
        if i > 0 && (s.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(ch);
    }
    out
}

fn blocks(c: &Ctx, loading: bool) -> Vec<Block> {
    let t = c.theme();
    let tools: Vec<&Tool> = c.w.families.iter().flat_map(|f| f.tools.iter()).collect();
    let on = tools.iter().filter(|t| t.on).count();
    let tool_tok: u32 = tools.iter().filter(|t| t.on).map(|t| t.tokens).sum();
    let sk_on = c.w.skills.iter().filter(|s| s.on).count();
    let sk_tok: u32 = c.w.skills.iter().filter(|s| s.on).map(|s| s.tokens).sum();
    let running = c.w.mcp.iter().filter(|m| m.enabled && m.dot == Dot::Ok).count();
    let mcp_tok: u32 = c.w.mcp.iter().filter(|m| m.enabled).map(|m| m.tokens).sum();
    let known = |n: u32| if loading { None } else { Some(n) };
    vec![
        Block { key: "system", title: "System prompt", color: t.purple, counts: "build · 2 sections".into(), tokens: known(2140) },
        Block { key: "agents", title: "AGENTS.md", color: t.blue, counts: "~/repos/nexus · 1 file".into(), tokens: known(3880) },
        Block { key: "tools", title: "Tools", color: t.cyan, counts: format!("{} tools · {on} on", tools.len()), tokens: known(tool_tok) },
        Block { key: "skills", title: "Skills", color: t.accent, counts: format!("{} skills · {sk_on} on", c.w.skills.len()), tokens: known(sk_tok) },
        Block { key: "mcp", title: "MCP", color: t.success, counts: format!("{} servers · {running} running", c.w.mcp.len()), tokens: known(mcp_tok) },
    ]
}

/// Draw the header at `area`; returns the rows used. `mode`: 0 normal, 2 loading, 3 error.
pub fn draw(c: &mut Ctx, area: Rect, mode: usize) -> u16 {
    let t = c.theme();
    let g = c.ui.glyphs;
    let x = area.x + 4;
    let w = area.width.saturating_sub(8);
    let loading = mode == 2;
    let mut y = area.y;
    let end = area.y + area.height;
    let mut total = 0u32;
    let mut total_known = !loading && mode != 3;
    if mode == 3 {
        callout(c.buf, c.ui, Rect::new(x, y, w, 1), "ctx:retry", nexus_widgets::theme::Level::Error, "Context preview failed: provider catalogue unavailable. Counts below come from the session.", "Retry");
        y += 2;
    }
    for b in blocks(c, loading) {
        let bid = id(b.key);
        let open = c.v.open.contains(&bid) && mode != 3;
        let content = if open { content_rows(c, b.key, w) } else { 0 };
        let h = 1 + content;
        if y + h > end {
            break;
        }
        let rect = Rect::new(area.x, y, area.width, h);
        let r = c.ui.stop(&bid, rect);
        let hover = c.ui.hover_of(&bid);
        let bg = if r.focused { t.focus_bg } else { nexus_widgets::theme::mix(t.bg, t.element_hi, hover * 0.6) };
        fill(c.buf, rect, Style::default().bg(bg).fg(t.text));
        if r.focused {
            focus_bar(c.buf, c.ui, area.x, y, 1);
        }
        // Heading: ◈ Title ········ counts          tokens
        let chev = if open { g.open } else { g.closed };
        put(c.buf, area.x + 2, y, chev, Style::default().fg(t.quiet).bg(bg), 1);
        put(c.buf, x, y, "◈", Style::default().fg(b.color).bg(bg), 1);
        let tw = put(c.buf, x + 2, y, b.title, Style::default().fg(t.text).bg(bg).add_modifier(Modifier::BOLD), 24);
        let tok = match b.tokens {
            Some(n) => { total += n; format!("{} tok", fmt(n)) }
            None => { total_known = false; "— tok".into() }
        };
        let right = x + w;
        let counts_w = width(&b.counts) as u16;
        put_right(c.buf, right, y, &tok, t.dim().bg(bg));
        let counts_x = right.saturating_sub(tok.chars().count() as u16 + 3 + counts_w);
        let lead_from = x + 2 + tw + 1;
        if counts_x > lead_from + 2 {
            put(c.buf, lead_from, y, &"·".repeat((counts_x - lead_from - 1) as usize), Style::default().fg(t.border_strong).bg(bg), counts_x - lead_from);
            put(c.buf, counts_x, y, &b.counts, t.dim().bg(bg), counts_w);
        } else {
            put(c.buf, lead_from, y, &"·".repeat(2), Style::default().fg(t.border_strong).bg(bg), 2);
        }
        if loading {
            put(c.buf, x + 3 + tw + 1, y, &format!("{} estimating", c.ui.spinner()), t.dim().bg(bg), 16);
        }
        if open {
            draw_content(c, b.key, Rect::new(x + 2, y + 1, w.saturating_sub(2), content), bg);
        }
        y += h + 1; // one blank row between blocks, as today
    }
    let foot = if total_known { format!("Context total · ~{} tokens", fmt(total)) } else { "Context total · tokens unavailable".into() };
    if y < end {
        put_right(c.buf, x + w, y, &foot, t.dim());
        y += 1;
    }
    y - area.y
}

fn content_rows(c: &Ctx, key: &str, w: u16) -> u16 {
    match key {
        "system" | "agents" => 1,
        "tools" => {
            let n: usize = c.w.families.iter().map(|f| f.tools.len()).sum();
            let cols = tool_cols(w);
            let rows = n.div_ceil(cols).min(ROWS);
            rows as u16 + 1
        }
        "skills" => c.w.skills.len().div_ceil(2).min(ROWS) as u16,
        "mcp" => c.w.mcp.len().min(5) as u16,
        _ => 0,
    }
}

fn draw_content(c: &mut Ctx, key: &str, r: Rect, bg: Color) {
    let t = c.theme();
    let g = c.ui.glyphs;
    let ell = g.ellipsis;
    let base = Style::default().fg(t.text).bg(bg);
    let dim = t.dim().bg(bg);
    match key {
        "system" => {
            put(c.buf, r.x, r.y, &truncate("You are Nexus, a provider-agnostic coding agent working in the user's workspace. …", r.width as usize, ell), dim, r.width);
        }
        "agents" => {
            put(c.buf, r.x, r.y, &truncate("Guidance for coding agents working on Nexus: the context is clearly presented to the user and to agents. …", r.width as usize, ell), dim, r.width);
        }
        "tools" => {
            let all: Vec<&Tool> = c.w.families.iter().flat_map(|f| f.tools.iter()).collect();
            let cols = tool_cols(r.width + 2);
            let cap = cols * ROWS;
            let col_w = r.width / cols as u16;
            for (i, tool) in all.iter().take(cap).enumerate() {
                let (cx, cy) = (r.x + (i % cols) as u16 * col_w, r.y + (i / cols) as u16);
                let mut st = if tool.on { base } else { t.quiet_style().bg(bg).add_modifier(Modifier::CROSSED_OUT) };
                if tool.locked {
                    st = st.add_modifier(Modifier::ITALIC);
                }
                put(c.buf, cx, cy, &truncate(tool.name, col_w.saturating_sub(2) as usize, ell), st, col_w.saturating_sub(1));
            }
            let rows = all.len().min(cap).div_ceil(cols) as u16;
            let omitted = all.len().saturating_sub(cap);
            let on = all.iter().filter(|t| t.on).count();
            let mut foot = format!("{} tools · {on} on", all.len());
            if omitted > 0 {
                foot.push_str(&format!(" · {omitted} not shown"));
            }
            foot.push_str(" · off tools struck through · Enter inspects all");
            put(c.buf, r.x, r.y + rows, &truncate(&foot, r.width as usize, ell), dim, r.width);
        }
        "skills" => {
            let col_w = r.width / 2;
            for (i, s) in c.w.skills.iter().take(2 * ROWS).enumerate() {
                let (cx, cy) = (r.x + (i % 2) as u16 * col_w, r.y + (i / 2) as u16);
                let st = if s.on { base } else { t.quiet_style().bg(bg).add_modifier(Modifier::CROSSED_OUT) };
                let tok = format!("~{} tok", fmt(s.tokens));
                let name_w = col_w.saturating_sub(width(&tok) as u16 + 3);
                put(c.buf, cx, cy, &truncate(s.name, name_w as usize, ell), st, name_w);
                put_right(c.buf, cx + col_w - 2, cy, &tok, dim);
            }
        }
        "mcp" => {
            for (i, m) in c.w.mcp.iter().take(5).enumerate() {
                let y = r.y + i as u16;
                let d = if m.enabled { m.dot } else { Dot::Idle };
                let (gl, col) = dot(c.ui, d);
                put(c.buf, r.x, y, gl, Style::default().fg(col).bg(bg), 1);
                put(c.buf, r.x + 2, y, m.name, if m.enabled { base } else { dim }, 14);
                let detail = if !m.enabled {
                    "disabled".to_string()
                } else if m.dot == Dot::Err {
                    m.status.to_string()
                } else {
                    format!("{} tools · ~{} tok · {}", m.tools, fmt(m.tokens), if m.eager { "eager" } else { "search" })
                };
                let st = if m.dot == Dot::Err && m.enabled { Style::default().fg(t.error).bg(bg) } else { dim };
                put(c.buf, r.x + 18, y, &truncate(&detail, r.width.saturating_sub(18) as usize, ell), st, r.width);
                put_right(c.buf, r.x + r.width - 1, y, m.scope, dim);
            }
        }
        _ => {}
    }
}
