//! The painter every screen draws with: cell-exact text, fills, rules and
//! frames on a Ratatui `Buffer`. Coordinates are absolute cells.
use crate::theme::Th;
use ratatui::{buffer::Buffer, layout::Rect, style::{Modifier, Style}};
use unicode_width::UnicodeWidthStr;

/// One styled run of text.
pub type Seg<'a> = (&'a str, Style);

pub struct P<'a> {
    pub buf: &'a mut Buffer,
    pub t: &'a Th,
    pub w: u16,
    pub h: u16,
}

pub fn width(s: &str) -> u16 {
    UnicodeWidthStr::width(s) as u16
}

pub fn bold(s: Style) -> Style {
    s.add_modifier(Modifier::BOLD)
}
pub fn italic(s: Style) -> Style {
    s.add_modifier(Modifier::ITALIC)
}
pub fn under(s: Style) -> Style {
    s.add_modifier(Modifier::UNDERLINED)
}
pub fn dim(s: Style) -> Style {
    s.add_modifier(Modifier::DIM)
}
pub fn rev(s: Style) -> Style {
    s.add_modifier(Modifier::REVERSED)
}

/// Total width of segments.
pub fn segs_w(segs: &[Seg]) -> u16 {
    segs.iter().map(|(s, _)| width(s)).sum()
}

/// Cut `s` to `max` cells, ending with `…` if cut.
pub fn trunc(s: &str, max: u16) -> String {
    if width(s) <= max {
        return s.to_string();
    }
    let mut out = String::new();
    for ch in s.chars() {
        if width(&out) + width(&ch.to_string()) + 1 > max {
            break;
        }
        out.push(ch);
    }
    out.push('…');
    out
}

/// Cut the middle of a path to `max` cells.
pub fn mid(s: &str, max: u16) -> String {
    if width(s) <= max || max < 5 {
        return s.to_string();
    }
    let chars: Vec<char> = s.chars().collect();
    let keep = (max - 1) as usize;
    let head = keep / 2;
    let tail = keep - head;
    let mut out: String = chars[..head].iter().collect();
    out.push('…');
    out.extend(&chars[chars.len() - tail..]);
    out
}

impl<'a> P<'a> {
    pub fn new(buf: &'a mut Buffer, t: &'a Th) -> P<'a> {
        let a = buf.area;
        P { buf, t, w: a.width, h: a.height }
    }

    /// Fill a rectangle with a surface token (spaces, background only).
    pub fn fill(&mut self, x: u16, y: u16, w: u16, h: u16, surface: &str) {
        let st = self.t.bg(surface);
        for yy in y..(y + h).min(self.h) {
            for xx in x..(x + w).min(self.w) {
                let c = &mut self.buf[(xx, yy)];
                c.set_symbol(" ");
                c.set_style(st);
            }
        }
    }

    /// Patch the style of existing cells (e.g. a selection over painted text).
    pub fn tint(&mut self, x: u16, y: u16, w: u16, st: Style) {
        for xx in x..(x + w).min(self.w) {
            if y < self.h {
                self.buf[(xx, y)].set_style(st);
            }
        }
    }

    /// Write segments from `x`; keeps each cell's background unless the style sets one.
    /// Returns the x after the last cell. Clips at `limit` (exclusive), default the width.
    pub fn put(&mut self, x: u16, y: u16, segs: &[Seg]) -> u16 {
        self.put_clip(x, y, segs, self.w)
    }

    pub fn put_clip(&mut self, x: u16, y: u16, segs: &[Seg], limit: u16) -> u16 {
        if y >= self.h {
            return x;
        }
        let mut cx = x;
        for (s, st) in segs {
            for ch in s.chars() {
                let cw = width(&ch.to_string()).max(if ch == ' ' { 1 } else { 0 });
                if cw == 0 {
                    continue;
                }
                if cx + cw > limit.min(self.w) {
                    return cx;
                }
                let cell = &mut self.buf[(cx, y)];
                cell.set_symbol(&ch.to_string());
                cell.set_style(*st);
                if cw == 2 && cx + 1 < self.w {
                    self.buf[(cx + 1, y)].set_symbol("");
                }
                cx += cw;
            }
        }
        cx
    }

    /// Right-align segments so they end just before `end` (exclusive).
    pub fn put_r(&mut self, end: u16, y: u16, segs: &[Seg]) -> u16 {
        let x = end.saturating_sub(segs_w(segs));
        self.put(x, y, segs);
        x
    }

    /// Centre segments in `[x, x+w)`.
    pub fn put_c(&mut self, x: u16, w: u16, y: u16, segs: &[Seg]) {
        let sw = segs_w(segs);
        self.put(x + w.saturating_sub(sw) / 2, y, segs);
    }

    /// Horizontal run of one glyph.
    pub fn hline(&mut self, x: u16, y: u16, w: u16, glyph: &str, st: Style) {
        for xx in x..(x + w).min(self.w) {
            if y < self.h {
                let c = &mut self.buf[(xx, y)];
                c.set_symbol(glyph);
                c.set_style(st);
            }
        }
    }

    /// Vertical run of one glyph.
    pub fn vline(&mut self, x: u16, y: u16, h: u16, glyph: &str, st: Style) {
        for yy in y..(y + h).min(self.h) {
            if x < self.w {
                let c = &mut self.buf[(x, yy)];
                c.set_symbol(glyph);
                c.set_style(st);
            }
        }
    }

    /// A box frame. `rounded` uses ╭╮╰╯. The title sits in the top border after one cell.
    pub fn frame(&mut self, x: u16, y: u16, w: u16, h: u16, st: Style, rounded: bool, title: &[Seg]) {
        let ascii = self.t.ascii;
        let (tl, tr, bl, br, hz, vt) = if ascii {
            ("+", "+", "+", "+", "-", "|")
        } else if rounded {
            ("╭", "╮", "╰", "╯", "─", "│")
        } else {
            ("┌", "┐", "└", "┘", "─", "│")
        };
        self.hline(x + 1, y, w - 2, hz, st);
        self.hline(x + 1, y + h - 1, w - 2, hz, st);
        self.vline(x, y + 1, h - 2, vt, st);
        self.vline(x + w - 1, y + 1, h - 2, vt, st);
        self.put(x, y, &[(tl, st)]);
        self.put(x + w - 1, y, &[(tr, st)]);
        self.put(x, y + h - 1, &[(bl, st)]);
        self.put(x + w - 1, y + h - 1, &[(br, st)]);
        if !title.is_empty() {
            let mut segs: Vec<Seg> = vec![(" ", st)];
            segs.extend_from_slice(title);
            segs.push((" ", st));
            self.put_clip(x + 2, y, &segs, x + w - 2);
        }
    }

    /// Scrim under a modal (D01): blend every cell's fg and bg 50% toward the canvas.
    /// Mono dims instead.
    pub fn scrim(&mut self) {
        let canvas = self.t.color("surface.canvas");
        let mono = self.t.mode == crate::theme::Mode::Mono;
        let blend = |c: ratatui::style::Color| match (c, canvas) {
            (ratatui::style::Color::Rgb(r, g, b), ratatui::style::Color::Rgb(cr, cg, cb)) => {
                let m = |x: u8, y: u8| ((x as u16 + y as u16) / 2) as u8;
                ratatui::style::Color::Rgb(m(r, cr), m(g, cg), m(b, cb))
            }
            (other, _) => other,
        };
        for y in 0..self.h {
            for x in 0..self.w {
                let cell = &mut self.buf[(x, y)];
                if mono {
                    cell.modifier.insert(Modifier::DIM);
                } else {
                    cell.fg = blend(cell.fg);
                    cell.bg = blend(cell.bg);
                }
            }
        }
    }

    /// Paragraph: wrap words of `text` into `w` cells from (x, y); returns rows used.
    pub fn wrap(&mut self, x: u16, y: u16, w: u16, text: &str, st: Style) -> u16 {
        let mut row = 0;
        let mut line = String::new();
        for word in text.split(' ') {
            let candidate = if line.is_empty() { word.to_string() } else { format!("{line} {word}") };
            if width(&candidate) > w && !line.is_empty() {
                self.put(x, y + row, &[(&line, st)]);
                row += 1;
                line = word.to_string();
            } else {
                line = candidate;
            }
        }
        if !line.is_empty() {
            self.put(x, y + row, &[(&line, st)]);
            row += 1;
        }
        row
    }
}

/// Area helper for screens that want ratatui layout.
pub fn rect(x: u16, y: u16, w: u16, h: u16) -> Rect {
    Rect::new(x, y, w, h)
}
