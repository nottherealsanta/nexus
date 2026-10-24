//! Bounded native Mermaid fallback. No browser, subprocess or external resources.
//!
//! The SVG is rasterised straight to the terminal's half-block grid: exactly
//! `columns` x `2 * rows` pixels (one `▀`/`▄` cell = two vertically stacked
//! pixels), supersampled and box-filtered. Transparency is preserved, so the
//! diagram composites over whatever the transcript draws instead of sitting on
//! an opaque white card, and cells are painted directly (no per-row resizing).
use image::RgbaImage;
use ratatui::{buffer::Buffer, layout::Rect, style::Color};
use std::{
    collections::HashMap,
    sync::{
        atomic::{AtomicU64, Ordering},
        mpsc, Arc, Mutex, OnceLock,
    },
};

const MAX_SOURCE: usize = 16_384;
/// Widest / tallest diagram, in terminal cells.
const MAX_COLUMNS: u32 = 200;
const MAX_ROWS: u32 = 48;
/// Smallest SVG-to-pixel scale at which labels stay legible in half blocks.
const MIN_SCALE: f32 = 0.5;
/// Supersampling factor per axis before the box filter down to cell pixels.
const SUPERSAMPLE: u32 = 4;
/// Canvas assumed under translucent pixels when the cell has no RGB background.
const CANVAS: (u8, u8, u8) = (24, 24, 27);
const CACHE_LIMIT: usize = 8;
const QUEUE_LIMIT: usize = 4;
type Key = (String, usize);
type Raster = Option<Arc<RgbaImage>>;
/// The raster plus the SVG-to-pixel scale it was drawn at.
type Scaled = Option<(Arc<RgbaImage>, f32)>;

#[derive(Default)]
struct State {
    ready: HashMap<Key, Scaled>,
    pending: std::collections::HashSet<Key>,
}
struct Worker {
    state: Arc<Mutex<State>>,
    sender: mpsc::SyncSender<Key>,
}
static GENERATION: AtomicU64 = AtomicU64::new(0);

/// Polled by the UI even when idle; completion must invalidate text layouts too.
pub fn generation() -> u64 {
    GENERATION.load(Ordering::Acquire)
}

impl Worker {
    fn new(compile: fn(&str, usize) -> Option<(RgbaImage, f32)>) -> Self {
        let state = Arc::new(Mutex::new(State::default()));
        let (sender, receiver) = mpsc::sync_channel::<Key>(QUEUE_LIMIT);
        let shared = state.clone();
        // Exactly one worker for the lifetime of the process. A stuck native
        // compiler/font scan can occupy it, but never blocks input or spawns replacements.
        std::thread::Builder::new()
            .name("mermaid".into())
            .spawn(move || {
                for key in receiver {
                    let image = crate::panic_log::catch_silent(|| compile(&key.0, key.1))
                        .ok()
                        .flatten()
                        .map(|(image, scale)| (Arc::new(image), scale));
                    let mut state = shared.lock().unwrap();
                    state.pending.remove(&key);
                    if state.ready.len() >= CACHE_LIMIT {
                        if let Some(old) = state.ready.keys().next().cloned() {
                            state.ready.remove(&old);
                        }
                    }
                    state.ready.insert(key, image);
                    GENERATION.fetch_add(1, Ordering::Release);
                }
            })
            .expect("start Mermaid worker");
        Self { state, sender }
    }

    fn request(&self, key: Key) -> Scaled {
        let mut state = self.state.lock().unwrap();
        if let Some(image) = state.ready.get(&key) {
            return image.clone();
        }
        if !state.pending.contains(&key) && self.sender.try_send(key.clone()).is_ok() {
            state.pending.insert(key);
        }
        None
    }
}

pub fn raster(source: &str, columns: usize) -> Raster {
    scaled(source, columns).map(|(image, _)| image)
}

/// Callers try the Unicode and text renderers first; every diagram type they
/// decline may attempt the raster. Ondine errors and the legibility gate keep
/// the literal source.
fn scaled(source: &str, columns: usize) -> Scaled {
    let source = source.trim();
    if source.len() > MAX_SOURCE || columns < 4 || source.is_empty() {
        return None;
    }
    static WORKER: OnceLock<Worker> = OnceLock::new();
    WORKER
        .get_or_init(|| Worker::new(compile))
        .request((source.to_owned(), columns))
}

fn compile(source: &str, columns: usize) -> Option<(RgbaImage, f32)> {
    // Panics in a third-party parser must not tear down the terminal session.
    crate::panic_log::catch_silent(|| {
        let svg = resolve_svg_theme(&ondine::render(source, "", 0.0).ok()?.svg);
        let mut options = resvg::usvg::Options::default();
        static FONTS: OnceLock<Arc<resvg::usvg::fontdb::Database>> = OnceLock::new();
        options.fontdb = FONTS
            .get_or_init(|| {
                let mut fonts = resvg::usvg::fontdb::Database::new();
                fonts.load_system_fonts();
                Arc::new(fonts)
            })
            .clone();
        if options.fontdb.is_empty() {
            return None;
        }
        options.image_href_resolver = resvg::usvg::ImageHrefResolver {
            resolve_data: Box::new(|_, _, _| None),
            resolve_string: Box::new(|_, _| None),
        };
        let tree = resvg::usvg::Tree::from_str(&svg, &options).ok()?;
        let size = tree.size();
        let (sw, sh) = (size.width(), size.height());
        if !(sw > 0.0 && sh > 0.0) {
            return None;
        }
        // One half-block pixel is about square (a cell is ~1:2), so the pixel
        // grid keeps the SVG aspect. Fill the width (upscaling small charts so
        // text is as large as the cells allow), bounded by MAX_ROWS.
        let max_w = (columns as u32).clamp(1, MAX_COLUMNS) as f32;
        let max_h = (MAX_ROWS * 2) as f32;
        let fit = (max_w / sw).min(max_h / sh);
        let w = ((sw * fit).round() as u32).clamp(1, MAX_COLUMNS);
        // Whole terminal rows: an even pixel height.
        let h = (((sh * fit).round() as u32).max(2) + 1) & !1;
        let k = SUPERSAMPLE;
        let mut pixmap = resvg::tiny_skia::Pixmap::new(w * k, h * k)?;
        resvg::render(
            &tree,
            resvg::tiny_skia::Transform::from_scale((w * k) as f32 / sw, (h * k) as f32 / sh),
            &mut pixmap.as_mut(),
        );
        Some((downsample(pixmap.data(), w, h, k), fit))
    })
    .ok()
    .flatten()
}

/// Dark theme baked into Ondine's SVG. Ondine styles via CSS custom properties
/// (`--ondine-*` public knobs, `--_*` internal aliases) that browsers resolve
/// but usvg does not, so unresolved they render as black on white. Names here
/// are the only variables ever substituted; anything else uses its own fallback.
const BG: &str = "#18181b";
const THEME: &[(&str, &str)] = &[
    ("--ondine-bg", BG),
    ("--ondine-fg", "#e4e4e7"),
    ("--ondine-line", "#71717a"),
    ("--ondine-border", "#52525b"),
    ("--ondine-muted", "#a1a1aa"),
    ("--ondine-surface", "#27272a"),
    ("--ondine-surface-alt", "#3f3f46"),
    ("--ondine-accent", "#e4e4e7"),
    ("--ondine-critical", "#f87171"),
    ("--ondine-font", "sans-serif"),
    ("--ondine-series-1", "#e4e4e7"),
    ("--ondine-series-2", "#a1a1aa"),
    ("--ondine-series-3", "#71717a"),
    ("--ondine-series-4", "#d4d4d8"),
    ("--ondine-series-5", "#52525b"),
    ("--ondine-series-6", "#bcbcc4"),
    ("--ondine-series-7", "#8a8a93"),
    ("--ondine-series-8", "#63636b"),
    ("--_bg", BG),
    ("--_fg", "#e4e4e7"),
    ("--_m", "#a1a1aa"),
    ("--_l", "#71717a"),
    ("--_b", "#52525b"),
    ("--_s", "#27272a"),
    ("--_a", "#3f3f46"),
    ("--_c", "#e4e4e7"),
    ("--_sw", "2"),
    ("--_r", "0"),
    ("--_tw", "700"),
    ("--_fm", "sans-serif"),
    ("--ondine-stroke", "2"),
    ("--ondine-radius", "0"),
    ("--ondine-title-weight", "700"),
];

/// Replaces every `var(...)` and `color-mix(...)` in `svg` with literals.
fn resolve_svg_theme(svg: &str) -> String {
    resolve_color_mix(&resolve_vars(svg, 0))
}

/// End of the `(` group opened just before `s[from..]`; returns the index of its `)`.
fn matching_paren(s: &str, from: usize) -> Option<usize> {
    let mut depth = 1;
    for (i, ch) in s[from..].char_indices() {
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

/// Splits on commas that are not inside nested parentheses.
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

fn resolve_vars(s: &str, depth: u32) -> String {
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
        let value = THEME
            .iter()
            .find(|(n, _)| *n == name)
            .map(|(_, v)| (*v).to_owned())
            .or_else(|| {
                let fallback = rest[inner_start..close].split_once(',')?.1;
                (depth < 8).then(|| resolve_vars(fallback.trim(), depth + 1))
            })
            .unwrap_or_else(|| THEME[1].1.to_owned());
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

/// `color` or `color NN%` operand of `color-mix`.
fn mix_term(s: &str) -> Option<([f32; 3], Option<f32>)> {
    let mut it = s.split_whitespace();
    let color = parse_hex(it.next()?)?;
    let pct = match it.next() {
        Some(p) => Some(p.strip_suffix('%')?.parse::<f32>().ok()?),
        None => None,
    };
    Some((color, pct))
}

/// Evaluates `color-mix(in srgb, A [p%], B [p%])` over literal hex colours.
fn resolve_color_mix(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(at) = rest.find("color-mix(") {
        let inner_start = at + "color-mix(".len();
        let Some(close) = matching_paren(rest, inner_start) else {
            break;
        };
        out.push_str(&rest[..at]);
        let inner = resolve_color_mix(&rest[inner_start..close]);
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
        // Unparseable mixes degrade to the foreground rather than reaching usvg.
        out.push_str(&mixed.unwrap_or_else(|| THEME[1].1.to_owned()));
        rest = &rest[close + 1..];
    }
    out.push_str(rest);
    out
}

/// Box-filters premultiplied `k`x supersampled RGBA down to `w`x`h` and returns
/// straight (unpremultiplied) RGBA. Pixels equal to the theme background become
/// transparent so the chart composites over the transcript.
fn downsample(data: &[u8], w: u32, h: u32, k: u32) -> RgbaImage {
    let stride = (w * k * 4) as usize;
    let area = k * k;
    let mut out = RgbaImage::new(w, h);
    for y in 0..h {
        for x in 0..w {
            let mut sum = [0u32; 4];
            for sy in 0..k {
                let row = ((y * k + sy) as usize) * stride + (x * k * 4) as usize;
                for sx in 0..k as usize {
                    for (c, total) in sum.iter_mut().enumerate() {
                        *total += u32::from(data[row + sx * 4 + c]);
                    }
                }
            }
            let a = sum[3];
            let mut px = [0u8; 4];
            if a > 0 {
                for c in 0..3 {
                    px[c] = (sum[c] * 255 / a).min(255) as u8;
                }
                px[3] = (a / area) as u8;
            }
            out.put_pixel(x, y, image::Rgba(keep_ink(px)));
        }
    }
    out
}

/// Colours are already themed for the dark canvas by `resolve_svg_theme`; only
/// drop empty and solid-background pixels (no tone remapping).
fn keep_ink([r, g, b, a]: [u8; 4]) -> [u8; 4] {
    let near = |v: u8, t: u8| v.abs_diff(t) <= 2;
    if a < 12 || (a >= 250 && near(r, 0x18) && near(g, 0x18) && near(b, 0x1b)) {
        return [0, 0, 0, 0];
    }
    [r, g, b, a]
}

/// Height in terminal rows (two pixels per row). `None` while compiling, when
/// the source cannot be drawn, or when a cell raster would be illegible: a
/// half-block grid has ~1 pixel per column, so a wide SVG (a Gantt is ~800px)
/// shrinks its 12px labels below `MIN_SCALE` and the picture is noise. The
/// caller then shows the source instead of a garbled image.
pub fn rows(source: &str, width: usize) -> Option<usize> {
    let (image, scale) = scaled(source, width)?;
    legible(scale).then_some((image.height() / 2) as usize)
}

/// Whether a raster drawn at `scale` keeps its labels readable. Layout tests
/// exercise the strip plumbing on tiny synthetic diagrams, so they skip the gate;
/// `legibility_gate_rejects_shrunken_svgs` pins the real threshold.
fn legible(scale: f32) -> bool {
    legible_at(scale, if cfg!(test) { 0.0 } else { MIN_SCALE })
}

fn legible_at(scale: f32, min: f32) -> bool {
    scale >= min
}

fn blend(px: &image::Rgba<u8>, under: (u8, u8, u8)) -> Color {
    let a = u32::from(px[3]);
    let mix =
        |over: u8, below: u8| ((u32::from(over) * a + u32::from(below) * (255 - a)) / 255) as u8;
    Color::Rgb(
        mix(px[0], under.0),
        mix(px[1], under.1),
        mix(px[2], under.2),
    )
}

/// Paints terminal row `row` of the diagram: pixel rows `2*row` (fg of `▀`) and
/// `2*row+1` (bg). Fully transparent halves leave the cell's own background
/// alone; translucent pixels blend over the cell's RGB bg (or a dark canvas).
pub fn render(source: &str, width: usize, row: usize, area: Rect, buf: &mut Buffer) {
    let Some(image) = raster(source, width) else {
        return;
    };
    let y = (row as u32).saturating_mul(2);
    if y + 1 >= image.height() {
        return;
    }
    let area = area.intersection(buf.area);
    if area.is_empty() {
        return;
    }
    for x in 0..u32::from(area.width).min(image.width()) {
        let (top, bottom) = (image.get_pixel(x, y), image.get_pixel(x, y + 1));
        let cell = &mut buf[(area.x + x as u16, area.y)];
        let under = match cell.bg {
            Color::Rgb(r, g, b) => (r, g, b),
            _ => CANVAS,
        };
        match (top[3] >= 12, bottom[3] >= 12) {
            (true, true) => {
                cell.set_symbol("▀")
                    .set_fg(blend(top, under))
                    .set_bg(blend(bottom, under));
            }
            (true, false) => {
                cell.set_symbol("▀").set_fg(blend(top, under));
            }
            (false, true) => {
                cell.set_symbol("▄").set_fg(blend(bottom, under));
            }
            (false, false) => {}
        }
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    pub(crate) fn wait_raster(source: &str, width: usize) -> Raster {
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(30);
        loop {
            if let Some(image) = raster(source, width) {
                return Some(image);
            }
            assert!(
                std::time::Instant::now() < deadline,
                "Mermaid did not finish: {source}"
            );
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
    }
    #[test]
    fn fallback_families_and_clipped_strips() {
        for source in [
            "timeline\n  2024 : Shipped",
            "gantt\n dateFormat YYYY-MM-DD\n section Work\n Build :a, 2024-01-01, 2d",
            "mindmap\n  root((Root))\n    Child",
            "sankey-beta\nA,B,10",
        ] {
            assert!(wait_raster(source, 80).is_some(), "{source}");
        }
        let source = "pie\n\"a\" : 1";
        let area = Rect::new(2, 3, 20, 1);
        let mut buffer = Buffer::empty(Rect::new(0, 0, 30, 10));
        wait_raster(source, 20);
        render(source, 20, 1, area, &mut buffer);
        for y in 0..10 {
            for x in 0..30 {
                if !area.contains(ratatui::layout::Position::new(x, y)) {
                    assert_eq!(buffer[(x, y)].symbol(), " ");
                }
            }
        }
        for width in 10..25 {
            let _ = raster(source, width);
        }
    }

    const PIE: &str = "pie title Pets\n\"Dogs\" : 386\n\"Cats\" : 85\n\"Rats\" : 15";

    /// Renders every diagram row into a `cols`-wide buffer on a dark canvas.
    fn paint(source: &str, cols: u16) -> Buffer {
        let rows = rows(source, cols as usize).unwrap_or_else(|| {
            wait_raster(source, cols as usize);
            rows(source, cols as usize).unwrap()
        });
        let area = Rect::new(0, 0, cols, rows as u16);
        let mut buffer = Buffer::empty(area);
        for cell in buffer.content.iter_mut() {
            cell.set_bg(Color::Rgb(CANVAS.0, CANVAS.1, CANVAS.2));
        }
        for row in 0..rows {
            render(
                source,
                cols as usize,
                row,
                Rect::new(0, row as u16, cols, 1),
                &mut buffer,
            );
        }
        buffer
    }

    #[test]
    fn raster_matches_the_half_block_grid() {
        let image = wait_raster(PIE, 40).unwrap();
        assert!(
            image.width() <= 40 && image.width() >= 20,
            "{}",
            image.width()
        );
        assert_eq!(image.height() % 2, 0);
        assert!(image.height() <= MAX_ROWS * 2);
        assert_eq!(rows(PIE, 40), Some((image.height() / 2) as usize));
        // Transparent, not a flattened opaque card.
        assert!(image.pixels().any(|p| p[3] == 0));
        assert!(image.pixels().any(|p| p[3] == 255));
    }

    #[test]
    fn pie_is_legible_monochrome_on_the_dark_canvas() {
        // Ondine compiles to monochrome SVG by design (see its README and
        // compiler/src/lib.rs), so there are no slice colours to preserve:
        // assert legibility (contrast, transparency, labels), not hue.
        wait_raster(PIE, 60);
        let buffer = paint(PIE, 60);
        let luma = |c: Color| match c {
            Color::Rgb(r, g, b) => {
                (299 * u32::from(r) + 587 * u32::from(g) + 114 * u32::from(b)) / 1000
            }
            _ => 0,
        };
        let canvas = luma(Color::Rgb(CANVAS.0, CANVAS.1, CANVAS.2));
        let mut tones = std::collections::HashSet::new();
        let (mut painted, mut white, mut bright) = (0, 0, 0);
        let mut populated_rows = 0;
        for y in 0..buffer.area.height {
            let mut any = false;
            for x in 0..buffer.area.width {
                let c = &buffer[(x, y)];
                if c.symbol() == "▀" || c.symbol() == "▄" {
                    painted += 1;
                    any = true;
                    // Ink must stand off the dark canvas; label text is light.
                    if luma(c.fg) >= canvas + 120 {
                        bright += 1;
                    }
                    if let Color::Rgb(r, g, b) = c.fg {
                        tones.insert((r / 32, g / 32, b / 32));
                    }
                }
                if c.bg == Color::Rgb(255, 255, 255) || c.fg == Color::Rgb(255, 255, 255) {
                    white += 1;
                }
            }
            populated_rows += usize::from(any);
        }
        assert!(painted > 50, "painted {painted}");
        assert!(
            bright > 10,
            "labels/strokes lack contrast: {bright} bright cells"
        );
        assert!(tones.len() >= 2, "flat single-tone ink {tones:?}");
        assert_eq!(white, 0, "white backdrop leaked");
        assert!(
            populated_rows * 2 >= buffer.area.height as usize,
            "blank bands: {populated_rows}/{} rows have ink",
            buffer.area.height
        );
    }

    #[test]
    fn transparent_halves_leave_the_cell_background_alone() {
        wait_raster(PIE, 40);
        let buffer = paint(PIE, 40);
        let canvas = Color::Rgb(CANVAS.0, CANVAS.1, CANVAS.2);
        let untouched = buffer.content.iter().filter(|c| c.symbol() == " ").count();
        assert!(untouched > 0, "the chart corners should stay empty");
        assert!(buffer
            .content
            .iter()
            .filter(|c| c.symbol() == " ")
            .all(|c| c.bg == canvas));
    }

    #[test]
    fn render_clips_to_the_area_and_buffer() {
        wait_raster(PIE, 30);
        let mut buffer = Buffer::empty(Rect::new(0, 0, 20, 4));
        // Wider than the buffer, starting off-origin, and past the last row.
        render(PIE, 30, 3, Rect::new(5, 2, 40, 1), &mut buffer);
        render(PIE, 30, 3, Rect::new(0, 9, 40, 1), &mut buffer);
        render(PIE, 30, 10_000, Rect::new(0, 0, 20, 1), &mut buffer);
        for y in 0..4 {
            for x in 0..5 {
                assert_eq!(buffer[(x, y)].symbol(), " ", "({x},{y})");
            }
        }
        for x in 0..20 {
            assert_eq!(buffer[(x, 0)].symbol(), " ");
        }
    }

    #[test]
    fn gantt_has_tonal_range_without_a_white_panel() {
        let source = "gantt\n dateFormat YYYY-MM-DD\n title Plan\n section Build\n Parse :a, 2024-01-01, 5d\n Render :b, after a, 6d\n section Ship\n Test :c, after b, 4d\n Release :d, after c, 2d";
        wait_raster(source, 100);
        let buffer = paint(source, 100);
        let mut tones = std::collections::HashSet::new();
        for c in &buffer.content {
            if let Color::Rgb(r, g, b) = c.fg {
                tones.insert((r / 32, g / 32, b / 32));
            }
        }
        // Monochrome source: tonal range (ink vs. mid-grey bars), not hue.
        assert!(tones.len() >= 2, "gantt tones {tones:?}");
        assert!(buffer
            .content
            .iter()
            .all(|c| c.bg != Color::Rgb(255, 255, 255)));
    }

    #[test]
    fn keep_ink_only_drops_empty_and_background() {
        assert_eq!(keep_ink([0x18, 0x18, 0x1b, 255])[3], 0);
        assert_eq!(keep_ink([10, 10, 10, 3])[3], 0);
        assert_eq!(keep_ink([0, 0, 0, 255]), [0, 0, 0, 255]);
        assert_eq!(keep_ink([228, 228, 231, 255]), [228, 228, 231, 255]);
    }

    #[test]
    fn theme_resolver_expands_vars_and_color_mix() {
        let svg = r#"<svg style="--_bg:var(--ondine-bg,#fff);--_a:var(--ondine-surface-alt,color-mix(in srgb,var(--_fg) 20%,var(--_bg)));color:var(--_fg)"><rect fill="var(--_a)" stroke="var(--unknown,#123456)" stroke-width="var(--_sw)"/></svg>"#;
        let out = resolve_svg_theme(svg);
        assert!(!out.contains("var("), "{out}");
        assert!(!out.contains("color-mix"), "{out}");
        assert!(
            out.contains("color:#e4e4e7") && out.contains("stroke=\"#123456\""),
            "{out}"
        );
        assert!(out.contains("stroke-width=\"2\""), "{out}");
        assert_eq!(
            resolve_svg_theme("color-mix(in srgb,#ffffff 25%,#000000)"),
            "#404040"
        );
        assert_eq!(resolve_svg_theme("color-mix(in srgb,#fff,#000)"), "#808080");
    }

    #[test]
    fn real_ondine_output_has_no_unresolved_vars() {
        for source in [
            PIE,
            "gantt\n dateFormat YYYY-MM-DD\n section A\n X :a, 2024-01-01, 2d",
        ] {
            let svg = ondine::render(source, "", 0.0).unwrap().svg;
            let out = resolve_svg_theme(&svg);
            assert!(
                !out.contains("var(") && !out.contains("color-mix("),
                "{out}"
            );
            // Non-colour aliases must resolve to numbers, never the fg hex.
            assert!(
                !out.contains("font-weight:#") && !out.contains("rx:#"),
                "{out}"
            );
            assert!(!out.contains("--_tw:#") && !out.contains("--_r:#"), "{out}");
        }
        let gantt = ondine::render(
            "gantt\n dateFormat YYYY-MM-DD\n section A\n X :a, 2024-01-01, 2d",
            "",
            0.0,
        )
        .unwrap()
        .svg;
        let out = resolve_svg_theme(&gantt);
        assert!(out.contains("rx:0") || out.contains("rx=\"0\""), "{out}");
        assert!(out.contains("stroke-width:2"), "{out}");
    }

    #[test]
    fn gantt_raster_has_structure_not_stripes() {
        let source = "gantt\n dateFormat YYYY-MM-DD\n title Plan\n section Build\n Parse :a, 2024-01-01, 5d\n Render :b, after a, 6d\n section Ship\n Test :c, after b, 4d";
        let image = wait_raster(source, 100).unwrap();
        let mut inked_rows = 0;
        let mut tones = std::collections::HashSet::new();
        for y in 0..image.height() {
            let row: Vec<_> = (0..image.width())
                .map(|x| image.get_pixel(x, y).0)
                .collect();
            let ink = row.iter().filter(|p| p[3] >= 12).count();
            if ink > 0 {
                inked_rows += 1;
            }
            // No row is a solid full-width white or opaque stripe.
            assert!(
                ink < row.len() || row.iter().any(|p| p[..3] != row[0][..3]),
                "stripe at {y}"
            );
            assert!(
                row.iter().all(|p| p[3] < 12 || p[..3] != [255, 255, 255]),
                "white at {y}"
            );
            tones.extend(row.iter().filter(|p| p[3] >= 12).map(|p| p[0] / 32));
        }
        assert!(
            inked_rows * 4 >= image.height() as usize,
            "{inked_rows}/{}",
            image.height()
        );
        assert!(tones.len() >= 3, "tones {tones:?}");
    }

    #[test]
    fn legibility_gate_rejects_shrunken_svgs() {
        // Root cause of the garbled charts: a wide SVG squeezed to ~1 pixel per
        // column draws its 12px labels far below a pixel.
        let gantt = "gantt\n dateFormat YYYY-MM-DD\n section Work\n Build :a, 2024-01-01, 2d";
        let scale = loop {
            if let Some((_, scale)) = scaled(gantt, 100) {
                break scale;
            }
            std::thread::sleep(std::time::Duration::from_millis(10));
        };
        assert!(!legible_at(scale, MIN_SCALE), "gantt scale {scale}");
        assert!(legible_at(1.0, MIN_SCALE) && legible_at(MIN_SCALE, MIN_SCALE));
    }

    const SAMPLES: [(&str, &str); 4] = [
        (
            "mindmap",
            "mindmap\n  root((Weekend))\n    Rest\n      Nap\n      Read\n    Errands\n      Groceries\n      Laundry\n    Fun\n      Walk\n      Movie",
        ),
        (
            "quadrant",
            "quadrantChart\n  title Household Task Priority\n  x-axis Low Urgency --> High Urgency\n  y-axis Low Impact --> High Impact\n  quadrant-1 Do first\n  quadrant-2 Schedule\n  quadrant-3 Skip\n  quadrant-4 Delegate\n  Laundry: [0.3, 0.6]\n  Taxes: [0.9, 0.9]\n  Dust: [0.2, 0.2]\n  Mail: [0.8, 0.3]",
        ),
        (
            "git",
            "gitGraph\n  commit\n  commit\n  branch feature\n  checkout feature\n  commit\n  checkout main\n  merge feature",
        ),
        (
            "xychart",
            "xychart-beta\n  title \"Sales\"\n  x-axis [jan, feb, mar, apr, may]\n  line [9, 7, 5, 3, 8]",
        ),
    ];

    #[test]
    fn screenshot_fixtures_compile_and_rasterize() {
        for (name, source) in SAMPLES {
            let svg = ondine::render(source, "", 0.0)
                .unwrap_or_else(|e| panic!("{name}: {e:?}"))
                .svg;
            assert!(svg.contains("<svg"), "{name}: missing SVG");
            let (image, scale) =
                compile(source, 80).unwrap_or_else(|| panic!("{name}: failed to rasterize"));
            assert!(scale.is_finite() && scale > 0.0, "{name}: {scale}");
            assert!(image.width() <= 80 && image.height() <= MAX_ROWS * 2);
            assert!(image.pixels().any(|p| p[3] >= 12), "{name}: blank raster");
            // All four screenshot fixtures compile with `all`, but their
            // labels shrink too far at 80 columns. In production they must
            // remain readable source, not an impressive-looking blurred blob.
            assert!(!legible_at(scale, MIN_SCALE), "{name}: {scale}");
            let mut buffer = Buffer::empty(Rect::new(0, 0, 80, MAX_ROWS as u16));
            wait_raster(source, 80).expect("worker raster");
            for row in 0..image.height() / 2 {
                render(
                    source,
                    80,
                    row as usize,
                    Rect::new(0, row as u16, 80, 1),
                    &mut buffer,
                );
            }
            assert!(
                buffer.content.iter().any(|cell| cell.symbol() != " "),
                "{name}: empty painted strip"
            );
        }
    }

    #[test]
    fn bounded_fallback() {
        // Flowcharts are eligible now; a cold async request returning None
        // says nothing about support. Check the completed result instead.
        assert!(wait_raster("graph TD\n A --> B", 80).is_some());
        assert!(raster("", 80).is_none());
        assert!(raster("pie\n\"a\" : 1", 3).is_none());
        assert!(raster(&"x".repeat(MAX_SOURCE + 1), 80).is_none());
        let image = wait_raster("pie\n\"a\" : 1", 80).unwrap();
        assert!(image.width() <= MAX_COLUMNS && image.height() <= MAX_ROWS * 2);
        assert!(raster("pie\ninvalid ???", 80).is_none());
    }
}
