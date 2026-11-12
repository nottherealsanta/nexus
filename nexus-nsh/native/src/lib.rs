//! Native services the NSH client needs on a platform whose runtime has no
//! browser APIs: microphone capture for dictation, and the Mermaid compiler.

use nsh_core::prelude::*;

mod discovery;
pub mod live_pcm;
pub mod live_socket;
#[cfg(target_os = "ios")]
mod live_ios;

#[cfg(any(target_os = "ios", target_os = "macos"))]
mod ios;
#[cfg(any(target_os = "ios", target_os = "macos"))]
pub use ios::Microphone;

/// Microphone capture for the composer's dictation flow.
#[cfg(not(any(target_os = "ios", target_os = "macos")))]
#[module]
pub struct Microphone;

#[cfg(not(any(target_os = "ios", target_os = "macos")))]
#[module]
impl Microphone {
    /// Whether this build can capture audio (iOS only).
    pub fn available() -> bool {
        false
    }

    /// Starts recording after asking for microphone permission.
    pub async fn start() -> Result<(), String> {
        Err("Microphone capture is not available in this build".into())
    }

    /// Stops recording and returns a 16 kHz mono PCM16 WAV.
    pub async fn stop() -> Result<Vec<u8>, String> {
        Err("Microphone capture is not available in this build".into())
    }

    /// Stops recording and discards the file.
    pub async fn cancel() -> Result<(), String> {
        Ok(())
    }

    /// The current input level, 0 to 1.
    pub fn level() -> f64 {
        0.0
    }
}

/// A compiled Mermaid diagram, ready for a client to draw or rasterize.
#[derive(Debug, Clone, PartialEq, JsObject)]
pub struct MermaidDiagram {
    /// The SVG document. It uses Ondine's CSS variables; a browser that shows
    /// it directly must set them. The timeline uses `renderPng` instead.
    pub svg: String,
    /// Ondine's diagram type (`flowchart`, `pie`, `sequence`, ...).
    pub kind: String,
    /// Intrinsic width in CSS pixels.
    pub width: f64,
    /// Intrinsic height in CSS pixels.
    pub height: f64,
}

/// Mermaid compilation for the timeline and the Manager's diagram card.
///
/// Bounded by `nexus-mermaid`: links and images are off, source and output
/// have limits, and a compiler panic is returned as an error.
#[module]
pub struct Mermaid;

#[module]
impl Mermaid {
    /// Compiles `source` to SVG for a `width`-pixel column. An unsupported or
    /// invalid diagram returns a message the client turns into the source
    /// fallback.
    pub fn render(source: String, width: u32) -> Result<MermaidDiagram, String> {
        let options = nexus_mermaid::svg::Options {
            max_width: (width > 0).then_some(f64::from(width)),
            ..nexus_mermaid::svg::Options::default()
        };
        let diagram =
            nexus_mermaid::svg::render(&source, &options).map_err(|error| error.to_string())?;
        Ok(MermaidDiagram {
            svg: diagram.svg,
            kind: diagram.kind.to_owned(),
            width: diagram.width,
            height: diagram.height,
        })
    }

    /// Renders the chart types that are legible as rows (pie, Gantt and
    /// timeline) instead of drawing them. `None` means the caller should use
    /// [`Self::render_png`] or show the source.
    pub fn render_text(source: String, width: u32) -> Option<Vec<MermaidTextRow>> {
        let rows = nexus_mermaid::text::render(&source, width as usize)?;
        Some(
            rows.into_iter()
                .map(|row| MermaidTextRow {
                    runs: row
                        .into_iter()
                        .map(|(text, tone)| MermaidTextRun {
                            text,
                            tone: match tone {
                                nexus_mermaid::text::Tone::Title => "title".into(),
                                nexus_mermaid::text::Tone::Heading => "heading".into(),
                                nexus_mermaid::text::Tone::Label => "label".into(),
                                nexus_mermaid::text::Tone::Bar => "bar".into(),
                                nexus_mermaid::text::Tone::Quiet => "quiet".into(),
                            },
                        })
                        .collect(),
                })
                .collect(),
        )
    }

    /// Draws `source` as a PNG for a `width` x `max_height` CSS-pixel box.
    ///
    /// Both targets use this artifact. The SVG path is exported for callers
    /// that can draw it, but the shared PNG is what the timeline shows.
    pub fn render_png(
        source: String,
        width: u32,
        max_height: u32,
        theme: MermaidTheme,
    ) -> Result<MermaidPng, String> {
        let raster = nexus_mermaid::raster::render_png(
            &source,
            &theme.into(),
            &nexus_mermaid::raster::RasterOptions {
                width,
                max_height,
                ..nexus_mermaid::raster::RasterOptions::default()
            },
        )
        .map_err(|error| error.to_string())?;
        Ok(MermaidPng {
            png: raster.png,
            width: raster.width,
            height: raster.height,
            css_width: raster.css_width,
            css_height: raster.css_height,
        })
    }
}

/// One row of the shared text renderer.
#[derive(Debug, Clone, PartialEq, JsObject)]
pub struct MermaidTextRow {
    pub runs: Vec<MermaidTextRun>,
}

/// A run of text and the tone the client maps to its own palette.
#[derive(Debug, Clone, PartialEq, JsObject)]
pub struct MermaidTextRun {
    pub text: String,
    pub tone: String,
}

/// The colours a diagram should use. Clients fill this from their own theme
/// tokens, so the same compiled diagram reads in either client's palette.
#[derive(Debug, Clone, PartialEq, JsObject)]
pub struct MermaidTheme {
    /// The canvas behind the diagram.
    pub background: String,
    /// Ordinary text and thin lines.
    pub foreground: String,
    /// Secondary text.
    pub muted: String,
    /// Edges.
    pub line: String,
    /// Shape borders.
    pub border: String,
    /// Shape fills.
    pub surface: String,
    /// The alternate surface (grids and finished work).
    pub surface_alt: String,
    /// Accent marks.
    pub accent: String,
    /// Critical marks.
    pub critical: String,
    /// The font family. The raster always uses the bundled Monaspace Argon.
    pub font: String,
    /// The stroke width.
    pub stroke: String,
    /// The corner radius.
    pub radius: String,
    /// The title weight.
    pub title_weight: String,
    /// Series colours, up to eight; missing entries are black.
    pub series: Vec<String>,
}

/// A rasterized diagram: PNG bytes and the CSS-pixel size to reserve.
#[derive(Debug, Clone, PartialEq, JsObject)]
pub struct MermaidPng {
    /// PNG bytes, RGBA with a transparent background.
    pub png: Vec<u8>,
    /// Pixel width.
    pub width: u32,
    /// Pixel height.
    pub height: u32,
    /// The width to reserve, in CSS pixels.
    pub css_width: f64,
    /// The height to reserve, in CSS pixels.
    pub css_height: f64,
}

impl From<MermaidTheme> for nexus_mermaid::svg::Theme {
    fn from(theme: MermaidTheme) -> Self {
        let series = theme.series;
        Self {
            background: theme.background,
            foreground: theme.foreground,
            muted: theme.muted,
            line: theme.line,
            border: theme.border,
            surface: theme.surface,
            surface_alt: theme.surface_alt,
            accent: theme.accent,
            critical: theme.critical,
            font: theme.font,
            stroke: theme.stroke,
            radius: theme.radius,
            title_weight: theme.title_weight,
            series: std::array::from_fn(|i| {
                series.get(i).cloned().unwrap_or_else(|| "#000".into())
            }),
        }
    }
}

/// One point of a pen stroke, each axis 0 to 1 across the image.
#[derive(Debug, Clone, PartialEq, JsObject)]
pub struct AnnotatePoint {
    pub x: f64,
    pub y: f64,
}

/// One pen stroke: `#rrggbb` and its points.
#[derive(Debug, Clone, PartialEq, JsObject)]
pub struct AnnotateStroke {
    pub color: String,
    pub points: Vec<AnnotatePoint>,
}

impl AnnotateStroke {
    fn shared(self) -> Result<nexus_annotate::Stroke, String> {
        let hex = self
            .color
            .strip_prefix('#')
            .filter(|hex| hex.len() == 6)
            .ok_or_else(|| format!("Not a colour: {}", self.color))?;
        let channel = |i: usize| {
            u8::from_str_radix(&hex[i..i + 2], 16)
                .map_err(|_| format!("Not a colour: {}", self.color))
        };
        Ok(nexus_annotate::Stroke {
            color: [channel(0)?, channel(2)?, channel(4)?],
            points: self
                .points
                .iter()
                .map(|point| [point.x as f32, point.y as f32])
                .collect(),
        })
    }
}

/// Merges marks into a composer image, with the TUI's code so both clients send
/// the same pixels. NSH's canvas cannot read its own pixels back.
#[module]
pub struct Annotate;

#[module]
impl Annotate {
    /// The image as a data URL with `strokes` drawn into it.
    pub fn flatten(data_url: String, strokes: Vec<AnnotateStroke>) -> Result<String, String> {
        let strokes = strokes
            .into_iter()
            .map(AnnotateStroke::shared)
            .collect::<Result<Vec<_>, _>>()?;
        nexus_annotate::flatten(&data_url, &strokes).map_err(|error| error.to_string())
    }
}

/// The crate's UI set: `Microphone`, `Mermaid` and `Annotate` from `nexus/native`.
#[native_module("nexus/native")]
fn native(registry: &mut Registry) {
    registry.module::<Microphone>();
    #[cfg(target_os = "ios")]
    registry.module::<live_ios::LiveAudio>();
    registry.module::<discovery::Discovery>();
    registry.module::<Mermaid>();
    registry.module::<Annotate>();
}

#[cfg(test)]
mod tests {
    use super::*;

    const FLOW: &str = "flowchart TD\n  Client --> Core --> Store\n";

    #[test]
    fn the_native_module_compiles_mermaid() {
        let diagram = Mermaid::render(FLOW.into(), 640).unwrap();
        assert_eq!(diagram.kind, "flowchart");
        assert!(diagram.svg.starts_with("<svg"));
        assert!(diagram.svg.contains("Client"));
        assert!(!diagram.svg.contains("href="));
    }

    #[test]
    fn the_native_module_rasterizes_mermaid() {
        let png = Mermaid::render_png(
            FLOW.into(),
            640,
            2048,
            MermaidTheme {
                background: "#18181b".into(),
                foreground: "#e4e4e7".into(),
                muted: "#a1a1aa".into(),
                line: "#71717a".into(),
                border: "#52525b".into(),
                surface: "#27272a".into(),
                surface_alt: "#3f3f46".into(),
                accent: "#e4e4e7".into(),
                critical: "#f87171".into(),
                font: "Monaspace Argon".into(),
                stroke: "2".into(),
                radius: "0".into(),
                title_weight: "700".into(),
                series: vec!["#e4e4e7".into()],
            },
        )
        .unwrap();
        assert!(png.png.starts_with(b"\x89PNG\r\n\x1a\n"));
        assert!(png.width > 0 && png.height > 0);
        assert!(png.css_width > 0.0 && png.css_height > 0.0);
    }

    #[test]
    fn the_native_module_renders_legible_text_rows() {
        let rows = Mermaid::render_text(
            "pie title Pets\n  \"Dogs\" : 3\n  \"Cats\" : 1\n".into(),
            60,
        )
        .unwrap();
        assert!(!rows.is_empty());
        assert!(rows.iter().any(|row| {
            row.runs
                .iter()
                .any(|run| run.text.contains("Dogs") && run.tone == "label")
        }));
        assert!(Mermaid::render_text(FLOW.into(), 60).is_none());
    }

    #[test]
    fn an_unsafe_theme_is_refused_before_rasterizing() {
        let error = Mermaid::render_png(
            FLOW.into(),
            640,
            2048,
            MermaidTheme {
                background: "#fff\" onload=\"alert(1)".into(),
                ..MermaidTheme {
                    background: String::new(),
                    foreground: String::new(),
                    muted: String::new(),
                    line: String::new(),
                    border: String::new(),
                    surface: String::new(),
                    surface_alt: String::new(),
                    accent: String::new(),
                    critical: String::new(),
                    font: String::new(),
                    stroke: String::new(),
                    radius: String::new(),
                    title_weight: String::new(),
                    series: Vec::new(),
                }
            },
        )
        .unwrap_err();
        assert!(error.contains("background"), "{error}");
    }
}
