//! `Buffer` to SVG, for visual review without a terminal: `--dump-frame ... --svg`.
//! Ported from the design mock-up writer. One cell is 8.4 x 18 px; the block
//! glyphs the shell draws with (`▄ ▀ █ ▌ ▐ ▏`) become rects so half-row edges look
//! as they do in a terminal. Reverse video swaps the painted colours.

use std::fmt::Write as _;

use ratatui::buffer::{Buffer, Cell};
use ratatui::style::{Color, Modifier};

const CELL_W: f32 = 8.4;
const CELL_H: f32 = 18.0;

/// Default foreground and background of the page, for cells that set none.
pub fn page_colors(light: bool) -> (&'static str, &'static str) {
    if light {
        ("#1f2124", "#f7f7f5")
    } else {
        ("#e6e7e8", "#0B0B0B")
    }
}

const ANSI16: [&str; 16] = [
    "#000000", "#cd0000", "#00cd00", "#cdcd00", "#0000ee", "#cd00cd", "#00cdcd", "#e5e5e5",
    "#7f7f7f", "#ff0000", "#00ff00", "#ffff00", "#5c5cff", "#ff00ff", "#00ffff", "#ffffff",
];

fn indexed(i: u8) -> String {
    match i {
        0..=15 => ANSI16[i as usize].to_owned(),
        16..=231 => {
            let n = i - 16;
            let step = |v: u8| if v == 0 { 0 } else { 55 + 40 * v };
            format!(
                "#{:02x}{:02x}{:02x}",
                step(n / 36),
                step(n / 6 % 6),
                step(n % 6)
            )
        }
        _ => {
            let v = 8 + 10 * (i - 232);
            format!("#{v:02x}{v:02x}{v:02x}")
        }
    }
}

/// `#rrggbb` for a colour, or `default` for the terminal default.
fn hex(c: Color, default: &str) -> String {
    match c {
        Color::Rgb(r, g, b) => format!("#{r:02x}{g:02x}{b:02x}"),
        Color::Indexed(i) => indexed(i),
        Color::Black => ANSI16[0].into(),
        Color::Red => ANSI16[1].into(),
        Color::Green => ANSI16[2].into(),
        Color::Yellow => ANSI16[3].into(),
        Color::Blue => ANSI16[4].into(),
        Color::Magenta => ANSI16[5].into(),
        Color::Cyan => ANSI16[6].into(),
        Color::Gray => ANSI16[7].into(),
        Color::DarkGray => ANSI16[8].into(),
        Color::LightRed => ANSI16[9].into(),
        Color::LightGreen => ANSI16[10].into(),
        Color::LightYellow => ANSI16[11].into(),
        Color::LightBlue => ANSI16[12].into(),
        Color::LightMagenta => ANSI16[13].into(),
        Color::LightCyan => ANSI16[14].into(),
        Color::White => ANSI16[15].into(),
        Color::Reset => default.into(),
    }
}

fn esc(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
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

/// Render `buf` as an SVG; `dfg` and `dbg` are the page's default colours.
pub fn to_svg(buf: &Buffer, dfg: &str, dbg: &str) -> String {
    let a = buf.area;
    let mut s = String::new();
    let _ = write!(
        s,
        r#"<svg xmlns="http://www.w3.org/2000/svg" width="{}" height="{}" font-family="'Monaspace Argon', Menlo, monospace" font-size="14" xml:space="preserve"><rect width="100%" height="100%" fill="{dbg}"/>"#,
        a.width as f32 * CELL_W,
        a.height as f32 * CELL_H
    );
    // The painted background of a cell: reverse video paints the foreground.
    let paint = |c: &Cell| -> Option<String> {
        if c.modifier.contains(Modifier::REVERSED) {
            Some(hex(c.fg, dfg))
        } else if c.bg == Color::Reset {
            None
        } else {
            Some(hex(c.bg, dbg))
        }
    };
    let mut blocks = String::new();
    for y in 0..a.height {
        let row = y as f32 * CELL_H;
        let mut x = 0;
        while x < a.width {
            let bg = paint(&buf[(x, y)]);
            let mut end = x + 1;
            while end < a.width && paint(&buf[(end, y)]) == bg {
                end += 1;
            }
            if let Some(col) = bg {
                let _ = write!(
                    s,
                    r#"<rect x="{:.1}" y="{row:.1}" width="{:.1}" height="{CELL_H}" fill="{col}"/>"#,
                    x as f32 * CELL_W,
                    (end - x) as f32 * CELL_W
                );
            }
            x = end;
        }
        let mut x = 0;
        while x < a.width {
            let c = &buf[(x, y)];
            let key = (c.fg, c.modifier, c.bg);
            let rev = c.modifier.contains(Modifier::REVERSED);
            let mut text = String::new();
            let mut xs: Vec<String> = Vec::new();
            let mut end = x;
            while end < a.width {
                let n = &buf[(end, y)];
                if (n.fg, n.modifier, n.bg) != key {
                    break;
                }
                let sym = if n.symbol().is_empty() {
                    " "
                } else {
                    n.symbol()
                };
                if let Some((dy, hh, dx, ww)) = block(sym) {
                    let col = if rev { hex(n.bg, dbg) } else { hex(n.fg, dfg) };
                    let _ = write!(
                        blocks,
                        r#"<rect x="{:.1}" y="{:.1}" width="{:.1}" height="{:.1}" fill="{col}"/>"#,
                        end as f32 * CELL_W + dx * CELL_W,
                        row + dy * CELL_H,
                        ww * CELL_W,
                        hh * CELL_H
                    );
                } else {
                    for chr in sym.chars().filter(|c| *c != ' ') {
                        text.push(chr);
                        xs.push(format!("{:.1}", end as f32 * CELL_W));
                    }
                }
                end += 1;
            }
            if !text.trim().is_empty() {
                let fg = if rev { hex(c.bg, dbg) } else { hex(c.fg, dfg) };
                let mut attrs = String::new();
                if c.modifier.contains(Modifier::BOLD) {
                    attrs.push_str(r#" font-weight="bold""#);
                }
                let lines: Vec<&str> = [
                    c.modifier
                        .contains(Modifier::UNDERLINED)
                        .then_some("underline"),
                    c.modifier
                        .contains(Modifier::CROSSED_OUT)
                        .then_some("line-through"),
                ]
                .into_iter()
                .flatten()
                .collect();
                if !lines.is_empty() {
                    let _ = write!(attrs, r#" text-decoration="{}""#, lines.join(" "));
                }
                if c.modifier.contains(Modifier::ITALIC) {
                    attrs.push_str(r#" font-style="italic""#);
                }
                if c.modifier.contains(Modifier::DIM) {
                    attrs.push_str(r#" opacity="0.6""#);
                }
                let _ = write!(
                    s,
                    r#"<text x="{}" y="{:.1}" fill="{fg}"{attrs}>{}</text>"#,
                    xs.join(" "),
                    row + 13.5,
                    esc(&text)
                );
            }
            x = end.max(x + 1);
        }
    }
    s.push_str(&blocks);
    s.push_str("</svg>");
    s
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::layout::Rect;
    use ratatui::style::Style;

    #[test]
    fn tiny_buffer_becomes_rects_and_text() {
        let mut buf = Buffer::empty(Rect::new(0, 0, 4, 1));
        buf.set_string(
            0,
            0,
            "a<",
            Style::new()
                .fg(Color::Rgb(255, 0, 0))
                .bg(Color::Rgb(0, 0, 255))
                .add_modifier(Modifier::BOLD | Modifier::UNDERLINED),
        );
        buf[(2, 0)].set_symbol("▌");
        buf[(3, 0)].set_symbol("b");
        buf[(3, 0)].modifier = Modifier::REVERSED;
        let svg = to_svg(&buf, "#ffffff", "#000000");
        assert!(svg.starts_with("<svg") && svg.ends_with("</svg>"));
        assert!(svg.contains(r#"width="33.6" height="18""#));
        // Background run for the two styled cells, then the text with its attributes.
        assert!(
            svg.contains(r##"<rect x="0.0" y="0.0" width="16.8" height="18" fill="#0000ff"/>"##)
        );
        assert!(svg.contains(
            r##"fill="#ff0000" font-weight="bold" text-decoration="underline">a&lt;</text>"##
        ));
        // The half block is a rect, 4.2 px wide; reverse video paints the default fg.
        assert!(svg.contains(r##"width="4.2" height="18.0" fill="#ffffff""##));
        assert!(svg.contains(r##"x="25.2" y="0.0" width="8.4" height="18" fill="#ffffff""##));
    }
}
