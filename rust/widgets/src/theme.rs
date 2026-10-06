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
        let rgb = Color::Rgb;
        let surface = rgb(20, 20, 20);
        let accent = rgb(250, 178, 131);
        Self {
            mode: Mode::Dark,
            bg: rgb(11, 11, 11),
            text: rgb(238, 238, 238),
            muted: rgb(163, 163, 163),
            quiet: rgb(111, 111, 111),
            accent,
            surface,
            raised: rgb(20, 20, 20),
            element: rgb(30, 30, 30),
            element_hi: rgb(40, 40, 40),
            border: rgb(44, 44, 44),
            border_strong: rgb(72, 72, 72),
            focus_bg: mix(surface, accent, 0.12),
            blue: rgb(92, 156, 245),
            purple: rgb(157, 124, 216),
            cyan: rgb(86, 212, 221),
            success: rgb(127, 216, 143),
            warning: rgb(245, 167, 66),
            error: rgb(224, 108, 117),
        }
    }
    pub fn light() -> Self {
        let rgb = Color::Rgb;
        let surface = rgb(245, 245, 244);
        let accent = rgb(200, 103, 47);
        Self {
            mode: Mode::Light,
            bg: rgb(255, 255, 255),
            text: rgb(27, 27, 27),
            muted: rgb(85, 85, 85),
            quiet: rgb(138, 138, 138),
            accent,
            surface,
            raised: rgb(255, 255, 255),
            element: rgb(236, 236, 234),
            element_hi: rgb(226, 226, 223),
            border: rgb(220, 220, 216),
            border_strong: rgb(185, 185, 180),
            focus_bg: mix(surface, accent, 0.10),
            blue: rgb(47, 111, 214),
            purple: rgb(122, 82, 199),
            cyan: rgb(14, 116, 144),
            success: rgb(38, 128, 68),
            warning: rgb(168, 98, 0),
            error: rgb(194, 58, 74),
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
