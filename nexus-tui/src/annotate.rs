//! Drawing on a composer image.
//!
//! The modal's state is [`Annotation`]. In Ghostty, [`Live`] draws the picture
//! with the kitty graphics protocol and sends each new piece of a stroke as a
//! small tile above it, with the mouse in pixel mode (SGR 1016): resending the
//! whole picture takes about half a second, a tile about a third of a
//! millisecond. Elsewhere the picture is redrawn when a stroke ends and the
//! stroke in progress shows as coloured cells.

use base64::{engine::general_purpose::STANDARD, Engine};
use nexus_annotate::{tile, Stroke};
use std::{cell::Cell, fmt::Write};

/// Where the picture sits, in pixels from the top-left of the terminal grid.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct PixelRect {
    pub x: u32,
    pub y: u32,
    pub width: u32,
    pub height: u32,
}

impl PixelRect {
    pub fn contains(&self, x: u32, y: u32) -> bool {
        x >= self.x && y >= self.y && x < self.x + self.width && y < self.y + self.height
    }

    /// A pixel as a point across the picture, each axis clamped to 0..=1.
    pub fn normalise(&self, x: u32, y: u32) -> [f32; 2] {
        [
            ((x as f32 - self.x as f32) / self.width.max(1) as f32).clamp(0.0, 1.0),
            ((y as f32 - self.y as f32) / self.height.max(1) as f32).clamp(0.0, 1.0),
        ]
    }
}

/// The drawing modal over composer image `index`.
#[derive(Debug, Clone, PartialEq)]
pub struct Annotation {
    pub index: usize,
    pub name: String,
    pub data_url: String,
    pub strokes: Vec<Stroke>,
    /// The last stroke is still under the pointer.
    pub drawing: bool,
    pub ink: usize,
    /// The picture with its finished strokes, where the terminal cannot draw
    /// them live. `None` until the first stroke ends.
    pub flattened: Option<String>,
    /// Where the last frame put the picture.
    pub canvas: Cell<PixelRect>,
    /// The picture's size, read once: a JPEG's needs a full base64 decode.
    pub natural: Option<(u32, u32)>,
}

impl Eq for Annotation {}

impl Annotation {
    pub fn new(index: usize, name: String, data_url: String) -> Self {
        Self {
            natural: crate::images::dimensions(&data_url),
            index,
            name,
            data_url,
            strokes: Vec::new(),
            drawing: false,
            ink: 0,
            flattened: None,
            canvas: Cell::new(PixelRect::default()),
        }
    }

    pub fn begin(&mut self, color: [u8; 3], point: [f32; 2]) {
        self.strokes.push(Stroke {
            color,
            points: vec![point],
        });
        self.drawing = true;
    }

    pub fn extend(&mut self, point: [f32; 2]) {
        if let Some(stroke) = self.strokes.last_mut() {
            if stroke.points.last() != Some(&point) {
                stroke.points.push(point);
            }
        }
    }

    /// Finished strokes only, for the cell-mode redraw.
    pub fn refresh_flattened(&mut self) {
        let done = self.strokes.len() - usize::from(self.drawing);
        self.flattened = if done == 0 {
            None
        } else {
            nexus_annotate::flatten(&self.data_url, &self.strokes[..done]).ok()
        };
    }
}

const FIRST_ID: u32 = 0x4E58_0000;
const LAST_ID: u32 = 0x4E58_FFFF;
const BASE_Z: i32 = 1;
const TILE_Z: i32 = 2;
/// Base64 characters per graphics command.
const CHUNK: usize = 3072;

/// What has been sent for one stroke.
#[derive(Debug, Default)]
struct Sent {
    ids: Vec<u32>,
    /// The first quadratic not yet sent.
    from: usize,
    finished: bool,
}

/// The terminal's copy of the picture and strokes (Ghostty only).
#[derive(Debug)]
pub struct Live {
    supported: bool,
    font: (u16, u16),
    /// Pixel mouse is on, so mouse events carry pixels instead of cells.
    pixel_mouse: bool,
    placed: Option<Placed>,
    strokes: Vec<Sent>,
    next_id: u32,
    queue: String,
}

#[derive(Debug, PartialEq)]
struct Placed {
    url: u64,
    rect: PixelRect,
    id: u32,
}

impl Default for Live {
    fn default() -> Self {
        Self {
            supported: false,
            font: (10, 20),
            pixel_mouse: false,
            placed: None,
            strokes: Vec::new(),
            next_id: FIRST_ID,
            queue: String::new(),
        }
    }
}

/// Ghostty directly: a multiplexer between us and it may keep neither the
/// graphics nor the pixel mode.
pub fn ghostty_env(term_program: Option<&str>, tmux: Option<&str>) -> bool {
    term_program.is_some_and(|name| name.eq_ignore_ascii_case("ghostty"))
        && tmux.is_none_or(str::is_empty)
}

impl Live {
    /// Set once the terminal's capabilities are known.
    pub fn configure(&mut self, supported: bool, font: (u16, u16)) {
        self.supported = supported;
        if font.0 > 0 && font.1 > 0 {
            self.font = font;
        }
    }

    pub fn supported(&self) -> bool {
        self.supported
    }

    pub fn font(&self) -> (u16, u16) {
        self.font
    }

    pub fn set_font(&mut self, font: (u16, u16)) {
        if font.0 > 0 && font.1 > 0 {
            self.font = font;
        }
    }

    pub fn pixel_mouse(&self) -> bool {
        self.pixel_mouse
    }

    /// The bytes to write after this frame.
    pub fn take(&mut self) -> String {
        std::mem::take(&mut self.queue)
    }

    /// The terminal was handed back (ctrl+z, an external editor): it has reset
    /// the mouse and cleared the screen, so everything is sent again.
    pub fn forget(&mut self) {
        self.pixel_mouse = false;
        self.placed = None;
        self.strokes.clear();
    }

    /// Bring the terminal up to date with `annotation` in `rect`.
    pub fn sync(&mut self, annotation: &Annotation, rect: PixelRect) {
        if !self.supported {
            return;
        }
        if !self.pixel_mouse {
            self.pixel_mouse = true;
            self.queue.push_str("\x1b[?1016h");
        }
        let url = hash(&annotation.data_url);
        if self.placed.as_ref().map(|p| (p.url, p.rect)) != Some((url, rect)) {
            self.delete_all();
            let Some(image) = scaled(&annotation.data_url, rect) else {
                return;
            };
            let id = self.id();
            self.place(
                id,
                image.as_raw(),
                rect.width,
                rect.height,
                rect.x,
                rect.y,
                BASE_Z,
            );
            self.placed = Some(Placed { url, rect, id });
        }
        // Undo and clear take tiles away without touching the picture.
        for sent in self
            .strokes
            .split_off(annotation.strokes.len().min(self.strokes.len()))
        {
            for id in sent.ids {
                self.delete(id);
            }
        }
        self.strokes
            .resize_with(annotation.strokes.len(), Sent::default);
        let live = annotation.strokes.len().saturating_sub(1);
        for (index, stroke) in annotation.strokes.iter().enumerate() {
            let finished = index < live || !annotation.drawing;
            let sent = &self.strokes[index];
            if sent.finished {
                continue;
            }
            let quads = stroke.points.len().saturating_sub(1);
            if !finished && quads <= sent.from.max(1) {
                continue;
            }
            let shown = tile(stroke, sent.from.max(1), finished, rect.width, rect.height);
            if let Some(shown) = shown {
                let id = self.id();
                self.place(
                    id,
                    &shown.rgba,
                    shown.width,
                    shown.height,
                    rect.x + shown.x,
                    rect.y + shown.y,
                    TILE_Z,
                );
                self.strokes[index].ids.push(id);
            }
            self.strokes[index].from = quads;
            self.strokes[index].finished = finished;
        }
    }

    /// The modal is gone: take everything back off the screen.
    pub fn teardown(&mut self) {
        if !self.supported {
            return;
        }
        self.delete_all();
        if self.pixel_mouse {
            self.pixel_mouse = false;
            self.queue.push_str("\x1b[?1016l");
        }
    }

    fn id(&mut self) -> u32 {
        let id = self.next_id;
        self.next_id = if id >= LAST_ID { FIRST_ID } else { id + 1 };
        id
    }

    fn delete_all(&mut self) {
        if let Some(placed) = self.placed.take() {
            self.delete(placed.id);
        }
        for sent in std::mem::take(&mut self.strokes) {
            for id in sent.ids {
                self.delete(id);
            }
        }
    }

    fn delete(&mut self, id: u32) {
        let _ = write!(self.queue, "\x1b_Ga=d,d=I,i={id},q=2\x1b\\");
    }

    /// Transmit and place RGBA pixels at a pixel position. The cursor is saved
    /// and restored, and `C=1` keeps the terminal from moving it.
    #[allow(clippy::too_many_arguments)]
    fn place(&mut self, id: u32, rgba: &[u8], width: u32, height: u32, x: u32, y: u32, z: i32) {
        let (cell_w, cell_h) = (u32::from(self.font.0), u32::from(self.font.1));
        let (column, row) = (x / cell_w + 1, y / cell_h + 1);
        let (offset_x, offset_y) = (x % cell_w, y % cell_h);
        let _ = write!(self.queue, "\x1b7\x1b[{row};{column}H");
        let data = STANDARD.encode(rgba);
        let mut chunks = data.as_bytes().chunks(CHUNK).peekable();
        let mut first = true;
        while let Some(chunk) = chunks.next() {
            let more = u8::from(chunks.peek().is_some());
            let chunk = std::str::from_utf8(chunk).unwrap_or_default();
            if first {
                let _ = write!(
                    self.queue,
                    "\x1b_Ga=T,i={id},f=32,s={width},v={height},X={offset_x},Y={offset_y},z={z},C=1,q=2,m={more};{chunk}\x1b\\"
                );
                first = false;
            } else {
                let _ = write!(self.queue, "\x1b_Gq=2,m={more};{chunk}\x1b\\");
            }
        }
        self.queue.push_str("\x1b8");
    }
}

fn hash(text: &str) -> u64 {
    use std::hash::{DefaultHasher, Hash, Hasher};
    let mut hasher = DefaultHasher::new();
    text.hash(&mut hasher);
    hasher.finish()
}

fn scaled(data_url: &str, rect: PixelRect) -> Option<image::RgbaImage> {
    let image = crate::images::decode(data_url)?;
    Some(
        image
            .resize_exact(
                rect.width,
                rect.height,
                image::imageops::FilterType::Triangle,
            )
            .into_rgba8(),
    )
}

/// The picture's natural size scaled to fit `area` pixels, centred in it.
/// Small pictures grow, so there is room to draw on them.
pub fn fit(natural: (u32, u32), area: PixelRect) -> PixelRect {
    let (width, height) = (natural.0.max(1) as f64, natural.1.max(1) as f64);
    let scale = (f64::from(area.width) / width).min(f64::from(area.height) / height);
    let fitted_w = ((width * scale).round() as u32).clamp(1, area.width.max(1));
    let fitted_h = ((height * scale).round() as u32).clamp(1, area.height.max(1));
    PixelRect {
        x: area.x + (area.width.saturating_sub(fitted_w)) / 2,
        y: area.y + (area.height.saturating_sub(fitted_h)) / 2,
        width: fitted_w,
        height: fitted_h,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const RECT: PixelRect = PixelRect {
        x: 45,
        y: 90,
        width: 200,
        height: 100,
    };

    fn png_url() -> String {
        let image = image::RgbaImage::from_pixel(8, 4, image::Rgba([9, 9, 9, 255]));
        let mut out = std::io::Cursor::new(Vec::new());
        image.write_to(&mut out, image::ImageFormat::Png).unwrap();
        format!(
            "data:image/png;base64,{}",
            STANDARD.encode(out.into_inner())
        )
    }

    fn live() -> Live {
        let mut live = Live::default();
        live.configure(true, (20, 40));
        live
    }

    fn annotation() -> Annotation {
        Annotation::new(0, "a.png".into(), png_url())
    }

    #[test]
    fn only_ghostty_itself_is_live() {
        assert!(ghostty_env(Some("ghostty"), None));
        assert!(!ghostty_env(Some("ghostty"), Some("/tmp/tmux,1,0")));
        assert!(!ghostty_env(Some("herdr"), None));
        assert!(!ghostty_env(None, None));
    }

    #[test]
    fn opening_turns_on_pixels_and_places_the_picture_at_its_pixel() {
        let mut live = live();
        live.sync(&annotation(), RECT);
        let sent = live.take();
        assert!(sent.starts_with("\x1b[?1016h"));
        // Pixel 45,90 is cell (2, 2) offset by (5, 10), one-based for the cursor.
        assert!(sent.contains("\x1b[3;3H"));
        assert!(sent.contains("s=200,v=100,X=5,Y=10,z=1,C=1,q=2"));
        assert!(live.pixel_mouse());
        // Nothing changed, nothing is sent again.
        live.sync(&annotation(), RECT);
        assert_eq!(live.take(), "");
    }

    #[test]
    fn a_stroke_sends_only_new_tiles_and_undo_deletes_them() {
        let mut live = live();
        let mut a = annotation();
        live.sync(&a, RECT);
        live.take();
        a.begin([255, 0, 0], [0.1, 0.5]);
        for x in [0.2, 0.3, 0.4] {
            a.extend([x, 0.5]);
        }
        live.sync(&a, RECT);
        let first = live.take();
        assert!(first.contains("z=2"));
        assert!(!first.contains("s=200,v=100"), "the picture is not resent");
        live.sync(&a, RECT);
        assert_eq!(live.take(), "", "no new points, no new tile");
        a.drawing = false;
        live.sync(&a, RECT);
        assert!(live.take().contains("z=2"), "the tail is sent on release");
        a.strokes.pop();
        live.sync(&a, RECT);
        let undone = live.take();
        assert!(undone.contains("a=d,d=I"));
        assert!(!undone.contains("a=T"));
    }

    #[test]
    fn closing_removes_everything_and_restores_the_mouse() {
        let mut live = live();
        let mut a = annotation();
        a.begin([255, 0, 0], [0.5, 0.5]);
        a.drawing = false;
        live.sync(&a, RECT);
        live.take();
        live.teardown();
        let sent = live.take();
        assert_eq!(sent.matches("a=d,d=I").count(), 2);
        assert!(sent.ends_with("\x1b[?1016l"));
        assert!(!live.pixel_mouse());
    }

    #[test]
    fn unsupported_terminals_send_nothing() {
        let mut live = Live::default();
        live.sync(&annotation(), RECT);
        live.teardown();
        assert_eq!(live.take(), "");
    }

    #[test]
    fn a_resize_sends_the_picture_and_strokes_again() {
        let mut live = live();
        let mut a = annotation();
        a.begin([255, 0, 0], [0.5, 0.5]);
        a.drawing = false;
        live.sync(&a, RECT);
        live.take();
        live.sync(
            &a,
            PixelRect {
                width: 100,
                height: 50,
                ..RECT
            },
        );
        let sent = live.take();
        assert!(sent.contains("s=100,v=50"));
        assert!(sent.contains("z=2"));
    }

    #[test]
    fn fit_centres_and_grows_small_pictures() {
        let area = PixelRect {
            x: 10,
            y: 20,
            width: 400,
            height: 400,
        };
        assert_eq!(
            fit((100, 50), area),
            PixelRect {
                x: 10,
                y: 120,
                width: 400,
                height: 200
            }
        );
    }

    #[test]
    fn normalise_clamps_to_the_picture() {
        assert_eq!(RECT.normalise(145, 140), [0.5, 0.5]);
        assert_eq!(RECT.normalise(0, 1000), [0.0, 1.0]);
    }
}
