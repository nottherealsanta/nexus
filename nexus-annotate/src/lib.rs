//! Marks drawn on a composer image. See the README.

use base64::{engine::general_purpose::STANDARD, Engine};
use image::{codecs::jpeg::JpegEncoder, DynamicImage, ImageFormat, RgbaImage};
use std::{fmt, io::Cursor};
use tiny_skia::{
    Color, FillRule, LineCap, LineJoin, Paint, PathBuilder, Pixmap, PixmapMut, Stroke as Line,
    Transform,
};

/// The most a flattened image may hold; Core's limit for one image.
pub const MAX_BYTES: usize = 8 * 1024 * 1024;
/// The pen width, as a fraction of the image's longer side.
pub const PEN_WIDTH: f32 = 0.004;
/// A pen never gets thinner than this many pixels.
const MIN_PEN_PIXELS: f32 = 2.0;

/// One pen stroke. `points` are `[x, y]`, each 0 to 1 across the image.
#[derive(Debug, Clone, PartialEq)]
pub struct Stroke {
    pub color: [u8; 3],
    pub points: Vec<[f32; 2]>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    /// The input is not a base64 image data URL.
    NotAnImage,
    /// The image could not be decoded or encoded.
    Codec(String),
    /// Even the smallest allowed encoding is over [`MAX_BYTES`].
    TooLarge,
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotAnImage => f.write_str("Not an image"),
            Self::Codec(message) => write!(f, "Could not process the image: {message}"),
            Self::TooLarge => f.write_str("The marked-up image exceeds 8 MiB"),
        }
    }
}

impl std::error::Error for Error {}

/// A rectangle of straight-alpha RGBA pixels at `(x, y)` in the display image.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Tile {
    pub x: u32,
    pub y: u32,
    pub width: u32,
    pub height: u32,
    pub rgba: Vec<u8>,
}

fn pen_pixels(width: u32, height: u32) -> f32 {
    (PEN_WIDTH * width.max(height) as f32).max(MIN_PEN_PIXELS)
}

/// The stroke in pixels, as midpoint quadratics: `quads` is the range of
/// quadratics wanted and `tail` adds the last straight run to the final point.
/// Quadratic `i` runs through control point `i`, so pieces cut at any index
/// join exactly. Returns `None` when the range holds nothing to draw.
fn path(
    points: &[[f32; 2]],
    width: u32,
    height: u32,
    quads: std::ops::Range<usize>,
    tail: bool,
) -> Option<tiny_skia::Path> {
    let at = |i: usize| {
        (
            points[i][0].clamp(0.0, 1.0) * width as f32,
            points[i][1].clamp(0.0, 1.0) * height as f32,
        )
    };
    let middle = |i: usize| {
        let (a, b) = (at(i), at(i + 1));
        ((a.0 + b.0) / 2.0, (a.1 + b.1) / 2.0)
    };
    let last = points.len().checked_sub(1)?;
    if last == 0 {
        return None;
    }
    let quads = quads.start.max(1)..quads.end.min(last);
    let mut builder = PathBuilder::new();
    if quads.is_empty() {
        if !tail {
            return None;
        }
        // No quadratic: the whole of what is left is the straight tail.
        let start = if last == 1 { at(0) } else { middle(last - 1) };
        builder.move_to(start.0, start.1);
    } else {
        let start = if quads.start == 1 {
            at(0)
        } else {
            middle(quads.start - 1)
        };
        builder.move_to(start.0, start.1);
        for i in quads {
            let (control, end) = (at(i), middle(i));
            builder.quad_to(control.0, control.1, end.0, end.1);
        }
    }
    if tail {
        let end = at(last);
        builder.line_to(end.0, end.1);
    }
    builder.finish()
}

fn ink(color: [u8; 3]) -> Paint<'static> {
    let mut paint = Paint::default();
    paint.set_color(Color::from_rgba8(color[0], color[1], color[2], 255));
    paint.anti_alias = true;
    paint
}

fn line(width: f32) -> Line {
    Line {
        width,
        line_cap: LineCap::Round,
        line_join: LineJoin::Round,
        ..Line::default()
    }
}

/// A tap leaves a dot, which a path of one point cannot.
fn dot(point: [f32; 2], width: u32, height: u32, radius: f32) -> Option<tiny_skia::Path> {
    PathBuilder::from_circle(
        point[0].clamp(0.0, 1.0) * width as f32,
        point[1].clamp(0.0, 1.0) * height as f32,
        radius,
    )
}

/// Paints whole strokes onto straight-alpha RGBA pixels.
pub fn paint(rgba: &mut [u8], width: u32, height: u32, strokes: &[Stroke]) {
    premultiply(rgba);
    if let Some(mut pixmap) = PixmapMut::from_bytes(rgba, width, height) {
        let pen = pen_pixels(width, height);
        for stroke in strokes {
            let paint = ink(stroke.color);
            match stroke.points.as_slice() {
                [] => {}
                [point] => {
                    if let Some(circle) = dot(*point, width, height, pen / 2.0) {
                        pixmap.fill_path(
                            &circle,
                            &paint,
                            FillRule::Winding,
                            Transform::identity(),
                            None,
                        );
                    }
                }
                points => {
                    if let Some(path) = path(points, width, height, 1..points.len(), true) {
                        pixmap.stroke_path(&path, &paint, &line(pen), Transform::identity(), None);
                    }
                }
            }
        }
    }
    demultiply(rgba);
}

fn premultiply(rgba: &mut [u8]) {
    for pixel in rgba.chunks_exact_mut(4) {
        let alpha = u16::from(pixel[3]);
        if alpha < 255 {
            for channel in &mut pixel[..3] {
                *channel = ((u16::from(*channel) * alpha + 127) / 255) as u8;
            }
        }
    }
}

fn demultiply(rgba: &mut [u8]) {
    for pixel in rgba.chunks_exact_mut(4) {
        let alpha = u16::from(pixel[3]);
        if alpha > 0 && alpha < 255 {
            for channel in &mut pixel[..3] {
                *channel = ((u16::from(*channel) * 255 + alpha / 2) / alpha).min(255) as u8;
            }
        }
    }
}

/// Paints quadratics `from..` of a stroke on a `width` x `height` display image,
/// plus the tail when `finished`. `None` means nothing new to draw.
pub fn tile(stroke: &Stroke, from: usize, finished: bool, width: u32, height: u32) -> Option<Tile> {
    let pen = pen_pixels(width, height);
    let points = &stroke.points;
    let shape = if points.len() == 1 {
        if !finished {
            return None;
        }
        Err(dot(points[0], width, height, pen / 2.0)?)
    } else {
        Ok(path(points, width, height, from..points.len(), finished)?)
    };
    let bounds = match &shape {
        Ok(path) => path.bounds(),
        Err(circle) => circle.bounds(),
    };
    let margin = pen + 2.0;
    let x0 = (bounds.left() - margin).floor().max(0.0) as u32;
    let y0 = (bounds.top() - margin).floor().max(0.0) as u32;
    let x1 = ((bounds.right() + margin).ceil() as u32).min(width);
    let y1 = ((bounds.bottom() + margin).ceil() as u32).min(height);
    if x1 <= x0 || y1 <= y0 {
        return None;
    }
    let mut pixmap = Pixmap::new(x1 - x0, y1 - y0)?;
    let shift = Transform::from_translate(-(x0 as f32), -(y0 as f32));
    let paint = ink(stroke.color);
    match &shape {
        Ok(path) => pixmap.stroke_path(path, &paint, &line(pen), shift, None),
        Err(circle) => pixmap.fill_path(circle, &paint, FillRule::Winding, shift, None),
    }
    let (tile_width, tile_height) = (pixmap.width(), pixmap.height());
    let mut rgba = pixmap.take();
    demultiply(&mut rgba);
    Some(Tile {
        x: x0,
        y: y0,
        width: tile_width,
        height: tile_height,
        rgba,
    })
}

/// The bytes and whether the source was a JPEG.
fn decode_url(data_url: &str) -> Result<(Vec<u8>, bool), Error> {
    let rest = data_url.strip_prefix("data:").ok_or(Error::NotAnImage)?;
    let (head, body) = rest.split_once(',').ok_or(Error::NotAnImage)?;
    let mime = head.strip_suffix(";base64").ok_or(Error::NotAnImage)?;
    if !mime.starts_with("image/") {
        return Err(Error::NotAnImage);
    }
    let bytes = STANDARD.decode(body).map_err(|_| Error::NotAnImage)?;
    Ok((bytes, mime == "image/jpeg" || mime == "image/jpg"))
}

fn url(mime: &str, bytes: &[u8]) -> String {
    format!("data:{mime};base64,{}", STANDARD.encode(bytes))
}

/// JPEG has no alpha, so transparency is flattened onto white.
fn jpeg(image: &RgbaImage, quality: u8) -> Result<Vec<u8>, Error> {
    let mut rgb = image::RgbImage::new(image.width(), image.height());
    for (to, from) in rgb.pixels_mut().zip(image.pixels()) {
        let alpha = u32::from(from[3]);
        for channel in 0..3 {
            to[channel] = ((u32::from(from[channel]) * alpha + 255 * (255 - alpha)) / 255) as u8;
        }
    }
    let mut out = Vec::new();
    rgb.write_with_encoder(JpegEncoder::new_with_quality(&mut out, quality))
        .map_err(|error| Error::Codec(error.to_string()))?;
    Ok(out)
}

fn png(image: &RgbaImage) -> Result<Vec<u8>, Error> {
    let mut out = Cursor::new(Vec::new());
    image
        .write_to(&mut out, ImageFormat::Png)
        .map_err(|error| Error::Codec(error.to_string()))?;
    Ok(out.into_inner())
}

/// Merges `strokes` into the image. With no strokes the input comes back
/// untouched, so an animated GIF is only reduced to a frame once it is drawn on.
pub fn flatten(data_url: &str, strokes: &[Stroke]) -> Result<String, Error> {
    let (bytes, was_jpeg) = decode_url(data_url)?;
    if strokes.iter().all(|stroke| stroke.points.is_empty()) {
        return Ok(data_url.to_owned());
    }
    let decoded: DynamicImage =
        image::load_from_memory(&bytes).map_err(|error| Error::Codec(error.to_string()))?;
    let mut image = decoded.into_rgba8();
    let (width, height) = image.dimensions();
    paint(&mut image, width, height, strokes);
    if !was_jpeg {
        let encoded = png(&image)?;
        if encoded.len() <= MAX_BYTES {
            return Ok(url("image/png", &encoded));
        }
    }
    for quality in [92, 80, 65] {
        let encoded = jpeg(&image, quality)?;
        if encoded.len() <= MAX_BYTES {
            return Ok(url("image/jpeg", &encoded));
        }
    }
    Err(Error::TooLarge)
}

#[cfg(test)]
mod tests {
    use super::*;

    const RED: [u8; 3] = [255, 0, 0];

    fn grey(width: u32, height: u32) -> RgbaImage {
        RgbaImage::from_pixel(width, height, image::Rgba([40, 40, 40, 255]))
    }

    fn png_url(image: &RgbaImage) -> String {
        url("image/png", &png(image).unwrap())
    }

    fn decoded(data_url: &str) -> RgbaImage {
        let (bytes, _) = decode_url(data_url).unwrap();
        image::load_from_memory(&bytes).unwrap().into_rgba8()
    }

    fn across() -> Stroke {
        Stroke {
            color: RED,
            points: vec![[0.1, 0.5], [0.5, 0.5], [0.9, 0.5]],
        }
    }

    #[test]
    fn flatten_marks_the_stroke_and_leaves_the_rest() {
        let out = decoded(&flatten(&png_url(&grey(200, 100)), &[across()]).unwrap());
        assert_eq!(out.dimensions(), (200, 100));
        assert_eq!(&out.get_pixel(100, 50).0[..3], &RED);
        assert_eq!(&out.get_pixel(100, 10).0[..3], &[40, 40, 40]);
    }

    #[test]
    fn no_strokes_return_the_input_unchanged() {
        let input = png_url(&grey(8, 8));
        assert_eq!(flatten(&input, &[]).unwrap(), input);
    }

    #[test]
    fn a_tap_leaves_a_dot_on_both_paths() {
        let tap = Stroke {
            color: RED,
            points: vec![[0.5, 0.5]],
        };
        let out = decoded(&flatten(&png_url(&grey(800, 800)), std::slice::from_ref(&tap)).unwrap());
        assert_eq!(&out.get_pixel(400, 400).0[..3], &RED);
        assert!(tile(&tap, 1, false, 800, 800).is_none());
        let shown = tile(&tap, 1, true, 800, 800).unwrap();
        assert!(shown.rgba.chunks(4).any(|pixel| pixel[3] == 255));
    }

    #[test]
    fn jpeg_stays_jpeg() {
        let source = url("image/jpeg", &jpeg(&grey(64, 64), 90).unwrap());
        assert!(flatten(&source, &[across()])
            .unwrap()
            .starts_with("data:image/jpeg;base64,"));
    }

    #[test]
    fn transparency_survives_a_png() {
        let clear = RgbaImage::from_pixel(100, 100, image::Rgba([0, 0, 0, 0]));
        let out = decoded(&flatten(&png_url(&clear), &[across()]).unwrap());
        assert_eq!(out.get_pixel(50, 5).0[3], 0);
        assert_eq!(out.get_pixel(50, 50).0, [255, 0, 0, 255]);
    }

    #[test]
    fn rejects_what_is_not_an_image() {
        assert_eq!(flatten("hello", &[]), Err(Error::NotAnImage));
        assert_eq!(
            flatten("data:text/plain;base64,aGk=", &[]),
            Err(Error::NotAnImage)
        );
        assert!(matches!(
            flatten("data:image/png;base64,aGk=", &[across()]),
            Err(Error::Codec(_))
        ));
    }

    /// Tiles cut while the pointer moves must draw what the flattened stroke
    /// draws: any pixel the whole stroke paints solid is solid in a tile.
    #[test]
    fn tiles_join_into_the_flattened_stroke() {
        let (width, height) = (400, 300);
        let points: Vec<[f32; 2]> = (0..40)
            .map(|i| {
                let t = i as f32 / 39.0;
                [0.1 + 0.8 * t, 0.5 + 0.3 * (t * std::f32::consts::TAU).sin()]
            })
            .collect();
        let whole = Stroke {
            color: RED,
            points: points.clone(),
        };
        let mut full = vec![0u8; (width * height * 4) as usize];
        paint(&mut full, width, height, std::slice::from_ref(&whole));

        let mut pieced = vec![0u8; (width * height * 4) as usize];
        let mut drawn = 1;
        let mut seen = 1;
        while seen <= points.len() {
            let finished = seen == points.len();
            let part = Stroke {
                color: RED,
                points: points[..seen].to_vec(),
            };
            if let Some(tile) = tile(&part, drawn, finished, width, height) {
                for y in 0..tile.height {
                    for x in 0..tile.width {
                        let from = ((y * tile.width + x) * 4) as usize;
                        let to = (((tile.y + y) * width + tile.x + x) * 4) as usize;
                        if tile.rgba[from + 3] > pieced[to + 3] {
                            pieced[to..to + 4].copy_from_slice(&tile.rgba[from..from + 4]);
                        }
                    }
                }
            }
            drawn = seen.saturating_sub(1).max(1);
            seen += 3;
            if seen > points.len() && !finished {
                seen = points.len();
            }
        }
        let missing = full
            .chunks(4)
            .zip(pieced.chunks(4))
            .filter(|(a, b)| a[3] == 255 && b[3] < 128)
            .count();
        assert_eq!(missing, 0);
    }
}
