//! The proposed theme: `../theme.toml` (a copy of `interface/theme.toml` with the
//! DESIGN_PLAN.md token changes applied). Screens ask for tokens, never colours.
use ratatui::style::{Color, Modifier, Style};
use std::collections::BTreeMap;

pub const SOURCE: &str = include_str!("../../theme.toml");

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Mode {
    Dark,
    Light,
    Mono,
}

impl Mode {
    pub fn name(self) -> &'static str {
        match self {
            Mode::Dark => "dark",
            Mode::Light => "light",
            Mode::Mono => "mono",
        }
    }
    pub fn parse(s: &str) -> Option<Mode> {
        match s {
            "dark" => Some(Mode::Dark),
            "light" => Some(Mode::Light),
            "mono" => Some(Mode::Mono),
            _ => None,
        }
    }
}

pub struct Th {
    pub mode: Mode,
    colors: BTreeMap<String, (u8, u8, u8)>,
    mono: BTreeMap<String, Vec<String>>,
    pins: BTreeMap<String, String>,
    pub ascii: bool,
}

fn parse_hex(s: &str) -> Option<(u8, u8, u8)> {
    let s = s.strip_prefix('#')?;
    if s.len() != 6 {
        return None;
    }
    let p = |i: usize| u8::from_str_radix(&s[i..i + 2], 16).ok();
    Some((p(0)?, p(2)?, p(4)?))
}

impl Th {
    pub fn new(mode: Mode) -> Th {
        let doc: toml::Table = SOURCE.parse().expect("theme.toml parses");
        let palette = if mode == Mode::Light { "light" } else { "dark" };
        let mut colors = BTreeMap::new();
        if let Some(toml::Value::Table(groups)) = doc.get(palette) {
            for (group, vals) in groups {
                if let toml::Value::Table(vals) = vals {
                    for (k, v) in vals {
                        if let Some(rgb) = v.as_str().and_then(parse_hex) {
                            colors.insert(format!("{group}.{k}"), rgb);
                        }
                    }
                }
            }
        }
        let mut mono = BTreeMap::new();
        if let Some(t) = doc.get("monochrome").and_then(|m| m.get("tokens")).and_then(|t| t.as_table()) {
            for (k, v) in t {
                let mods = v.as_array().map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect()).unwrap_or_default();
                mono.insert(k.clone(), mods);
            }
        }
        let mut pins = BTreeMap::new();
        if let Some(t) = doc.get("agent_pins").and_then(|t| t.as_table()) {
            for (k, v) in t {
                pins.insert(k.clone(), v.as_str().unwrap_or("blue").to_string());
            }
        }
        Th { mode, colors, mono, pins, ascii: false }
    }

    /// Override one token (alternative palettes in the chooser).
    pub fn set(&mut self, tok: &str, hex: &str) {
        if let Some(rgb) = parse_hex(hex) {
            self.colors.insert(tok.to_string(), rgb);
        }
    }

    /// The raw colour of a token (mono maps to a neutral grey ramp).
    pub fn color(&self, tok: &str) -> Color {
        if self.mode == Mode::Mono {
            return match tok {
                t if t.starts_with("surface.") => Color::Rgb(0x10, 0x10, 0x10),
                _ => Color::Rgb(0xd8, 0xd8, 0xd8),
            };
        }
        match self.colors.get(tok) {
            Some(&(r, g, b)) => Color::Rgb(r, g, b),
            None => panic!("unknown token {tok}"),
        }
    }

    fn mono_mods(&self, tok: &str) -> Modifier {
        let mut m = Modifier::empty();
        for x in self.mono.get(tok).into_iter().flatten() {
            m |= match x.as_str() {
                "bold" => Modifier::BOLD,
                "dim" => Modifier::DIM,
                "reverse" => Modifier::REVERSED,
                "underline" => Modifier::UNDERLINED,
                _ => Modifier::empty(),
            };
        }
        m
    }

    /// Foreground style for a token.
    pub fn fg(&self, tok: &str) -> Style {
        if self.mode == Mode::Mono {
            return Style::default().fg(self.color(tok)).add_modifier(self.mono_mods(tok));
        }
        Style::default().fg(self.color(tok))
    }

    /// Background-only style. Mono has no fills except selection (reverse).
    pub fn bg(&self, tok: &str) -> Style {
        if self.mode == Mode::Mono {
            return if tok == "interactive.selection" { Style::default().add_modifier(Modifier::REVERSED) } else { Style::default() };
        }
        Style::default().bg(self.color(tok))
    }

    /// Blend two tokens (for shimmer frames and soft tints). `t` in 0..=1.
    pub fn mix(&self, a: &str, b: &str, t: f32) -> Color {
        match (self.color(a), self.color(b)) {
            (Color::Rgb(r1, g1, b1), Color::Rgb(r2, g2, b2)) => {
                let m = |x: u8, y: u8| (x as f32 + (y as f32 - x as f32) * t).round() as u8;
                Color::Rgb(m(r1, r2), m(g1, g2), m(b1, b2))
            }
            (c, _) => c,
        }
    }

    /// Agent identity colour: built-ins are pinned, others hash to rose/sky/gold.
    pub fn agent(&self, name: &str) -> Style {
        let slot = self.pins.get(&name.to_lowercase()).cloned().unwrap_or_else(|| {
            let h = name.to_lowercase().bytes().fold(0x811c9dc5u32, |h, b| (h ^ b as u32).wrapping_mul(0x01000193));
            ["rose", "sky", "gold"][(h % 3) as usize].to_string()
        });
        self.fg(&format!("agent.{slot}"))
    }

    pub fn primary(&self) -> Style {
        self.fg("text.primary")
    }
    pub fn secondary(&self) -> Style {
        self.fg("text.secondary")
    }
    pub fn muted(&self) -> Style {
        self.fg("text.muted")
    }
    pub fn accent(&self) -> Style {
        self.fg("interactive.accent")
    }
    pub fn hair(&self) -> Style {
        self.fg("border.default")
    }

    /// Default page colours for the SVG writer.
    pub fn page(&self) -> (&'static str, String) {
        match self.mode {
            Mode::Mono => ("#d8d8d8", "#101010".into()),
            _ => {
                let fg = if self.mode == Mode::Light { "#1f2124" } else { "#e6e7e8" };
                let (r, g, b) = self.colors["surface.canvas"];
                (fg, format!("#{r:02x}{g:02x}{b:02x}"))
            }
        }
    }

    /// Glyph lookup with the ASCII fallback from theme.toml.
    pub fn g(&self, name: &str) -> &'static str {
        let (u, a) = match name {
            "running" => ("◐", "~"),
            "done" => ("✓", "+"),
            "error" => ("✕", "x"),
            "denied" => ("⊘", "-"),
            "waiting" => ("◆", "?"),
            "interrupted" => ("◼", "!"),
            "cancelled" => ("⊖", "o"),
            "unread" => ("●", "*"),
            "open" => ("▾", "v"),
            "closed" => ("▸", ">"),
            "focus" => ("▌", ">"),
            "rail" => ("│", "|"),
            "rail_heavy" => ("┃", "|"),
            "tree_mid" => ("├─", "|-"),
            "tree_end" => ("╰─", "`-"),
            "rule" => ("─", "-"),
            "dots" => ("┄", "-"),
            "ellipsis" => ("…", "..."),
            "sep" => ("·", "-"),
            "crumb" => ("›", ">"),
            "arrow" => ("→", "->"),
            "close" => ("×", "x"),
            "branch" => ("⌥", "~"),
            "child" => ("↳", "^"),
            "left_open" => ("◧", "[|"),
            "right_open" => ("◨", "|]"),
            "closed_box" => ("□", "[ "),
            "sessions" => ("≡", "S"),
            "files" => ("▤", "F"),
            "projects" => ("◫", "P"),
            "search" => ("⌕", "/"),
            "bell" => ("◔", "!"),
            "diamond" => ("◇", "<>"),
            "square" => ("▪", "#"),
            "block_full" => ("█", "#"),
            "block_light" => ("░", "."),
            "tick" => ("╎", "|"),
            "spark" => ("✦", "*"),
            _ => panic!("unknown glyph {name}"),
        };
        if self.ascii { a } else { u }
    }
}
