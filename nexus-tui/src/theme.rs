//! Theme: `interface/theme.toml` embedded at build time, parsed into typed
//! structs and resolved to Ratatui styles for the active mode (dark/light),
//! colour tier (truecolor, 256, ANSI-16, monochrome) and glyph set (Unicode or
//! ASCII). No colour or glyph is hard-coded in the renderer.

use std::collections::BTreeMap;

use ratatui::style::{Color, Modifier, Style};
use ratatui::text::Span;
use serde::Deserialize;
use unicode_segmentation::UnicodeSegmentation;

/// Soft travelling shimmer adapted from old Nexus `rust/tui/src/render.rs`.
/// Grapheme boundaries keep combined characters intact. Static when animation is disabled.
pub fn shimmer(
    word: &str,
    frame: usize,
    animate: bool,
    base: Style,
    peak: Style,
) -> Vec<Span<'static>> {
    if !animate {
        return vec![Span::styled(word.to_owned(), base)];
    }
    let count = word.graphemes(true).count() as f32;
    let reach = 4.0_f32;
    let head = (frame % 54) as f32 / 54.0 * (count + 2.0 * reach) - reach;
    word.graphemes(true)
        .enumerate()
        .map(|(index, grapheme)| {
            let near = (1.0 - (index as f32 - head).abs() / reach).clamp(0.0, 1.0);
            let amount = near * near * (3.0 - 2.0 * near);
            let color = match (base.fg, peak.fg) {
                (Some(Color::Rgb(r, g, b)), Some(Color::Rgb(pr, pg, pb))) => {
                    let mix = |a: u8, b: u8| {
                        (f32::from(a) + (f32::from(b) - f32::from(a)) * amount).round() as u8
                    };
                    Some(Color::Rgb(mix(r, pr), mix(g, pg), mix(b, pb)))
                }
                (a, b) => {
                    if amount > 0.5 {
                        b.or(a)
                    } else {
                        a
                    }
                }
            };
            Span::styled(
                grapheme.to_owned(),
                color.map_or(base, |color| base.fg(color)),
            )
        })
        .collect()
}

const SOURCE: &str = include_str!("../../interface/theme.toml");

type Tokens = BTreeMap<String, BTreeMap<String, String>>;

#[derive(Debug, Clone, Deserialize)]
pub struct ThemeData {
    pub dark: Tokens,
    pub light: Tokens,
    pub roles: BTreeMap<String, RoleDef>,
    pub agent_pins: BTreeMap<String, String>,
    pub fallback256: Fallback<u8>,
    pub fallback16: Fallback<String>,
    pub monochrome: Mono,
    pub glyphs: Glyphs,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Fallback<T> {
    pub dark: BTreeMap<String, T>,
    pub light: BTreeMap<String, T>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct RoleDef {
    pub fg: String,
    pub bg: String,
    pub rail: String,
    pub label: String,
    pub style: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Mono {
    pub tokens: BTreeMap<String, Vec<String>>,
    pub roles: BTreeMap<String, Vec<String>>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Pair {
    pub unicode: String,
    pub ascii: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct StatusGlyph {
    pub unicode: String,
    pub ascii: String,
    pub label: String,
    pub token: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct AttentionGlyph {
    pub unicode: String,
    pub ascii: String,
    pub token: String,
    pub rank: u32,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Spinner {
    pub unicode: Vec<String>,
    pub ascii: Vec<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Glyphs {
    pub status: BTreeMap<String, StatusGlyph>,
    pub spinner: Spinner,
    pub fold: BTreeMap<String, Pair>,
    pub sidebar: BTreeMap<String, Pair>,
    pub attention: BTreeMap<String, AttentionGlyph>,
    pub structure: BTreeMap<String, Pair>,
}

impl ThemeData {
    pub fn load() -> Self {
        toml::from_str(SOURCE)
            .expect("interface/theme.toml is invalid; run interface/check_theme.py")
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    Dark,
    Light,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tier {
    TrueColor,
    Ansi256,
    Ansi16,
    Mono,
}

/// Environment-derived options (also settable by flags).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Options {
    pub mode: Mode,
    pub tier: Tier,
    pub ascii: bool,
    pub reduced_motion: bool,
}

impl Options {
    /// Detect from the environment: `NO_COLOR`, `TERM`, `COLORTERM`,
    /// `COLORFGBG`, `NEXUS10_ASCII`, `NEXUS10_REDUCED_MOTION`, locale.
    pub fn from_env() -> Self {
        let get = |k: &str| std::env::var(k).ok().filter(|v| !v.is_empty());
        let term = get("TERM").unwrap_or_default();
        let tier = if get("NO_COLOR").is_some() || term == "dumb" {
            Tier::Mono
        } else if matches!(get("COLORTERM").as_deref(), Some("truecolor" | "24bit")) {
            Tier::TrueColor
        } else if term.contains("256color") {
            Tier::Ansi256
        } else if term.is_empty() {
            Tier::TrueColor
        } else {
            Tier::Ansi16
        };
        // COLORFGBG is "fg;bg": a light background has index 7 or 9..=15.
        let mode = match get("COLORFGBG")
            .and_then(|v| v.rsplit(';').next().and_then(|b| b.parse::<u8>().ok()))
        {
            Some(b) if b == 7 || (9..=15).contains(&b) => Mode::Light,
            _ => Mode::Dark,
        };
        let locale = ["LC_ALL", "LC_CTYPE", "LANG"].iter().find_map(|k| get(k));
        let non_utf8 = locale.is_some_and(|l| {
            let l = l.to_ascii_lowercase();
            !(l.contains("utf-8") || l.contains("utf8"))
        });
        Self {
            mode,
            tier,
            ascii: get("NEXUS10_ASCII").is_some() || non_utf8,
            reduced_motion: get("NEXUS10_REDUCED_MOTION").is_some()
                || get("REDUCE_MOTION").is_some(),
        }
    }
}

impl Default for Options {
    fn default() -> Self {
        Self {
            mode: Mode::Dark,
            tier: Tier::TrueColor,
            ascii: false,
            reduced_motion: false,
        }
    }
}

#[derive(Debug, Clone)]
pub struct Theme {
    data: std::sync::Arc<ThemeData>,
    pub opts: Options,
}

fn hex(s: &str) -> Option<Color> {
    let s = s.strip_prefix('#')?;
    if s.len() != 6 {
        return None;
    }
    let n = u32::from_str_radix(s, 16).ok()?;
    Some(Color::Rgb((n >> 16) as u8, (n >> 8) as u8, n as u8))
}

fn ansi(name: &str) -> Option<Color> {
    Some(match name {
        "black" => Color::Black,
        "red" => Color::Red,
        "green" => Color::Green,
        "yellow" => Color::Yellow,
        "blue" => Color::Blue,
        "magenta" => Color::Magenta,
        "cyan" => Color::Cyan,
        "white" => Color::Gray,
        "bright_black" => Color::DarkGray,
        "bright_white" => Color::White,
        _ => return None, // "default"
    })
}

fn modifier(name: &str) -> Modifier {
    match name {
        "bold" => Modifier::BOLD,
        "dim" => Modifier::DIM,
        "reverse" => Modifier::REVERSED,
        "underline" => Modifier::UNDERLINED,
        "italic" => Modifier::ITALIC,
        _ => Modifier::empty(),
    }
}

impl Theme {
    pub fn new(opts: Options) -> Self {
        Self {
            data: std::sync::Arc::new(ThemeData::load()),
            opts,
        }
    }

    pub fn with(&self, opts: Options) -> Self {
        Self {
            data: self.data.clone(),
            opts,
        }
    }

    pub fn data(&self) -> &ThemeData {
        &self.data
    }

    pub fn mono(&self) -> bool {
        self.opts.tier == Tier::Mono
    }

    fn tokens(&self) -> &Tokens {
        match self.opts.mode {
            Mode::Dark => &self.data.dark,
            Mode::Light => &self.data.light,
        }
    }

    /// The colour of a token such as `text.primary`, or `None` when the tier has
    /// no colour for it (monochrome, or ANSI-16 "default").
    pub fn color(&self, token: &str) -> Option<Color> {
        let (group, name) = token.split_once('.')?;
        match self.opts.tier {
            Tier::Mono => None,
            Tier::TrueColor => self.tokens().get(group)?.get(name).and_then(|v| hex(v)),
            Tier::Ansi256 => {
                let f = match self.opts.mode {
                    Mode::Dark => &self.data.fallback256.dark,
                    Mode::Light => &self.data.fallback256.light,
                };
                f.get(token).map(|i| Color::Indexed(*i))
            }
            Tier::Ansi16 => {
                let f = match self.opts.mode {
                    Mode::Dark => &self.data.fallback16.dark,
                    Mode::Light => &self.data.fallback16.light,
                };
                f.get(token).and_then(|n| ansi(n))
            }
        }
    }

    /// Stable identity slot. Core agent names are restricted to lowercase ASCII.
    pub fn agent_slot(&self, name: &str) -> &str {
        const SLOTS: [&str; 8] = [
            "amber", "blue", "violet", "green", "teal", "rose", "sky", "gold",
        ];
        let name = name.to_ascii_lowercase();
        if let Some(slot) = self.data.agent_pins.get(&name) {
            return slot;
        }
        let hash = name.bytes().fold(0x811c9dc5u32, |h, byte| {
            (h ^ u32::from(byte)).wrapping_mul(0x01000193)
        });
        let start = hash as usize % SLOTS.len();
        for offset in 0..SLOTS.len() {
            let slot = SLOTS[(start + offset) % SLOTS.len()];
            if !self.data.agent_pins.values().any(|p| p == slot) {
                return slot;
            }
        }
        SLOTS[start]
    }

    pub fn agent_style(&self, name: &str) -> Style {
        self.fg(&format!("agent.{}", self.agent_slot(name)))
    }

    /// Text attributes a token carries: the monochrome mapping, plus `dim` for
    /// muted text on ANSI-16 where colours alone cannot tell it apart.
    fn mods(&self, token: &str) -> Modifier {
        match self.opts.tier {
            Tier::Mono => self
                .data
                .monochrome
                .tokens
                .get(token)
                .map(|v| v.iter().fold(Modifier::empty(), |m, n| m | modifier(n)))
                .unwrap_or_default(),
            Tier::Ansi16 if token == "text.muted" => Modifier::DIM,
            _ => Modifier::empty(),
        }
    }

    /// Foreground style of a token.
    pub fn fg(&self, token: &str) -> Style {
        let mut s = Style::default().add_modifier(self.mods(token));
        if let Some(c) = self.color(token) {
            s = s.fg(c);
        }
        s
    }

    /// Background style of a token. In monochrome, surfaces are empty and
    /// selection is reverse video.
    pub fn bg(&self, token: &str) -> Style {
        match self.opts.tier {
            Tier::Mono => Style::default().add_modifier(match token {
                "interactive.selection" => Modifier::REVERSED,
                "interactive.hover" => Modifier::UNDERLINED,
                _ => Modifier::empty(),
            }),
            _ => match self.color(token) {
                Some(c) => Style::default().bg(c),
                None => Style::default(),
            },
        }
    }

    pub fn style(&self, fg: &str, bg: &str) -> Style {
        self.fg(fg).patch(self.bg(bg))
    }

    pub fn bold(&self, s: Style) -> Style {
        s.add_modifier(Modifier::BOLD)
    }

    // -- roles ---------------------------------------------------------------

    pub fn role(&self, name: &str) -> &RoleDef {
        self.data
            .roles
            .get(name)
            .unwrap_or_else(|| panic!("theme role {name}"))
    }

    /// Text style of a timeline role, honouring the monochrome mapping.
    pub fn role_style(&self, name: &str) -> Style {
        let r = self.role(name);
        if self.mono() {
            let m = self
                .data
                .monochrome
                .roles
                .get(name)
                .map(|v| v.iter().fold(Modifier::empty(), |m, n| m | modifier(n)))
                .unwrap_or_default();
            return Style::default().add_modifier(m);
        }
        let mut s = self.style(&r.fg, &r.bg);
        if r.style == "italic" {
            s = s.add_modifier(Modifier::ITALIC);
        }
        s
    }

    pub fn role_rail_style(&self, name: &str) -> Style {
        let r = self.role(name);
        if r.rail.is_empty() {
            Style::default()
        } else {
            self.fg(&r.rail).patch(self.bg(&r.bg))
        }
    }

    // -- glyphs --------------------------------------------------------------

    fn pick<'a>(&self, p: &'a Pair) -> &'a str {
        if self.opts.ascii {
            &p.ascii
        } else {
            &p.unicode
        }
    }

    pub fn status(&self, name: &str) -> (&str, &str, &str) {
        let g = &self.data.glyphs.status[name];
        let glyph = if self.opts.ascii {
            &g.ascii
        } else {
            &g.unicode
        };
        (glyph, &g.label, &g.token)
    }

    pub fn attention(&self, name: &str) -> (&str, &str) {
        let g = &self.data.glyphs.attention[name];
        (
            if self.opts.ascii {
                &g.ascii
            } else {
                &g.unicode
            },
            &g.token,
        )
    }

    pub fn fold(&self, open: bool) -> &str {
        self.pick(&self.data.glyphs.fold[if open { "open" } else { "closed" }])
    }

    pub fn sidebar_glyph(&self, name: &str) -> &str {
        self.pick(&self.data.glyphs.sidebar[name])
    }

    pub fn structure(&self, name: &str) -> &str {
        self.pick(&self.data.glyphs.structure[name])
    }

    /// Spinner frame; the static running glyph under reduced motion.
    pub fn spinner(&self, frame: usize) -> &str {
        if self.opts.reduced_motion {
            return self.status("running").0;
        }
        let frames = if self.opts.ascii {
            &self.data.glyphs.spinner.ascii
        } else {
            &self.data.glyphs.spinner.unicode
        };
        &frames[frame % frames.len()]
    }

    /// The finished subagent's mark: the spinner with every dot on.
    pub fn dots_full(&self) -> &'static str {
        if self.opts.ascii {
            "#"
        } else {
            "\u{28ff}"
        }
    }

    /// Old Nexus modal frame: rounded Unicode hairlines or ASCII.
    pub fn border_set(&self) -> ratatui::symbols::border::Set {
        if self.opts.ascii {
            ratatui::symbols::border::Set {
                top_left: "+",
                top_right: "+",
                bottom_left: "+",
                bottom_right: "+",
                vertical_left: "|",
                vertical_right: "|",
                horizontal_top: "-",
                horizontal_bottom: "-",
            }
        } else {
            ratatui::symbols::border::ROUNDED
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn agent_pins_and_hash_are_stable() {
        let theme = Theme::new(Options::default());
        for (name, slot) in [
            ("orchestrate", "amber"),
            ("build", "blue"),
            ("quick", "green"),
            ("scout", "teal"),
            ("plan", "violet"),
            ("review", "rose"),
            ("tester", "sky"),
        ] {
            assert_eq!(theme.agent_slot(name), slot);
        }
        assert_eq!(theme.agent_slot("BUILD"), "blue");
    }

    fn all_opts() -> Vec<Options> {
        let mut v = Vec::new();
        for mode in [Mode::Dark, Mode::Light] {
            for tier in [Tier::TrueColor, Tier::Ansi256, Tier::Ansi16, Tier::Mono] {
                for ascii in [false, true] {
                    v.push(Options {
                        mode,
                        tier,
                        ascii,
                        reduced_motion: false,
                    });
                }
            }
        }
        v
    }

    #[test]
    fn every_token_resolves_in_every_tier() {
        for o in all_opts() {
            let t = Theme::new(o);
            for (group, names) in t.tokens() {
                for name in names.keys() {
                    let tok = format!("{group}.{name}");
                    let c = t.color(&tok);
                    match o.tier {
                        Tier::Mono => assert!(c.is_none() && t.mono()),
                        Tier::TrueColor | Tier::Ansi256 => {
                            assert!(c.is_some(), "{tok} {:?}", o.tier)
                        }
                        Tier::Ansi16 => {} // "default" is None
                    }
                }
            }
        }
    }

    #[test]
    fn monochrome_uses_only_attributes() {
        let t = Theme::new(Options {
            tier: Tier::Mono,
            ..Options::default()
        });
        for tok in [
            "text.primary",
            "text.muted",
            "status.error",
            "interactive.accent",
        ] {
            let s = t.fg(tok);
            assert!(s.fg.is_none() && s.bg.is_none());
        }
        assert!(t.fg("text.muted").add_modifier.contains(Modifier::DIM));
        assert!(t
            .bg("interactive.selection")
            .add_modifier
            .contains(Modifier::REVERSED));
        assert!(t.bg("surface.panel").bg.is_none());
        assert!(!t
            .role_style("reasoning")
            .add_modifier
            .contains(Modifier::ITALIC));
    }

    #[test]
    fn ascii_glyphs_are_single_cell_ascii() {
        let t = Theme::new(Options {
            ascii: true,
            ..Options::default()
        });
        for name in t.data().glyphs.status.keys() {
            assert!(t.status(name).0.is_ascii());
        }
        assert_eq!(t.sidebar_glyph("left_open"), "[|");
        assert_eq!(t.structure("focus_bar"), ">");
        assert!(t.spinner(3).is_ascii());
    }

    #[test]
    fn reduced_motion_freezes_the_spinner() {
        let t = Theme::new(Options {
            reduced_motion: true,
            ..Options::default()
        });
        assert_eq!(t.spinner(0), t.spinner(5));
        assert_eq!(t.spinner(0), "◐");
    }

    #[test]
    fn dark_accent_matches_the_theme_file() {
        let t = Theme::new(Options::default());
        assert_eq!(
            t.color("interactive.accent"),
            Some(Color::Rgb(0xe8, 0xa4, 0x6e))
        );
    }
}
