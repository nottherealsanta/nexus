//! Colour roles (plan §5.1). Dark and light extend `rust/tui` `Palette`; `mono`
//! uses only modifiers so meaning survives `NO_COLOR`.
use ratatui::style::{Color, Modifier, Style};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Dark,
    Light,
    Mono,
}

#[derive(Clone, Copy, Debug)]
pub struct Theme {
    pub mode: Mode,
    pub bg: Color,
    pub text: Color,
    pub muted: Color,
    pub quiet: Color,
    pub accent: Color,
    pub surface: Color,
    pub raised: Color,
    pub element: Color,
    pub element_hi: Color,
    pub border: Color,
    pub border_strong: Color,
    pub focus_bg: Color,
    pub blue: Color,
    pub purple: Color,
    pub cyan: Color,
    pub success: Color,
    pub warning: Color,
    pub error: Color,
}

pub fn mix(from: Color, to: Color, amount: f32) -> Color {
    match (from, to) {
        (Color::Rgb(r, g, b), Color::Rgb(rr, gg, bb)) => {
            let a = amount.clamp(0.0, 1.0);
            let c = |x: u8, y: u8| (x as f32 + (y as f32 - x as f32) * a).round() as u8;
            Color::Rgb(c(r, rr), c(g, gg), c(b, bb))
        }
        _ => {
            if amount >= 0.5 {
                to
            } else {
                from
            }
        }
    }
}

impl Theme {
    pub fn dark() -> Self {
        // Grok Night; keep in step with `rust/tui` `Palette::new(false)`.
        let rgb = Color::Rgb;
        let surface = rgb(20, 20, 20);
        let accent = rgb(250, 178, 131);
        Self {
            mode: Mode::Dark,
            bg: rgb(20, 20, 20),
            text: rgb(225, 225, 225),
            muted: rgb(120, 120, 120),
            quiet: rgb(88, 88, 88),
            accent,
            surface,
            raised: rgb(20, 20, 20),
            element: rgb(36, 36, 36),
            element_hi: rgb(44, 44, 44),
            border: rgb(50, 50, 55),
            border_strong: rgb(80, 80, 88),
            focus_bg: mix(surface, accent, 0.12),
            blue: rgb(122, 162, 247),
            purple: rgb(187, 154, 247),
            cyan: rgb(125, 207, 255),
            success: rgb(158, 206, 106),
            warning: rgb(224, 175, 104),
            error: rgb(247, 118, 142),
        }
    }
    pub fn light() -> Self {
        // Grok Day; keep in step with `rust/tui` `Palette::new(true)`.
        let rgb = Color::Rgb;
        let surface = rgb(245, 245, 245);
        let accent = rgb(200, 103, 47);
        Self {
            mode: Mode::Light,
            bg: rgb(245, 245, 245),
            text: rgb(38, 38, 38),
            muted: rgb(118, 118, 118),
            quiet: rgb(142, 142, 142),
            accent,
            surface,
            raised: rgb(245, 245, 245),
            element: rgb(228, 228, 228),
            element_hi: rgb(218, 218, 218),
            border: rgb(200, 200, 205),
            border_strong: rgb(165, 165, 175),
            focus_bg: mix(surface, accent, 0.10),
            blue: rgb(47, 100, 210),
            purple: rgb(108, 62, 178),
            cyan: rgb(0, 130, 170),
            success: rgb(55, 142, 35),
            warning: rgb(162, 118, 18),
            error: rgb(205, 48, 72),
        }
    }
    /// No colour at all: terminal defaults plus modifiers.
    pub fn mono() -> Self {
        let r = Color::Reset;
        Self {
            mode: Mode::Mono,
            bg: r,
            text: r,
            muted: r,
            quiet: r,
            accent: r,
            surface: r,
            raised: r,
            element: r,
            element_hi: r,
            border: r,
            border_strong: r,
            focus_bg: r,
            blue: r,
            purple: r,
            cyan: r,
            success: r,
            warning: r,
            error: r,
        }
    }
    pub fn is_mono(&self) -> bool {
        self.mode == Mode::Mono
    }
    pub fn name(&self) -> &'static str {
        match self.mode {
            Mode::Dark => "dark",
            Mode::Light => "light",
            Mode::Mono => "mono",
        }
    }
    pub fn base(&self) -> Style {
        Style::default().fg(self.text).bg(self.bg)
    }
    pub fn on(&self, fg: Color, bg: Color) -> Style {
        Style::default().fg(fg).bg(bg)
    }
    /// Mono replacement for colour emphasis.
    pub fn strong(&self, s: Style) -> Style {
        if self.is_mono() {
            s.add_modifier(Modifier::BOLD)
        } else {
            s
        }
    }
    pub fn text_style(&self) -> Style {
        Style::default().fg(self.text)
    }
    pub fn dim(&self) -> Style {
        let s = Style::default().fg(self.muted);
        if self.is_mono() {
            s.add_modifier(Modifier::DIM)
        } else {
            s
        }
    }
    pub fn quiet_style(&self) -> Style {
        let s = Style::default().fg(self.quiet);
        if self.is_mono() {
            s.add_modifier(Modifier::DIM)
        } else {
            s
        }
    }

    // Neutral roles (Grok Build plan §3.1). Methods, not fields, so struct
    // literals of `Theme` elsewhere keep compiling. Each maps to an existing
    // colour, so adopting a role changes nothing visible.
    /// Tree connectors, meta punctuation.
    pub fn gray_dim(&self) -> Color {
        self.border_strong
    }
    /// Member rows, folded thoughts.
    pub fn gray(&self) -> Color {
        self.quiet
    }
    /// Verb-group header text.
    pub fn gray_bright(&self) -> Color {
        self.muted
    }
    /// Thinking and group guide bars.
    pub fn rail(&self) -> Color {
        self.border
    }
    /// Live headers, spinner, wave peak. Never the agent colour.
    pub fn running(&self) -> Color {
        self.text
    }
    /// Wave trough for running text (`gray_bright`).
    pub fn running_base(&self) -> Color {
        self.muted
    }
    /// Active context header titles and rails.
    pub fn header(&self) -> Color {
        self.muted
    }
    /// Empty or inactive context headers.
    pub fn header_inactive(&self) -> Color {
        self.quiet
    }
    /// Reasoning body: quiet, dim and italic. Modifiers are off in mono.
    pub fn thinking(&self) -> Style {
        let s = Style::default().fg(self.quiet);
        if self.is_mono() {
            s
        } else {
            s.add_modifier(Modifier::DIM | Modifier::ITALIC)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn neutral_roles_map_to_existing_colours_in_every_mode() {
        for t in [Theme::dark(), Theme::light(), Theme::mono()] {
            assert_eq!(t.gray_dim(), t.border_strong);
            assert_eq!(t.gray(), t.quiet);
            assert_eq!(t.gray_bright(), t.muted);
            assert_eq!(t.rail(), t.border);
            assert_eq!(t.running(), t.text);
            assert_eq!(t.running_base(), t.muted);
            assert_eq!(t.header(), t.muted);
            assert_eq!(t.header_inactive(), t.quiet);
            assert_eq!(t.thinking().fg, Some(t.quiet));
        }
    }

    #[test]
    fn thinking_dim_italic_off_in_mono() {
        let both = Modifier::DIM | Modifier::ITALIC;
        for t in [Theme::dark(), Theme::light()] {
            assert!(t.thinking().add_modifier.contains(both));
        }
        assert!(Theme::mono().thinking().add_modifier.is_empty());
    }

    #[test]
    fn roles_never_use_the_accent() {
        for t in [Theme::dark(), Theme::light()] {
            for c in [t.running(), t.header(), t.gray(), t.rail()] {
                assert_ne!(c, t.accent);
            }
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Level {
    Info,
    Success,
    Warning,
    Error,
}
impl Level {
    pub fn color(self, t: &Theme) -> Color {
        match self {
            Level::Info => t.blue,
            Level::Success => t.success,
            Level::Warning => t.warning,
            Level::Error => t.error,
        }
    }
    pub fn label(self) -> &'static str {
        match self {
            Level::Info => "info",
            Level::Success => "success",
            Level::Warning => "warning",
            Level::Error => "error",
        }
    }
}
