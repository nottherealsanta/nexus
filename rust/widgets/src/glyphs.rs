//! Glyph sets (plan §5.2). No emoji and no ambiguous-width characters.
#[derive(Clone, Copy, Debug)]
pub struct Glyphs {
    pub focus_bar: &'static str,
    pub toggle_on: &'static str,
    pub toggle_off: &'static str,
    pub check_on: &'static str,
    pub check_off: &'static str,
    pub check_mixed: &'static str,
    pub radio_on: &'static str,
    pub radio_off: &'static str,
    pub caret: &'static str,
    pub handle: &'static str,
    pub up: &'static str,
    pub down: &'static str,
    pub close: &'static str,
    pub dot_ok: &'static str,
    pub dot_work: &'static str,
    pub dot_idle: &'static str,
    pub dot_err: &'static str,
    pub open: &'static str,
    pub closed: &'static str,
    pub ellipsis: &'static str,
    pub search: &'static str,
    pub bar_full: &'static str,
    pub bar_empty: &'static str,
    pub rule: &'static str,
    pub tab_rule: &'static str,
    pub spinner: &'static [&'static str],
    pub info: &'static str,
    pub ok: &'static str,
    pub warn: &'static str,
    pub err: &'static str,
    pub ascii: bool,
}
impl Glyphs {
    pub const fn unicode() -> Self {
        Self {
            focus_bar: "▌",
            toggle_on: "■ ON ",
            toggle_off: " OFF□",
            check_on: "☑",
            check_off: "☐",
            check_mixed: "⊟",
            radio_on: "◉",
            radio_off: "○",
            caret: "▾",
            handle: "⋮⋮",
            up: "↑",
            down: "↓",
            close: "×",
            dot_ok: "●",
            dot_work: "◐",
            dot_idle: "○",
            dot_err: "✕",
            open: "▾",
            closed: "▸",
            ellipsis: "…",
            search: "⌕",
            bar_full: "█",
            bar_empty: "░",
            rule: "─",
            tab_rule: "━",
            spinner: &["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"],
            info: "i",
            ok: "✓",
            warn: "!",
            err: "✕",
            ascii: false,
        }
    }
    pub const fn ascii() -> Self {
        Self {
            focus_bar: ">",
            toggle_on: "x ON ",
            toggle_off: "  OFF",
            check_on: "[x]",
            check_off: "[ ]",
            check_mixed: "[-]",
            radio_on: "(*)",
            radio_off: "( )",
            caret: "v",
            handle: "::",
            up: "^",
            down: "v",
            close: "x",
            dot_ok: "*",
            dot_work: "~",
            dot_idle: "o",
            dot_err: "x",
            open: "v",
            closed: ">",
            ellipsis: "...",
            search: "/",
            bar_full: "#",
            bar_empty: "-",
            rule: "-",
            tab_rule: "=",
            spinner: &["|", "/", "-", "\\"],
            info: "i",
            ok: "+",
            warn: "!",
            err: "x",
            ascii: true,
        }
    }
    /// `NEXUS_ASCII=1` or `TERM=linux` selects ASCII.
    pub fn from_env() -> Self {
        let ascii = std::env::var("NEXUS_ASCII").map(|v| v == "1").unwrap_or(false)
            || std::env::var("TERM").map(|v| v == "linux").unwrap_or(false);
        if ascii {
            Self::ascii()
        } else {
            Self::unicode()
        }
    }
    /// Every single-glyph field, for width tests.
    pub fn singles(&self) -> Vec<&'static str> {
        vec![
            self.focus_bar, self.caret, self.up, self.down, self.close, self.dot_ok,
            self.dot_work, self.dot_idle, self.dot_err, self.open, self.closed, self.search,
            self.bar_full, self.bar_empty, self.rule, self.tab_rule, self.info, self.ok,
            self.warn, self.err, self.check_on, self.check_off, self.check_mixed,
            self.radio_on, self.radio_off,
        ]
    }
}
