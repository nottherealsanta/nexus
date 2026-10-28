//! `Buffer` → text and SVG (adapted from main's `design-mockups/src/shoot.rs`).
use ratatui::{buffer::Buffer, style::{Color, Modifier}};
use std::fmt::Write as _;

pub fn to_text(buf: &Buffer) -> String {
    let a = buf.area;
    let mut out = String::new();
    for y in 0..a.height {
        let mut line = String::new();
        for x in 0..a.width {
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
        _ => default.into(),
    }
}
fn esc(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;")
}

pub fn to_svg(buf: &Buffer, dfg: &str, dbg: &str) -> String {
    let a = buf.area;
    let (cw, ch) = (8.4f32, 18.0f32);
    let mut s = String::new();
    let _ = write!(s, r#"<svg xmlns="http://www.w3.org/2000/svg" width="{}" height="{}" font-family="'Monaspace Argon', Menlo, monospace" font-size="14" xml:space="preserve"><style>@font-face{{font-family:'Monaspace Argon';src:url('file:///Users/santosh.rajan/repos/nexus/nexus-nsh/fonts/MonaspaceArgon-Regular.otf')}}@font-face{{font-family:'Monaspace Argon';font-weight:bold;src:url('file:///Users/santosh.rajan/repos/nexus/nexus-nsh/fonts/MonaspaceArgon-Bold.otf')}}</style><rect width="100%" height="100%" fill="{dbg}"/>"#, a.width as f32 * cw, a.height as f32 * ch);
    let mut blocks: Vec<String> = Vec::new();
    for y in 0..a.height {
        let row = y as f32 * ch;
        // Background runs: each cell's painted colour (reverse video paints the fg, default fg = page fg).
        let paint = |c: &ratatui::buffer::Cell| -> Option<String> {
            if c.modifier.contains(Modifier::REVERSED) {
                Some(if c.fg == Color::Reset { dfg.to_string() } else { hex(c.fg, dfg) })
            } else if c.bg == Color::Reset {
                None
            } else {
                Some(hex(c.bg, dbg))
            }
        };
        let mut x = 0;
        while x < a.width {
            let bg = paint(&buf[(x, y)]);
            let mut end = x + 1;
            while end < a.width && paint(&buf[(end, y)]) == bg {
                end += 1;
            }
            if let Some(col) = bg {
                let _ = write!(s, r##"<rect x="{:.1}" y="{:.1}" width="{:.1}" height="{ch}" fill="{col}"/>"##, x as f32 * cw, row, (end - x) as f32 * cw);
            }
            x = end;
        }
        let mut x = 0;
        while x < a.width {
            let c = &buf[(x, y)];
            let key = (c.fg, c.modifier, c.bg);
            let mut text = String::new();
            let mut xs: Vec<String> = Vec::new();
            let mut end = x;
            while end < a.width {
                let n = &buf[(end, y)];
                if (n.fg, n.modifier, n.bg) != key {
                    break;
                }
                let sym = if n.symbol().is_empty() { " " } else { n.symbol() };
                // Block elements become rects so half-row edges look as they do in a terminal.
                if let Some((dy, hh, dx, ww)) = block(sym) {
                    let col = if n.modifier.contains(Modifier::REVERSED) { hex(n.bg, dbg) } else { hex(n.fg, dfg) };
                    blocks.push(format!(r##"<rect x="{:.1}" y="{:.1}" width="{:.1}" height="{:.1}" fill="{}"/>"##, end as f32 * cw + dx * cw, row + dy * ch, ww * cw, hh * ch, col));
                    end += 1;
                    continue;
                }
                for chr in sym.chars().filter(|c| *c != ' ') {
                    text.push(chr);
                    xs.push(format!("{:.1}", end as f32 * cw));
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
                    attrs.push_str(&format!(r#" text-decoration="{}""#, [ul.then_some("underline"), st.then_some("line-through")].into_iter().flatten().collect::<Vec<_>>().join(" ")));
                }
                if c.modifier.contains(Modifier::ITALIC) {
                    attrs.push_str(r#" font-style="italic""#);
                }
                if c.modifier.contains(Modifier::DIM) {
                    attrs.push_str(r#" opacity="0.6""#);
                }
                let _ = write!(s, r#"<text x="{}" y="{:.1}" fill="{fg}"{attrs}>{}</text>"#, xs.join(" "), row + 13.5, esc(&text));
            }
            x = end;
        }
    }
    for b in blocks {
        s.push_str(&b);
    }
    s.push_str("</svg>");
    s
}

/// (dy, h, dx, w) in cell fractions for block glyphs.
fn block(sym: &str) -> Option<(f32, f32, f32, f32)> {
    Some(match sym {
        "▄" => (0.5, 0.5, 0.0, 1.0),
        "▀" => (0.0, 0.5, 0.0, 1.0),
        "█" => (0.0, 1.0, 0.0, 1.0),
        "▌" => (0.0, 1.0, 0.0, 0.5),
        "▐" => (0.0, 1.0, 0.5, 0.5),
        "▏" => (0.0, 1.0, 0.0, 0.125),
        _ => return None,
    })
}
