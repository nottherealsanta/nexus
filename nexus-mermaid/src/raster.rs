//! A shared PNG artifact for diagrams.
//!
//! SVG is the source of truth, but terminals and single-text-engine native
//! views cannot all draw it: a terminal may only offer half blocks, and the
//! NSH native SVG backend deliberately omits text. This module draws the SVG
//! into a PNG with the same bundled font on every platform, so both clients
//! can show one artifact rather than two renderers' interpretations.
//!
//! Everything is bounded: source and SVG limits come from [`super::svg`], the
//! raster has a pixel budget, the PNG has a byte limit, images are not
//! resolved, and a panic in resvg becomes an error. A diagram that would
//! shrink below [`RasterOptions::min_scale`] is refused so the caller can show
//! its source instead of illegible noise.

use std::{
    fmt,
    panic::{catch_unwind, AssertUnwindSafe},
    sync::{Arc, OnceLock},
};

use resvg::usvg::{self, fontdb};

use super::svg::{self, Error as SvgError, Options, Theme};

/// The widest raster side, and the most pixels in all. The PNG limit matches
/// the clients' 8 MiB image limit.
pub const MAX_SIDE: u32 = 4096;
pub const MAX_PIXELS: u32 = 16_000_000;
pub const MAX_PNG_BYTES: usize = 8 * 1024 * 1024;
/// The most a small diagram is enlarged, so a tiny flowchart does not become
/// a blur of giant labels.
pub const MAX_SCALE: f32 = 3.0;

const FONT_REGULAR: &[u8] = include_bytes!("../fonts/MonaspaceArgon-Regular.otf");
const FONT_BOLD: &[u8] = include_bytes!("../fonts/MonaspaceArgon-Bold.otf");

/// The space a client has for a diagram, in CSS pixels.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct RasterOptions {
    /// The available width.
    pub width: u32,
    /// The available height. A tall diagram is scaled down to fit both.
    pub max_height: u32,
    /// The smallest scale at which labels are still legible. Below it the
    /// caller shows the source.
    pub min_scale: f32,
}

impl Default for RasterOptions {
    fn default() -> Self {
        Self {
            width: 640,
            max_height: 4096,
            min_scale: 0.5,
        }
    }
}

/// A PNG image and the CSS-pixel size it was drawn for.
#[derive(Clone, Debug, PartialEq)]
pub struct Raster {
    /// PNG bytes, RGBA with a transparent background.
    pub png: Vec<u8>,
    /// Pixel width.
    pub width: u32,
    /// Pixel height.
    pub height: u32,
    /// The width the caller should reserve, in CSS pixels, before rounding.
    pub css_width: f64,
    /// The height the caller should reserve, in CSS pixels, before rounding.
    pub css_height: f64,
}

/// Why a diagram did not become a PNG.
#[derive(Clone, Debug, PartialEq)]
pub enum RasterError {
    /// The SVG compiler declined the source.
    Compile(SvgError),
    /// The diagram would be smaller than the legibility floor.
    TooSmall { scale: f32, floor: f32 },
    /// The raster or PNG exceeded a resource limit.
    TooLarge(&'static str),
    /// The SVG could not be parsed or drawn.
    Draw(String),
    /// The bundled font is unavailable (a build mistake).
    Font,
    /// resvg or usvg panicked.
    Panicked,
}

impl fmt::Display for RasterError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Compile(error) => write!(f, "{error}"),
            Self::TooSmall { scale, floor } => {
                write!(
                    f,
                    "the diagram would shrink to {scale:.2}x; the floor is {floor:.2}x"
                )
            }
            Self::TooLarge(what) => write!(f, "the diagram {what} is too large"),
            Self::Draw(message) => write!(f, "the diagram could not be drawn: {message}"),
            Self::Font => f.write_str("the bundled diagram font is unavailable"),
            Self::Panicked => f.write_str("the diagram rasterizer panicked"),
        }
    }
}

impl std::error::Error for RasterError {}

impl From<SvgError> for RasterError {
    fn from(error: SvgError) -> Self {
        Self::Compile(error)
    }
}

fn fonts() -> Arc<fontdb::Database> {
    static FONTS: OnceLock<Arc<fontdb::Database>> = OnceLock::new();
    FONTS
        .get_or_init(|| {
            let mut database = fontdb::Database::new();
            database.load_font_data(FONT_REGULAR.to_vec());
            database.load_font_data(FONT_BOLD.to_vec());
            Arc::new(database)
        })
        .clone()
}

/// Compiles `source` and draws it into a PNG at a size that fits
/// `options.width` x `options.max_height`, never enlarged beyond
/// [`MAX_SCALE`] and never shrunk below `options.min_scale`.
pub fn render_png(
    source: &str,
    theme: &Theme,
    options: &RasterOptions,
) -> Result<Raster, RasterError> {
    let width = options.width.clamp(16, MAX_SIDE);
    let max_height = options.max_height.clamp(16, MAX_SIDE);
    let floor = options.min_scale.clamp(0.05, 1.0);

    let diagram = svg::render(
        source,
        &Options {
            max_width: Some(f64::from(width)),
            ..Options::default()
        },
    )?;
    // The raster loads only the bundled family, so use it regardless of the
    // client's UI font: both clients then draw the same labels.
    let mut raster_theme = theme.clone();
    raster_theme.font = "Monaspace Argon".into();
    let themed = svg::resolve_theme(&diagram.svg, &raster_theme)?;

    catch_unwind(AssertUnwindSafe(|| draw(&themed, width, max_height, floor)))
        .map_err(|_| RasterError::Panicked)?
}

fn draw(themed: &str, width: u32, max_height: u32, floor: f32) -> Result<Raster, RasterError> {
    let database = fonts();
    if database.faces().next().is_none() {
        return Err(RasterError::Font);
    }
    let usvg_options = usvg::Options {
        fontdb: database,
        image_href_resolver: usvg::ImageHrefResolver {
            resolve_data: Box::new(|_, _, _| None),
            resolve_string: Box::new(|_, _| None),
        },
        ..usvg::Options::default()
    };
    let tree = usvg::Tree::from_str(themed, &usvg_options)
        .map_err(|e| RasterError::Draw(e.to_string()))?;
    let size = tree.size();
    let (source_width, source_height) = (f64::from(size.width()), f64::from(size.height()));
    if !(source_width > 0.0 && source_height > 0.0) {
        return Err(RasterError::Draw("the SVG has no area".into()));
    }

    let mut scale = (f64::from(width) / source_width)
        .min(f64::from(max_height) / source_height)
        .min(f64::from(MAX_SCALE)) as f32;
    if scale < floor {
        return Err(RasterError::TooSmall { scale, floor });
    }

    let mut pixel_width = (source_width * f64::from(scale)).ceil().max(1.0) as u32;
    let mut pixel_height = (source_height * f64::from(scale)).ceil().max(1.0) as u32;
    if pixel_width > MAX_SIDE || pixel_height > MAX_SIDE {
        return Err(RasterError::TooLarge("side"));
    }
    // Shrink until the pixel budget holds, keeping the aspect.
    while u64::from(pixel_width) * u64::from(pixel_height) > u64::from(MAX_PIXELS) {
        scale *= 0.9;
        pixel_width = (source_width * f64::from(scale)).ceil().max(1.0) as u32;
        pixel_height = (source_height * f64::from(scale)).ceil().max(1.0) as u32;
    }
    if scale < floor {
        return Err(RasterError::TooSmall { scale, floor });
    }

    let Some(mut pixmap) = resvg::tiny_skia::Pixmap::new(pixel_width, pixel_height) else {
        return Err(RasterError::TooLarge("raster"));
    };
    resvg::render(
        &tree,
        resvg::tiny_skia::Transform::from_scale(scale, scale),
        &mut pixmap.as_mut(),
    );

    let mut png = Vec::new();
    {
        let mut encoder = png::Encoder::new(&mut png, pixel_width, pixel_height);
        encoder.set_color(png::ColorType::Rgba);
        encoder.set_depth(png::BitDepth::Eight);
        let mut writer = encoder
            .write_header()
            .map_err(|e| RasterError::Draw(e.to_string()))?;
        writer
            .write_image_data(pixmap.data())
            .map_err(|e| RasterError::Draw(e.to_string()))?;
    }
    if png.len() > MAX_PNG_BYTES {
        return Err(RasterError::TooLarge("PNG"));
    }

    Ok(Raster {
        png,
        width: pixel_width,
        height: pixel_height,
        css_width: source_width * f64::from(scale),
        css_height: source_height * f64::from(scale),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const FLOW: &str = "flowchart TD\n  Client --> Core --> Store\n";
    const PIE: &str = "pie title Traffic\n  \"Reads\" : 3\n  \"Writes\" : 1\n";

    fn theme() -> Theme {
        Theme {
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
            ..Theme::default()
        }
    }

    fn raster(source: &str) -> Raster {
        render_png(
            source,
            &theme(),
            &RasterOptions {
                width: 640,
                max_height: 2048,
                min_scale: 0.5,
            },
        )
        .unwrap()
    }

    #[test]
    fn draws_flowchart_and_pie_as_png() {
        for source in [FLOW, PIE] {
            let raster = raster(source);
            assert!(raster.png.starts_with(b"\x89PNG\r\n\x1a\n"));
            assert!(raster.width > 0 && raster.height > 0);
            assert!(raster.png.len() <= MAX_PNG_BYTES);
            let decoder = png::Decoder::new(std::io::Cursor::new(&raster.png));
            let mut reader = decoder.read_info().unwrap();
            let mut pixels = vec![0; reader.output_buffer_size()];
            let info = reader.next_frame(&mut pixels).unwrap();
            assert_eq!((info.width, info.height), (raster.width, raster.height));
            let channels = match info.color_type {
                png::ColorType::Rgba => 4,
                png::ColorType::Rgb => 3,
                other => panic!("unexpected {other:?}"),
            };
            assert!(
                pixels.chunks(channels).any(|p| p[channels - 1] > 0),
                "the PNG has no ink: {source}"
            );
        }
    }

    #[test]
    fn a_wide_gantt_scales_down_to_the_column() {
        let source = "gantt\n dateFormat YYYY-MM-DD\n Build :a, 2024-01-01, 2d\n";
        let raster = raster(source);
        assert!(raster.width <= 640, "{}", raster.width);
        assert!(raster.height > 0);
    }

    #[test]
    fn output_is_deterministic() {
        assert_eq!(raster(FLOW), raster(FLOW));
    }

    #[test]
    fn a_too_narrow_column_is_refused_for_the_source_fallback() {
        let error = render_png(
            FLOW,
            &theme(),
            &RasterOptions {
                width: 24,
                max_height: 2048,
                min_scale: 0.5,
            },
        )
        .unwrap_err();
        assert!(matches!(error, RasterError::TooSmall { .. }), "{error:?}");
    }

    #[test]
    fn invalid_and_unsupported_sources_keep_their_compile_error() {
        assert!(matches!(
            render_png("not a diagram", &theme(), &RasterOptions::default()),
            Err(RasterError::Compile(_))
        ));
    }

    #[test]
    fn no_external_images_are_resolved() {
        let source = "flowchart TD\n  A[One] --> B[Two]\n  click A \"https://example.test/x\"\n";
        let raster = raster(source);
        assert!(raster.png.starts_with(b"\x89PNG"));
    }

    #[test]
    fn themed_svg_keeps_its_text_nodes() {
        let diagram = svg::render(FLOW, &Options::default()).unwrap();
        let themed = svg::resolve_theme(&diagram.svg, &theme()).unwrap();
        let usvg_options = usvg::Options {
            fontdb: fonts(),
            ..usvg::Options::default()
        };
        let tree = usvg::Tree::from_str(&themed, &usvg_options).unwrap();
        fn texts(group: &usvg::Group) -> usize {
            group
                .children()
                .iter()
                .map(|node| match node {
                    usvg::Node::Text(_) => 1,
                    usvg::Node::Group(group) => texts(group),
                    _ => 0,
                })
                .sum()
        }
        assert!(texts(tree.root()) >= 3, "the labels were dropped");
    }

    #[test]
    fn theme_colours_reach_the_pixels() {
        let raster = raster(FLOW);
        let decoder = png::Decoder::new(std::io::Cursor::new(&raster.png));
        let mut reader = decoder.read_info().unwrap();
        let mut pixels = vec![0; reader.output_buffer_size()];
        let info = reader.next_frame(&mut pixels).unwrap();
        let channels = match info.color_type {
            png::ColorType::Rgba => 4,
            png::ColorType::Rgb => 3,
            other => panic!("unexpected {other:?}"),
        };
        // The foreground is #e4e4e7; antialiasing means exact matches are
        // sparse, but some pixel is close to it.
        assert!(
            pixels
                .chunks(channels)
                .any(|p| p[0] > 180 && p[1] > 180 && p[2] > 180),
            "no light ink in {info:?}"
        );
    }

    #[test]
    fn an_unsafe_theme_is_a_compile_error_not_markup() {
        let error = render_png(
            FLOW,
            &Theme {
                background: "#fff\" onload=\"alert(1)".into(),
                ..theme()
            },
            &RasterOptions::default(),
        )
        .unwrap_err();
        assert!(
            matches!(error, RasterError::Compile(SvgError::UnsafeTheme { .. })),
            "{error:?}"
        );
    }
}
