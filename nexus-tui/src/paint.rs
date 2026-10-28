//! Drawing helpers that work on a finished `Buffer` rather than on one widget.
//! They use only the public `Theme` surface (`color`, `opts`).

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier};

use crate::theme::{Theme, Tier};

fn mix(a: u8, b: u8) -> u8 {
    (u16::from(a) + u16::from(b)).div_ceil(2) as u8
}

fn toward(c: Color, canvas: (u8, u8, u8)) -> Option<Color> {
    match c {
        Color::Rgb(r, g, b) => Some(Color::Rgb(
            mix(r, canvas.0),
            mix(g, canvas.1),
            mix(b, canvas.2),
        )),
        _ => None,
    }
}

/// The scrim under a modal (DESIGN_PLAN.md D01): every cell's fg and bg move
/// 50% toward `surface.canvas`. Cells whose colours cannot be blended (indexed,
/// named, default), and every cell in ANSI-16, 256 and mono, get
/// `Modifier::DIM` instead.
pub fn scrim(theme: &Theme, buf: &mut Buffer, area: Rect) {
    let area = area.intersection(buf.area);
    let canvas = match (theme.opts.tier, theme.color("surface.canvas")) {
        (Tier::TrueColor, Some(Color::Rgb(r, g, b))) => Some((r, g, b)),
        _ => None,
    };
    for y in area.y..area.bottom() {
        for x in area.x..area.right() {
            let cell = &mut buf[(x, y)];
            let blended = canvas.and_then(|k| Some((toward(cell.fg, k)?, toward(cell.bg, k)?)));
            match blended {
                Some((fg, bg)) => {
                    cell.fg = fg;
                    cell.bg = bg;
                }
                None => {
                    cell.modifier.insert(Modifier::DIM);
                }
            }
        }
    }
}

/// The context meter (DESIGN_PLAN.md D11): `cells` hatch cells, the first
/// `ceil(cells * fraction)` painted with `filled` and the rest with `empty`.
/// Mono and ASCII use `/` for filled and `.` for empty cells so the two stay
/// distinguishable without colour. Returns how many cells were filled.
pub fn meter(
    theme: &Theme,
    buf: &mut Buffer,
    (x, y): (u16, u16),
    cells: u16,
    fraction: f64,
    filled: ratatui::style::Style,
    empty: ratatui::style::Style,
) -> u16 {
    let fraction = if fraction.is_finite() {
        fraction.clamp(0.0, 1.0)
    } else {
        0.0
    };
    let on = ((f64::from(cells) * fraction).ceil() as u16).min(cells);
    let plain = theme.opts.ascii || theme.mono();
    let (full_glyph, empty_glyph) = if plain { ("/", ".") } else { ("╱", "╱") };
    for i in 0..cells {
        let (glyph, style) = if i < on {
            (full_glyph, filled)
        } else {
            (empty_glyph, empty)
        };
        if x + i < buf.area.right() && y < buf.area.bottom() {
            buf.set_string(x + i, y, glyph, style);
        }
    }
    on
}

/// Verb glyphs for tool rows (DESIGN_PLAN.md D06), as `(name, unicode, ascii)`.
/// Mirrors `[glyphs.verb]` in `interface/theme.toml` (a test compares them).
pub const VERBS: [(&str, &str, &str); 8] = [
    ("read", "→", ">"),
    ("edit", "←", "<"),
    ("shell", "$", "$"),
    ("search", "*", "*"),
    ("code", "λ", "L"),
    ("ask", "?", "?"),
    ("session", "↳", "^"),
    ("web", "↗", "^"),
];

/// The verb family of a tool name (a `VERBS` name), if it has one.
pub fn verb_family(tool: &str) -> Option<&'static str> {
    Some(match tool {
        "read" => "read",
        "edit" | "write" | "patch" => "edit",
        "bash" | "bash_output" | "bash_kill" => "shell",
        "grep" | "glob" | "search" | "find" => "search",
        "code" => "code",
        "ask" => "ask",
        "session" => "session",
        "web_search" | "web_fetch" => "web",
        _ => return None,
    })
}

/// The glyph in front of a tool row: its verb glyph, or a dot for a tool
/// without a verb. `ascii` picks the ASCII fallback.
pub fn verb_glyph(tool: &str, ascii: bool) -> &'static str {
    verb_family(tool)
        .and_then(|family| VERBS.iter().find(|(name, _, _)| *name == family))
        .map_or(if ascii { "-" } else { "·" }, |(_, uni, asc)| {
            if ascii {
                asc
            } else {
                uni
            }
        })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::theme::{Mode, Options};

    #[test]
    fn verb_glyphs_match_the_theme_file_and_have_ascii_fallbacks() {
        use unicode_width::UnicodeWidthStr;
        let doc: toml::Table = include_str!("../../interface/theme.toml").parse().unwrap();
        let table = doc["glyphs"]["verb"].as_table().expect("[glyphs.verb]");
        assert_eq!(table.len(), VERBS.len());
        for (name, uni, asc) in VERBS {
            let row = table[name].as_table().unwrap();
            assert_eq!(row["unicode"].as_str(), Some(uni), "{name}");
            assert_eq!(row["ascii"].as_str(), Some(asc), "{name}");
            assert!(asc.is_ascii(), "{name}");
            assert_eq!(uni.width(), 1, "{name} is single width");
        }
        for tool in ["read", "edit", "write", "bash", "code", "ask", "session"] {
            assert!(verb_glyph(tool, true).is_ascii(), "{tool}");
            assert_eq!(verb_glyph(tool, false).width(), 1, "{tool}");
        }
        assert_eq!(verb_glyph("read", false), "→");
        assert_eq!(verb_glyph("read", true), ">");
        assert_eq!(verb_glyph("web_fetch", false), "↗");
        assert_eq!(verb_glyph("mystery", true), "-");
    }

    fn theme(tier: Tier) -> Theme {
        Theme::new(Options {
            tier,
            mode: Mode::Dark,
            ..Options::default()
        })
    }

    #[test]
    fn rgb_cell_blends_halfway_toward_the_canvas() {
        let t = theme(Tier::TrueColor);
        let Some(Color::Rgb(cr, cg, cb)) = t.color("surface.canvas") else {
            panic!("canvas is rgb in truecolor");
        };
        let mut buf = Buffer::empty(Rect::new(0, 0, 2, 1));
        buf[(0, 0)].set_fg(Color::Rgb(255, 255, 255));
        buf[(0, 0)].set_bg(Color::Rgb(100, 0, 40));
        scrim(&t, &mut buf, Rect::new(0, 0, 1, 1));
        let c = &buf[(0, 0)];
        assert_eq!(c.fg, Color::Rgb(mix(255, cr), mix(255, cg), mix(255, cb)));
        assert_eq!(c.bg, Color::Rgb(mix(100, cr), mix(0, cg), mix(40, cb)));
        assert!(!c.modifier.contains(Modifier::DIM));
        // outside the area: untouched
        assert_eq!(buf[(1, 0)].fg, Color::Reset);
    }

    #[test]
    fn indexed_cell_and_low_tiers_get_dim() {
        let t = theme(Tier::TrueColor);
        let mut buf = Buffer::empty(Rect::new(0, 0, 1, 1));
        buf[(0, 0)].set_fg(Color::Indexed(100));
        buf[(0, 0)].set_bg(Color::Indexed(30));
        let area = buf.area;
        scrim(&t, &mut buf, area);
        assert_eq!(buf[(0, 0)].fg, Color::Indexed(100));
        assert!(buf[(0, 0)].modifier.contains(Modifier::DIM));
        for tier in [Tier::Ansi256, Tier::Ansi16, Tier::Mono] {
            let t = theme(tier);
            let mut buf = Buffer::empty(Rect::new(0, 0, 1, 1));
            buf[(0, 0)].set_fg(Color::Rgb(1, 2, 3));
            buf[(0, 0)].set_bg(Color::Rgb(4, 5, 6));
            let area = buf.area;
            scrim(&t, &mut buf, area);
            assert_eq!(buf[(0, 0)].fg, Color::Rgb(1, 2, 3), "{tier:?}");
            assert!(buf[(0, 0)].modifier.contains(Modifier::DIM), "{tier:?}");
        }
    }
}
