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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::theme::{Mode, Options};

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
