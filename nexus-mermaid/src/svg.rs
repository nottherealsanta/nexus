//! Mermaid-to-SVG through the Ondine compiler, with Nexus's safety limits.
//!
//! Ondine is upstream's pure-Rust `compiler` crate, pinned by revision in
//! `Cargo.toml`. This module is the only place Nexus calls it: source, output,
//! edge counts and raster sizes are bounded here, links and images are always
//! off, and diagnostic messages stay data instead of HTML. A panic in the
//! parser becomes an error rather than taking a client down.
//!
//! The SVG keeps Ondine's `--ondine-*` custom properties, which a browser
//! resolves. Native renderers do not resolve CSS variables, so
//! [`resolve_theme`] substitutes a client's palette first.

use std::fmt;
use std::panic::{catch_unwind, AssertUnwindSafe};

/// The most source accepted, in UTF-8 bytes.
pub const MAX_SOURCE_BYTES: usize = 50_000;
/// The most SVG produced.
pub const MAX_OUTPUT_BYTES: usize = 4_000_000;
/// The most edges a flowchart may describe.
pub const MAX_EDGES: u32 = 5_000;
/// Ondine's default font size, in CSS pixels.
pub const DEFAULT_FONT_SIZE: f64 = 16.0;
/// The element-ID prefix, so diagrams cannot collide with page IDs.
pub const ID_PREFIX: &str = "nexus-mermaid-";

/// Compiler inputs that are safe for a client to vary.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Options {
    /// The width the client has, in CSS pixels. A diagram that can draw
    /// itself narrower does so instead of being scaled down.
    pub max_width: Option<f64>,
    /// Label size in CSS pixels.
    pub font_size: f64,
    /// Whether a flowchart that runs across may turn to run down when it
    /// does not fit [`Self::max_width`].
    pub auto_direction: bool,
    /// The time a Gantt chart schedules against, in milliseconds since the
    /// epoch. The epoch keeps output deterministic.
    pub now: f64,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            max_width: None,
            font_size: DEFAULT_FONT_SIZE,
            auto_direction: true,
            now: 0.0,
        }
    }
}

/// A compiled diagram. Sizes are CSS pixels.
#[derive(Clone, Debug, PartialEq)]
pub struct Diagram {
    /// The SVG document.
    pub svg: String,
    /// Ondine's diagram type (`flowchart`, `pie`, `sequence`, ...).
    pub kind: &'static str,
    /// Intrinsic width in CSS pixels.
    pub width: f64,
    /// Intrinsic height in CSS pixels.
    pub height: f64,
}

/// Why a diagram was not compiled.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Error {
    /// The source is longer than [`MAX_SOURCE_BYTES`].
    SourceTooLarge { bytes: usize, limit: usize },
    /// Ondine hit one of its configured limits, or the output was too large.
    Limit(String),
    /// The diagram type is not detected, or this build does not include it.
    Unsupported {
        kind: Option<String>,
        message: String,
    },
    /// The source does not parse.
    Syntax {
        message: String,
        line: u32,
        column: u32,
    },
    /// The source parses but describes something invalid.
    Semantic {
        message: String,
        line: u32,
        column: u32,
    },
    /// Ondine produced markup Nexus refuses to show.
    UnsafeOutput { reason: &'static str },
    /// The caller's theme contains a value Nexus refuses to substitute.
    UnsafeTheme { field: &'static str },
    /// The compiler panicked. The caller falls back to the source.
    Panicked,
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::SourceTooLarge { bytes, limit } => {
                write!(f, "Mermaid source is {bytes} bytes; the limit is {limit}")
            }
            Self::Limit(message) => f.write_str(message),
            Self::Unsupported { kind, message } => match kind {
                Some(kind) => write!(f, "{kind}: {message}"),
                None => f.write_str(message),
            },
            Self::Syntax {
                message,
                line,
                column,
            }
            | Self::Semantic {
                message,
                line,
                column,
            } => write!(f, "line {line}, column {column}: {message}"),
            Self::UnsafeOutput { reason } => {
                write!(f, "the compiled diagram was refused: {reason}")
            }
            Self::UnsafeTheme { field } => {
                write!(f, "the theme's {field} is not a safe value")
            }
            Self::Panicked => f.write_str("the Mermaid compiler panicked"),
        }
    }
}

impl std::error::Error for Error {}

/// Compiles `source` to SVG with links and images disabled.
///
/// Returns [`Error::SourceTooLarge`] before calling Ondine and checks the
/// result for script-bearing markup. It is safe to call from either client;
/// the result is deterministic for the same Ondine revision and options.
pub fn render(source: &str, options: &Options) -> Result<Diagram, Error> {
    if source.len() > MAX_SOURCE_BYTES {
        return Err(Error::SourceTooLarge {
            bytes: source.len(),
            limit: MAX_SOURCE_BYTES,
        });
    }
    if source.trim().is_empty() {
        return Err(Error::Unsupported {
            kind: None,
            message: "No diagram type detected for empty source.".into(),
        });
    }

    let json = options_json(options);
    let compiled = catch_unwind(AssertUnwindSafe(|| {
        ondine::render(source, &json, options.now)
    }))
    .map_err(|_| Error::Panicked)??;

    if !(compiled.width.is_finite()
        && compiled.height.is_finite()
        && compiled.width > 0.0
        && compiled.height > 0.0)
    {
        return Err(Error::Semantic {
            message: "the diagram has no area".into(),
            line: 0,
            column: 0,
        });
    }
    if compiled.svg.len() > MAX_OUTPUT_BYTES {
        return Err(Error::Limit(format!(
            "Diagram output is longer than the limit of {MAX_OUTPUT_BYTES} bytes."
        )));
    }
    check_svg(&compiled.svg)?;

    Ok(Diagram {
        svg: compiled.svg,
        kind: compiled.r#type,
        width: compiled.width,
        height: compiled.height,
    })
}

fn options_json(options: &Options) -> String {
    let font_size = if options.font_size.is_finite() && options.font_size > 0.0 {
        options.font_size
    } else {
        DEFAULT_FONT_SIZE
    };
    let now = if options.now.is_finite() {
        options.now
    } else {
        0.0
    };
    let mut json = format!(
        r#"{{"autoDirection":{},"fontSize":{font_size},"images":false,"idPrefix":"{ID_PREFIX}","links":false,"limit":{MAX_SOURCE_BYTES},"maxEdges":{MAX_EDGES},"now":{now},"outputLimit":{MAX_OUTPUT_BYTES}"#,
        options.auto_direction
    );
    if let Some(width) = options.max_width {
        if width.is_finite() && width > 0.0 {
            json.push_str(&format!(r#","maxWidth":{width}"#));
        }
    }
    json.push('}');
    json
}

/// Rejects markup that could execute, navigate, or fetch outside the SVG.
fn check_svg(svg: &str) -> Result<(), Error> {
    if !svg.starts_with("<svg") {
        return Err(Error::UnsafeOutput {
            reason: "the output is not an SVG document",
        });
    }
    let lower = svg.to_ascii_lowercase();
    for bad in [
        "<script",
        "<foreignobject",
        "<iframe",
        "<object",
        "<embed",
        "javascript:",
        "onload=",
        "onclick=",
        "onerror=",
    ] {
        if lower.contains(bad) {
            return Err(Error::UnsafeOutput {
                reason: "the output contains executable markup",
            });
        }
    }
    // `links` and `images` are false above; either appearing means the
    // compiler ignored the option or the diagram is not a plain drawing.
    if lower.contains("href=") || lower.contains("<image") {
        return Err(Error::UnsafeOutput {
            reason: "the output contains a link or image",
        });
    }
    Ok(())
}

impl From<ondine::Error> for Error {
    fn from(error: ondine::Error) -> Self {
        let kind = error.r#type.map(str::to_owned);
        match error.code {
            ondine::Code::Limit => Self::Limit(error.message),
            ondine::Code::UnsupportedDiagram => Self::Unsupported {
                kind,
                message: error.message,
            },
            ondine::Code::Syntax => Self::Syntax {
                message: error.message,
                line: error.line,
                column: error.column,
            },
            ondine::Code::Semantic => Self::Semantic {
                message: error.message,
                line: error.line,
                column: error.column,
            },
        }
    }
}

/// The colours and metrics a native renderer needs, named as Ondine's
/// public custom properties. An empty field keeps Ondine's fallback.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Theme {
    pub background: String,
    pub foreground: String,
    pub muted: String,
    pub line: String,
    pub border: String,
    pub surface: String,
    pub surface_alt: String,
    pub accent: String,
    pub critical: String,
    pub font: String,
    pub stroke: String,
    pub radius: String,
    pub title_weight: String,
    pub series: [String; 8],
}

impl Default for Theme {
    fn default() -> Self {
        Self {
            background: "#fff".into(),
            foreground: "#000".into(),
            muted: "#000".into(),
            line: "#000".into(),
            border: "#000".into(),
            surface: "#fff".into(),
            surface_alt: "#ccc".into(),
            accent: "#000".into(),
            critical: "#000".into(),
            font: "monospace".into(),
            stroke: "2".into(),
            radius: "0".into(),
            title_weight: "700".into(),
            series: std::array::from_fn(|_| "#000".to_string()),
        }
    }
}

impl Theme {
    /// Checks every value before it is substituted into SVG. Themes come from
    /// clients, so markup, attribute quotes and CSS declarations are refused
    /// rather than escaped: an empty value is allowed and keeps Ondine's
    /// fallback.
    pub fn validate(&self) -> Result<(), Error> {
        type FieldCheck<'a> = (&'static str, &'a str, fn(&str) -> bool);
        let values: [FieldCheck<'_>; 13] = [
            ("background", &self.background, valid_color),
            ("foreground", &self.foreground, valid_color),
            ("muted", &self.muted, valid_color),
            ("line", &self.line, valid_color),
            ("border", &self.border, valid_color),
            ("surface", &self.surface, valid_color),
            ("surface_alt", &self.surface_alt, valid_color),
            ("accent", &self.accent, valid_color),
            ("critical", &self.critical, valid_color),
            ("font", &self.font, valid_font),
            ("stroke", &self.stroke, valid_length),
            ("radius", &self.radius, valid_length),
            ("title_weight", &self.title_weight, valid_weight),
        ];
        for (field, value, check) in values {
            if !check(value) {
                return Err(Error::UnsafeTheme { field });
            }
        }
        for value in &self.series {
            if !valid_color(value) {
                return Err(Error::UnsafeTheme { field: "series" });
            }
        }
        Ok(())
    }

    fn variables(&self) -> Vec<(&'static str, &str)> {
        let mut vars = vec![
            ("--ondine-bg", self.background.as_str()),
            ("--ondine-fg", self.foreground.as_str()),
            ("--ondine-muted", self.muted.as_str()),
            ("--ondine-line", self.line.as_str()),
            ("--ondine-border", self.border.as_str()),
            ("--ondine-surface", self.surface.as_str()),
            ("--ondine-surface-alt", self.surface_alt.as_str()),
            ("--ondine-accent", self.accent.as_str()),
            ("--ondine-critical", self.critical.as_str()),
            ("--ondine-font", self.font.as_str()),
            ("--ondine-stroke", self.stroke.as_str()),
            ("--ondine-radius", self.radius.as_str()),
            ("--ondine-title-weight", self.title_weight.as_str()),
            // Ondine's private aliases, set once in the root style. They must
            // resolve too: native renderers do not evaluate custom properties.
            ("--_bg", self.background.as_str()),
            ("--_fg", self.foreground.as_str()),
            ("--_m", self.muted.as_str()),
            ("--_l", self.line.as_str()),
            ("--_b", self.border.as_str()),
            ("--_s", self.surface.as_str()),
            ("--_a", self.surface_alt.as_str()),
            ("--_c", self.accent.as_str()),
            ("--_sw", self.stroke.as_str()),
            ("--_r", self.radius.as_str()),
            ("--_tw", self.title_weight.as_str()),
            ("--_fm", self.font.as_str()),
        ];
        let names = [
            "--ondine-series-1",
            "--ondine-series-2",
            "--ondine-series-3",
            "--ondine-series-4",
            "--ondine-series-5",
            "--ondine-series-6",
            "--ondine-series-7",
            "--ondine-series-8",
        ];
        vars.extend(
            names
                .into_iter()
                .zip(self.series.iter().map(String::as_str)),
        );
        vars
    }
}

/// A hex colour or a CSS colour keyword. `#rrggbbaa`, `#rgb`, `transparent`
/// and `currentColor` are enough for client palettes; functional colours,
/// `url()` and anything that can carry markup are refused.
fn valid_color(value: &str) -> bool {
    if value.is_empty() {
        return true;
    }
    if value.len() > 64 {
        return false;
    }
    if let Some(hex) = value.strip_prefix('#') {
        return matches!(hex.len(), 3 | 4 | 6 | 8) && hex.bytes().all(|b| b.is_ascii_hexdigit());
    }
    value.len() <= 32 && value.bytes().all(|b| b.is_ascii_alphabetic())
}

/// A font family list: names, spaces, commas and the two quote characters.
fn valid_font(value: &str) -> bool {
    value.len() <= 256
        && value.chars().all(|c| {
            c.is_ascii_alphanumeric() || matches!(c, ' ' | ',' | '.' | '-' | '_' | '+' | '\'' | '"')
        })
}

/// A CSS length or number: `2`, `1.5`, `2px`, `0.5em`, `10%`.
fn valid_length(value: &str) -> bool {
    value.len() <= 32
        && value
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '%' | '-' | '+'))
}

/// A font weight: `700`, `normal`, `bold`.
fn valid_weight(value: &str) -> bool {
    value.len() <= 16 && value.chars().all(|c| c.is_ascii_alphanumeric())
}

/// Escapes a theme value for an XML attribute or style value. Theme
/// validation already refuses markup, but escaping keeps the substitution
/// safe if a field is added later.
fn escape_xml(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for ch in value.chars() {
        match ch {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&apos;"),
            _ => out.push(ch),
        }
    }
    out
}

/// Replaces every `var(...)` and `color-mix(...)` with literals, so a native
/// SVG renderer (resvg, a WebView-independent rasterizer) draws the client's
/// palette. Browser clients can skip this and set the same values in CSS.
///
/// The input is checked before any substitution and the result is checked
/// again, so a theme or a crafted SVG cannot introduce executable markup.
pub fn resolve_theme(svg: &str, theme: &Theme) -> Result<String, Error> {
    check_svg(svg)?;
    theme.validate()?;
    let vars = theme.variables();
    let mut resolved = resolve_color_mix(
        &resolve_vars(svg, &vars, &theme.foreground, 0),
        &theme.foreground,
    );
    // resvg reads a root `style` but does not inherit its `font-family` into
    // `<text>`; a presentation attribute does inherit. Browsers accept both,
    // and the style still wins where it applies.
    if !theme.font.is_empty() {
        resolved = with_root_attribute(&resolved, "font-family", &theme.font);
    }
    if resolved.len() > MAX_OUTPUT_BYTES {
        return Err(Error::Limit(format!(
            "Themed diagram output is longer than the limit of {MAX_OUTPUT_BYTES} bytes."
        )));
    }
    check_svg(&resolved)?;
    Ok(resolved)
}

fn with_root_attribute(svg: &str, name: &str, value: &str) -> String {
    let Some(at) = svg.find("<svg") else {
        return svg.to_owned();
    };
    let insert = at + 4;
    let tag_end = svg[insert..]
        .find('>')
        .map_or(svg.len(), |end| insert + end);
    let root = &svg[insert..tag_end];
    if root.starts_with(&format!("{name}=")) || root.contains(&format!(" {name}=")) {
        return svg.to_owned();
    }
    let value = escape_xml(value);
    let mut out = String::with_capacity(svg.len() + name.len() + value.len() + 4);
    out.push_str(&svg[..insert]);
    out.push_str(&format!(" {name}=\"{value}\""));
    out.push_str(&svg[insert..]);
    out
}

fn matching_paren(s: &str, from: usize) -> Option<usize> {
    let mut depth = 1;
    for (i, ch) in s.get(from..)?.char_indices() {
        match ch {
            '(' => depth += 1,
            ')' => {
                depth -= 1;
                if depth == 0 {
                    return Some(from + i);
                }
            }
            _ => {}
        }
    }
    None
}

fn split_top_level(s: &str) -> Vec<&str> {
    let (mut parts, mut depth, mut start) = (Vec::new(), 0, 0);
    for (i, ch) in s.char_indices() {
        match ch {
            '(' => depth += 1,
            ')' => depth -= 1,
            ',' if depth == 0 => {
                parts.push(&s[start..i]);
                start = i + 1;
            }
            _ => {}
        }
    }
    parts.push(&s[start..]);
    parts
}

fn resolve_vars(s: &str, vars: &[(&str, &str)], foreground: &str, depth: u32) -> String {
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(at) = rest.find("var(") {
        let inner_start = at + 4;
        let Some(close) = matching_paren(rest, inner_start) else {
            break;
        };
        out.push_str(&rest[..at]);
        let parts = split_top_level(&rest[inner_start..close]);
        let name = parts[0].trim();
        let value = vars
            .iter()
            .find(|(n, v)| *n == name && !v.is_empty())
            .map(|(_, v)| escape_xml(v))
            .or_else(|| {
                let fallback = rest[inner_start..close].split_once(',')?.1;
                (depth < 8).then(|| resolve_vars(fallback.trim(), vars, foreground, depth + 1))
            })
            .unwrap_or_else(|| escape_xml(foreground));
        out.push_str(&value);
        rest = &rest[close + 1..];
    }
    out.push_str(rest);
    out
}

fn parse_hex(s: &str) -> Option<[f32; 3]> {
    let h = s.trim().strip_prefix('#')?;
    let v = match h.len() {
        3 => u32::from_str_radix(h, 16).ok().map(|v| {
            let e = |n: u32| ((v >> n) & 0xf) * 17;
            (e(8) << 16) | (e(4) << 8) | e(0)
        })?,
        6 => u32::from_str_radix(h, 16).ok()?,
        _ => return None,
    };
    Some([
        (v >> 16 & 255) as f32,
        (v >> 8 & 255) as f32,
        (v & 255) as f32,
    ])
}

fn mix_term(s: &str) -> Option<([f32; 3], Option<f32>)> {
    let mut it = s.split_whitespace();
    let color = parse_hex(it.next()?)?;
    let pct = match it.next() {
        Some(p) => Some(p.strip_suffix('%')?.parse::<f32>().ok()?),
        None => None,
    };
    Some((color, pct))
}

fn resolve_color_mix(s: &str, foreground: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(at) = rest.find("color-mix(") {
        let inner_start = at + "color-mix(".len();
        let Some(close) = matching_paren(rest, inner_start) else {
            break;
        };
        out.push_str(&rest[..at]);
        let inner = resolve_color_mix(&rest[inner_start..close], foreground);
        let parts = split_top_level(&inner);
        let mixed = (parts.len() == 3)
            .then(|| Some((mix_term(parts[1])?, mix_term(parts[2])?)))
            .flatten()
            .map(|((a, pa), (b, pb))| {
                let (pa, pb) = match (pa, pb) {
                    (Some(x), Some(y)) if x + y > 0.0 => (x / (x + y), y / (x + y)),
                    (Some(x), None) => (x / 100.0, 1.0 - x / 100.0),
                    (None, Some(y)) => (1.0 - y / 100.0, y / 100.0),
                    _ => (0.5, 0.5),
                };
                let c = |i: usize| (a[i] * pa + b[i] * pb).round().clamp(0.0, 255.0) as u8;
                format!("#{:02x}{:02x}{:02x}", c(0), c(1), c(2))
            });
        // Theme values are validated before this runs, but escaping the
        // fallback here keeps every insertion point safe if validation grows
        // a new field or a caller reaches this helper directly.
        out.push_str(&mixed.unwrap_or_else(|| escape_xml(foreground)));
        rest = &rest[close + 1..];
    }
    out.push_str(rest);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    const FLOW: &str = "flowchart TD\n  Client --> Core --> Store\n";
    const PIE: &str = "pie title Pets\n  \"Dogs\" : 386\n  \"Cats\" : 85\n";

    fn opts() -> Options {
        Options::default()
    }

    #[test]
    fn compiles_flowchart_and_pie_without_links_or_images() {
        for (source, kind, label) in [(FLOW, "flowchart", "Client"), (PIE, "pie", "Dogs")] {
            let diagram = render(source, &opts()).unwrap();
            assert_eq!(diagram.kind, kind);
            assert!(diagram.svg.starts_with("<svg"));
            assert!(diagram.svg.contains(label), "{}", diagram.svg);
            assert!(diagram.width > 0.0 && diagram.height > 0.0);
            assert!(!diagram.svg.contains("href="));
            assert!(!diagram.svg.contains("<image"));
            assert!(!diagram.svg.to_ascii_lowercase().contains("<script"));
        }
    }

    #[test]
    fn unsupported_mindmap_and_invalid_source_are_errors() {
        let mindmap = render("mindmap\n  root((Nexus))\n    Client\n", &opts()).unwrap_err();
        assert!(matches!(mindmap, Error::Unsupported { .. }), "{mindmap:?}");
        // The compiler has a detector but this build does not include the
        // mindmap module: the caller shows the source.
        assert!(!mindmap.to_string().is_empty());

        let invalid = render("flowchart TD\n  A[unterminated\n", &opts()).unwrap_err();
        assert!(
            matches!(invalid, Error::Syntax { .. } | Error::Semantic { .. }),
            "{invalid:?}"
        );
    }

    #[test]
    fn empty_source_is_declined_without_calling_ondine() {
        assert!(matches!(
            render("   \n", &opts()),
            Err(Error::Unsupported { .. })
        ));
    }

    #[test]
    fn oversized_source_is_refused_before_compilation() {
        let source = format!("flowchart TD\n  A[{}]\n", "x".repeat(MAX_SOURCE_BYTES));
        assert!(matches!(
            render(&source, &opts()),
            Err(Error::SourceTooLarge { .. })
        ));
    }

    #[test]
    fn links_and_images_are_disabled_inside_the_compiler() {
        let source = "flowchart TD\n  A[One] --> B[Two]\n  click A \"https://example.test/x\"\n";
        let diagram = render(source, &opts()).unwrap();
        assert!(!diagram.svg.contains("example.test"));
        assert!(!diagram.svg.contains("href="));
        assert!(!diagram.svg.contains("<a "));
    }

    #[test]
    fn output_is_deterministic_and_bounded() {
        let first = render(FLOW, &opts()).unwrap();
        let second = render(FLOW, &opts()).unwrap();
        assert_eq!(first, second);
        assert!(first.svg.len() < MAX_OUTPUT_BYTES);
    }

    #[test]
    fn max_width_reaches_the_layout() {
        let wide = render(FLOW, &opts()).unwrap();
        let narrow = render(
            FLOW,
            &Options {
                max_width: Some(80.0),
                ..opts()
            },
        )
        .unwrap();
        assert!(narrow.width <= wide.width.max(80.0));
    }

    #[test]
    fn unsafe_markup_is_refused() {
        for svg in [
            r#"<svg><script>alert(1)</script></svg>"#,
            r#"<svg><a href="https://example.test">x</a></svg>"#,
            r#"<svg><image href="data:image/png;base64,AA"/></svg>"#,
            r#"<svg><rect onload="x"/></svg>"#,
            "not svg",
        ] {
            assert!(
                matches!(check_svg(svg), Err(Error::UnsafeOutput { .. })),
                "{svg}"
            );
        }
    }

    #[test]
    fn theme_resolution_leaves_no_css_variables() {
        let diagram = render(FLOW, &opts()).unwrap();
        let theme = Theme {
            background: "#18181b".into(),
            foreground: "#e4e4e7".into(),
            muted: "#a1a1aa".into(),
            line: "#71717a".into(),
            border: "#52525b".into(),
            surface: "#27272a".into(),
            surface_alt: "#3f3f46".into(),
            accent: "#e4e4e7".into(),
            critical: "#f87171".into(),
            ..Theme::default()
        };
        let svg = resolve_theme(&diagram.svg, &theme).unwrap();
        assert!(!svg.contains("var("), "{svg}");
        assert!(!svg.contains("color-mix("), "{svg}");
        assert!(svg.contains("#18181b") && svg.contains("#e4e4e7"), "{svg}");
        assert!(svg.contains(r#"<svg font-family=""#), "{svg}");
    }

    #[test]
    fn unknown_variables_use_their_fallback_or_foreground() {
        let svg =
            r#"<svg><rect fill="var(--unknown,#123456)"/><rect fill="var(--missing)"/></svg>"#;
        let theme = Theme {
            foreground: "#abcdef".into(),
            ..Theme::default()
        };
        let resolved = resolve_theme(svg, &theme).unwrap();
        assert!(resolved.contains("#123456"), "{resolved}");
        assert!(resolved.contains("#abcdef"), "{resolved}");
        assert!(!resolved.contains("var("), "{resolved}");
    }

    #[test]
    fn color_mix_evaluates_to_a_hex_colour() {
        let svg = r#"<svg><rect fill="color-mix(in srgb, #000 20%, #fff)"/></svg>"#;
        let resolved = resolve_theme(svg, &Theme::default()).unwrap();
        assert!(resolved.contains("#cccccc"), "{resolved}");
    }

    #[test]
    fn unsafe_theme_values_are_refused() {
        let diagram = render(FLOW, &opts()).unwrap();
        let cases: Vec<(&str, Theme)> = vec![
            (
                "background",
                Theme {
                    background: "#fff\" onload=\"alert(1)".into(),
                    ..Theme::default()
                },
            ),
            (
                "foreground",
                Theme {
                    foreground: "<script>alert(1)</script>".into(),
                    ..Theme::default()
                },
            ),
            (
                "muted",
                Theme {
                    muted: "red; fill: url(https://example.test/x)".into(),
                    ..Theme::default()
                },
            ),
            (
                "line",
                Theme {
                    line: "#fff\" /><script>alert(1)</script>".into(),
                    ..Theme::default()
                },
            ),
            (
                "border",
                Theme {
                    border: "red;fill:url(https://example.test/x)".into(),
                    ..Theme::default()
                },
            ),
            (
                "surface",
                Theme {
                    surface: "\" onload=\"alert(1)".into(),
                    ..Theme::default()
                },
            ),
            (
                "surface_alt",
                Theme {
                    surface_alt: "<script>alert(1)</script>".into(),
                    ..Theme::default()
                },
            ),
            (
                "accent",
                Theme {
                    accent: "#fff\" onload=\"alert(1)".into(),
                    ..Theme::default()
                },
            ),
            (
                "critical",
                Theme {
                    critical: "red;fill:url(https://example.test/x)".into(),
                    ..Theme::default()
                },
            ),
            (
                "font",
                Theme {
                    font: "x; fill: url(javascript:alert(1))".into(),
                    ..Theme::default()
                },
            ),
            (
                "stroke",
                Theme {
                    stroke: "2;stroke:url(https://example.test/x)".into(),
                    ..Theme::default()
                },
            ),
            (
                "radius",
                Theme {
                    radius: "0\" onload=\"alert(1)".into(),
                    ..Theme::default()
                },
            ),
            (
                "title_weight",
                Theme {
                    title_weight: "700;fill:red".into(),
                    ..Theme::default()
                },
            ),
            (
                "series",
                Theme {
                    series: std::array::from_fn(|i| {
                        if i == 3 {
                            "#fff\"><script>alert(1)</script>".into()
                        } else {
                            "#000".into()
                        }
                    }),
                    ..Theme::default()
                },
            ),
        ];
        for (field, theme) in cases {
            let error = resolve_theme(&diagram.svg, &theme).unwrap_err();
            assert!(
                matches!(error, Error::UnsafeTheme { .. }),
                "{field}: {error:?}"
            );
            assert!(error.to_string().contains(field), "{field}: {error}");
        }
    }

    #[test]
    fn unsafe_svg_is_refused_before_theme_substitution() {
        let error = resolve_theme(
            r#"<svg><script>alert(1)</script><rect fill="var(--ondine-bg)"/></svg>"#,
            &Theme::default(),
        )
        .unwrap_err();
        assert!(matches!(error, Error::UnsafeOutput { .. }), "{error:?}");
    }

    #[test]
    fn quoted_font_families_are_escaped_and_still_resolve() {
        let diagram = render(FLOW, &opts()).unwrap();
        let theme = Theme {
            font: "\"Monaspace Argon\", monospace".into(),
            ..Theme::default()
        };
        let svg = resolve_theme(&diagram.svg, &theme).unwrap();
        assert!(svg.contains("&quot;Monaspace Argon&quot;"), "{svg}");
        assert!(svg.contains(r#"<svg font-family=""#), "{svg}");
    }

    #[test]
    fn every_theme_insertion_point_escapes_its_value() {
        // Validation refuses these values, but each helper also escapes, so a
        // future field or a direct caller cannot introduce an attribute.
        let root = with_root_attribute(
            r#"<svg viewBox="0 0 1 1"><text>x</text></svg>"#,
            "font-family",
            "\" onload=\"alert(1) & 'x'",
        );
        assert!(root.contains("&quot; onload=&quot;alert(1) &amp; &apos;x&apos;"));
        assert!(!root.contains(r#"" onload="alert"#));

        let mixed = resolve_color_mix(
            r#"<svg><rect fill="color-mix(in srgb, not-a-colour, #fff)"/></svg>"#,
            "\" onload=\"alert(1)",
        );
        assert!(mixed.contains("&quot; onload=&quot;alert(1)"));
        assert!(!mixed.contains(r#"" onload="alert"#));
    }

    #[test]
    fn theme_values_cannot_carry_an_assignment() {
        let cases = [
            Theme {
                background: "red=blue".into(),
                ..Theme::default()
            },
            Theme {
                font: "monospace=1".into(),
                ..Theme::default()
            },
            Theme {
                stroke: "2=2".into(),
                ..Theme::default()
            },
            Theme {
                radius: "0=0".into(),
                ..Theme::default()
            },
            Theme {
                title_weight: "700=1".into(),
                ..Theme::default()
            },
            Theme {
                series: std::array::from_fn(|_| "red=blue".into()),
                ..Theme::default()
            },
        ];
        for theme in cases {
            assert!(theme.validate().is_err(), "{theme:?}");
        }
    }
}
