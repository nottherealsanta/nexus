//! Bounded local VT state and rendering for a Core terminal stream.
use base64::{engine::general_purpose::STANDARD, Engine};
use ratatui::{
    buffer::Buffer,
    layout::Rect,
    style::{Color, Modifier, Style},
};
use serde_json::Value;

pub const GAP_NOTICE: &str = "Terminal history was discarded; screen may be incomplete.";
const SCROLLBACK: usize = 10_000;

pub struct TerminalScreen {
    parser: vt100::Parser,
    pub seq: Option<u64>,
    pub gap: bool,
}

impl TerminalScreen {
    pub fn new(rows: u16, cols: u16) -> Self {
        Self {
            parser: vt100::Parser::new(rows.max(1), cols.max(1), SCROLLBACK),
            seq: None,
            gap: false,
        }
    }

    pub fn apply(&mut self, frame: &Value) -> Result<(), String> {
        if frame.get("version").and_then(Value::as_u64) != Some(1) {
            return Err("unsupported terminal stream version".into());
        }
        let seq = frame
            .get("seq")
            .and_then(Value::as_u64)
            .ok_or("missing terminal sequence")?;
        let kind = frame
            .get("type")
            .and_then(Value::as_str)
            .ok_or("missing terminal frame type")?;
        if kind == "gap" {
            let rows = frame
                .get("rows")
                .and_then(Value::as_u64)
                .and_then(|n| u16::try_from(n).ok())
                .filter(|n| *n > 0)
                .ok_or("invalid gap rows")?;
            let cols = frame
                .get("cols")
                .and_then(Value::as_u64)
                .and_then(|n| u16::try_from(n).ok())
                .filter(|n| *n > 0)
                .ok_or("invalid gap cols")?;
            self.parser = vt100::Parser::new(rows, cols, SCROLLBACK);
            self.gap = true;
        } else {
            if self.seq.is_some_and(|last| seq <= last) {
                return Ok(());
            }
            if self.seq.is_some_and(|last| seq != last + 1) {
                return Err("terminal sequence gap; reconnect required".into());
            }
            if kind == "output" {
                let data = frame
                    .get("data")
                    .and_then(Value::as_str)
                    .ok_or("missing output data")?;
                self.parser.process(
                    &STANDARD
                        .decode(data)
                        .map_err(|_| "invalid terminal output")?,
                );
            } else if kind == "resize" {
                let rows = frame
                    .get("rows")
                    .and_then(Value::as_u64)
                    .and_then(|n| u16::try_from(n).ok())
                    .filter(|n| *n > 0)
                    .ok_or("invalid resize rows")?;
                let cols = frame
                    .get("cols")
                    .and_then(Value::as_u64)
                    .and_then(|n| u16::try_from(n).ok())
                    .filter(|n| *n > 0)
                    .ok_or("invalid resize cols")?;
                self.parser.set_size(rows, cols);
            }
        }
        self.seq = Some(seq);
        Ok(())
    }

    pub fn paste(&self, text: &str) -> Vec<u8> {
        if self.parser.screen().bracketed_paste() {
            format!("\x1b[200~{text}\x1b[201~").into_bytes()
        } else {
            text.as_bytes().to_vec()
        }
    }

    pub fn scroll(&mut self, rows: usize) {
        self.parser.set_scrollback(rows);
    }
    pub fn scrollback(&self) -> usize {
        self.parser.screen().scrollback()
    }
    pub fn screen(&self) -> &vt100::Screen {
        self.parser.screen()
    }

    pub fn render(&self, area: Rect, buf: &mut Buffer) -> Option<(u16, u16)> {
        let screen = self.parser.screen();
        let (rows, cols) = screen.size();
        for y in 0..rows.min(area.height) {
            for x in 0..cols.min(area.width) {
                if let Some(cell) = screen.cell(y, x) {
                    if cell.is_wide_continuation() {
                        continue;
                    }
                    let mut style = Style::default()
                        .fg(color(cell.fgcolor()))
                        .bg(color(cell.bgcolor()));
                    if cell.bold() {
                        style = style.add_modifier(Modifier::BOLD);
                    }
                    if cell.italic() {
                        style = style.add_modifier(Modifier::ITALIC);
                    }
                    if cell.underline() {
                        style = style.add_modifier(Modifier::UNDERLINED);
                    }
                    if cell.inverse() {
                        style = style.add_modifier(Modifier::REVERSED);
                    }
                    let contents = cell.contents();
                    let symbol = if contents.is_empty() {
                        " "
                    } else {
                        contents.as_str()
                    };
                    buf[(area.x + x, area.y + y)]
                        .set_symbol(symbol)
                        .set_style(style);
                }
            }
        }
        let (y, x) = screen.cursor_position();
        (!screen.hide_cursor() && screen.scrollback() == 0 && y < area.height && x < area.width)
            .then_some((area.x + x, area.y + y))
    }
}

fn color(value: vt100::Color) -> Color {
    match value {
        vt100::Color::Default => Color::Reset,
        vt100::Color::Idx(n) => Color::Indexed(n),
        vt100::Color::Rgb(r, g, b) => Color::Rgb(r, g, b),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn split_utf8_colors_alternate_and_paste() {
        let mut screen = TerminalScreen::new(4, 20);
        for (seq, bytes) in [b"\x1b[31m".as_slice(), &[0xe7], &[0x95, 0x8c]]
            .into_iter()
            .enumerate()
        {
            screen
                .apply(
                    &json!({"version":1,"seq":seq+1,"type":"output","data":STANDARD.encode(bytes)}),
                )
                .unwrap();
        }
        assert_eq!(screen.screen().cell(0, 0).unwrap().contents(), "界");
        let mut buf = Buffer::empty(Rect::new(0, 0, 20, 4));
        screen.render(buf.area, &mut buf);
        assert_eq!(buf[(0, 0)].fg, Color::Indexed(1));
        screen.apply(&json!({"version":1,"seq":4,"type":"output","data":STANDARD.encode(b"\x1b[?1049h\x1b[?2004h")})).unwrap();
        assert!(screen.screen().alternate_screen());
        assert_eq!(screen.paste("x"), b"\x1b[200~x\x1b[201~");
    }

    #[test]
    fn gaps_reset_and_sequences_do_not_silently_skip() {
        let mut screen = TerminalScreen::new(4, 20);
        screen
            .apply(&json!({"version":1,"seq":1,"type":"output","data":STANDARD.encode("old")}))
            .unwrap();
        assert!(screen
            .apply(&json!({"version":1,"seq":3,"type":"control"}))
            .is_err());
        screen
            .apply(&json!({"version":1,"seq":9,"type":"gap","rows":4,"cols":20}))
            .unwrap();
        assert!(screen.gap);
        assert!(!screen.screen().contents().contains("old"));
        screen
            .apply(&json!({"version":1,"seq":10,"type":"output","data":STANDARD.encode("new")}))
            .unwrap();
        assert!(screen.screen().contents().contains("new"));
    }
}
