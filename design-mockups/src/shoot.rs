//! Screenshots without a terminal: render into a `Buffer`, write `.txt` and `.svg`,
//! and a static `shots/index.html` to browse them.
use crate::app::{App, SIZES};
use ratatui::{
    buffer::Buffer,
    layout::Rect,
    style::{Color, Modifier},
};
use std::{fmt::Write as _, fs, io, path::Path, time::Instant};

pub const THEMES: [&str; 3] = ["dark", "light", "mono"];

pub fn render(key: &str, state: usize, theme: usize, size: usize, ascii: bool) -> Option<(Buffer, Rect)> {
    let mut app = App::new();
    if !app.select(key, state) {
        return None;
    }
    app.theme_i = theme;
    app.size_i = size;
    app.ascii = ascii;
    let (w, h) = SIZES[size];
    let full = Rect::new(0, 0, w.max(80), h.max(24) + 1);
    let mut buf = Buffer::empty(full);
    let now = Instant::now();
    // Two passes: the first settles focus and scroll, the second is what you see.
    app.render(&mut buf, full, now);
    app.render(&mut buf, full, now);
    Some((buf, app.frame_area))
}

pub fn to_text(buf: &Buffer, area: Rect) -> String {
    let mut out = String::new();
    for y in area.y..area.y + area.height {
        let mut line = String::new();
        for x in area.x..area.x + area.width {
            line.push_str(buf[(x, y)].symbol());
        }
        out.push_str(line.trim_end());
        out.push('\n');
    }
    out
}

fn hex(c: Color, default: &str) -> String {
    match c {
        Color::Rgb(r, g, b) => format!("#{r:02x}{g:02x}{b:02x}"),
        Color::Black => "#000000".into(),
        Color::Gray => "#c0c0c0".into(),
        _ => default.into(),
    }
}
fn esc(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;")
}

pub fn to_svg(buf: &Buffer, area: Rect, theme: usize) -> String {
    let (cw, ch) = (8.4f32, 18.0f32);
    let (dfg, dbg) = if theme == 1 { ("#1b1b1b", "#ffffff") } else if theme == 2 { ("#d8d8d8", "#101010") } else { ("#eeeeee", "#0b0b0b") };
    let mut s = String::new();
    let _ = write!(s, r#"<svg xmlns="http://www.w3.org/2000/svg" width="{}" height="{}" font-family="Menlo, Consolas, 'DejaVu Sans Mono', monospace" font-size="14" xml:space="preserve"><rect width="100%" height="100%" fill="{dbg}"/>"#, area.width as f32 * cw, area.height as f32 * ch);
    for y in area.y..area.y + area.height {
        let row = (y - area.y) as f32 * ch;
        // background runs
        let mut x = area.x;
        while x < area.x + area.width {
            let c = &buf[(x, y)];
            let rev = c.modifier.contains(Modifier::REVERSED);
            let bg = if rev { c.fg } else { c.bg };
            let mut end = x + 1;
            while end < area.x + area.width && {
                let n = &buf[(end, y)];
                (if n.modifier.contains(Modifier::REVERSED) { n.fg } else { n.bg }) == bg
            } {
                end += 1;
            }
            if bg != Color::Reset {
                let _ = write!(s, r##"<rect x="{:.1}" y="{:.1}" width="{:.1}" height="{ch}" fill="{}"/>"##, (x - area.x) as f32 * cw, row, (end - x) as f32 * cw, hex(bg, dbg));
            } else if rev {
                let _ = write!(s, r##"<rect x="{:.1}" y="{:.1}" width="{:.1}" height="{ch}" fill="{dfg}"/>"##, (x - area.x) as f32 * cw, row, (end - x) as f32 * cw);
            }
            x = end;
        }
        // text runs of equal style
        let mut x = area.x;
        while x < area.x + area.width {
            let c = &buf[(x, y)];
            let key = (c.fg, c.modifier, c.bg);
            let mut text = String::new();
            let mut xs: Vec<String> = Vec::new();
            let mut end = x;
            while end < area.x + area.width {
                let n = &buf[(end, y)];
                if (n.fg, n.modifier, n.bg) != key {
                    break;
                }
                // One x per character keeps every glyph on its cell, whatever the font does.
                let sym = if n.symbol().is_empty() { " " } else { n.symbol() };
                // Spaces are skipped: SVG collapses them and would shift the x list.
                for ch in sym.chars().filter(|c| *c != ' ') {
                    text.push(ch);
                    xs.push(format!("{:.1}", (end - area.x) as f32 * cw));
                }
                end += 1;
            }
            if !text.trim().is_empty() {
                let rev = c.modifier.contains(Modifier::REVERSED);
                let fg = if rev { if c.bg == Color::Reset { dbg.to_string() } else { hex(c.bg, dbg) } } else { hex(c.fg, dfg) };
                let mut attrs = String::new();
                if c.modifier.contains(Modifier::BOLD) {
                    attrs.push_str(r#" font-weight="bold""#);
                }
                let (ul, st) = (c.modifier.contains(Modifier::UNDERLINED), c.modifier.contains(Modifier::CROSSED_OUT));
                if ul || st {
                    attrs.push_str(&format!(r#" text-decoration="{}{}""#, if ul { "underline" } else { "" }, if st { if ul { " line-through" } else { "line-through" } } else { "" }));
                }
                if c.modifier.contains(Modifier::ITALIC) {
                    attrs.push_str(r#" font-style="italic""#);
                }
                if c.modifier.contains(Modifier::DIM) {
                    attrs.push_str(r#" opacity="0.6""#);
                }
                let _ = write!(s, r#"<text x="{}" y="{:.1}" fill="{fg}"{attrs}>{}</text>"#, xs.join(" "), row + 14.0, esc(&text));
            }
            x = end;
        }
    }
    s.push_str("</svg>");
    s
}

/// Write every screen × state × theme × size. Returns the number of files written.
pub fn shoot(dir: &Path, only: Option<&str>, sizes: &[usize], themes: &[usize]) -> io::Result<usize> {
    let app = App::new();
    let mut n = 0;
    let mut index = String::from(INDEX_HEAD);
    for def in &app.screens {
        if only.map(|o| o != def.key).unwrap_or(false) {
            continue;
        }
        let _ = write!(index, "<section><h2>{} <small>{}</small></h2><div class=g>", esc(def.title), def.key);
        fs::create_dir_all(dir.join(def.key))?;
        for (si, sname) in def.states.iter().enumerate() {
            for &th in themes {
                for &sz in sizes {
                    let (w, h) = SIZES[sz];
                    let Some((buf, area)) = render(def.key, si, th, sz, false) else { continue };
                    let stem = format!("{}-{}-{}x{}", sname.replace([' ', '+', '×', '/'], "_"), THEMES[th], w, h);
                    fs::write(dir.join(def.key).join(format!("{stem}.txt")), to_text(&buf, area))?;
                    fs::write(dir.join(def.key).join(format!("{stem}.svg")), to_svg(&buf, area, th))?;
                    n += 2;
                    let _ = write!(index, r#"<figure data-theme="{}" data-size="{}x{}"><figcaption>{} · {} · {}×{}</figcaption><a href="{}/{stem}.svg"><img loading=lazy src="{}/{stem}.svg"></a></figure>"#, THEMES[th], w, h, esc(sname), THEMES[th], w, h, def.key, def.key);
                }
            }
        }
        index.push_str("</div></section>");
    }
    index.push_str("</main><script>const f=()=>{const t=document.getElementById('t').value,s=document.getElementById('s').value;document.querySelectorAll('figure').forEach(e=>e.hidden=!((t=='all'||e.dataset.theme==t)&&(s=='all'||e.dataset.size==s)))};document.querySelectorAll('select').forEach(e=>e.onchange=f)</script>");
    fs::write(dir.join("index.html"), index)?;
    Ok(n)
}

const INDEX_HEAD: &str = r#"<!doctype html><meta charset=utf-8><title>Nexus Ratatui mock-ups</title><style>
body{margin:0;background:#161616;color:#ddd;font:14px system-ui}header{position:sticky;top:0;background:#222;padding:10px 20px;display:flex;gap:16px;align-items:center}
main{padding:0 20px 40px}h2{margin:28px 0 8px}small{color:#888;font-weight:400}.g{display:flex;flex-wrap:wrap;gap:14px}
figure{margin:0}figcaption{color:#999;font-size:12px;margin-bottom:4px}img{max-width:560px;border:1px solid #333}select{background:#111;color:#ddd;border:1px solid #444;padding:3px}</style>
<header><b>Nexus Ratatui mock-ups</b><label>theme <select id=t><option>all<option>dark<option>light<option>mono</select></label>
<label>size <select id=s><option>all<option>80x24<option>120x36<option>200x50</select></label></header><main>"#;
