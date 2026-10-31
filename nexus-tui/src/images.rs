//! Local clipboard images; no daemon clipboard access.
use base64::{engine::general_purpose::STANDARD, Engine};
use ratatui_image::{
    picker::{Picker, ProtocolType},
    protocol::StatefulProtocol,
};
use serde::{Deserialize, Serialize};
use std::{
    collections::{hash_map::DefaultHasher, HashMap},
    hash::{Hash, Hasher},
    io::Cursor,
    time::Duration,
};
use tokio::io::AsyncReadExt;

const MAX_BYTES: usize = 8 * 1024 * 1024;
/// A preview is a thumbnail: at most this wide and this tall, so an attachment
/// cannot push the conversation off the screen.
const PREVIEW_MAX_COLS: usize = 64;
const PREVIEW_MAX_ROWS: usize = 14;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Image {
    pub name: String,
    pub data_url: String,
}
impl Image {
    pub fn from_png(name: String, data: &[u8]) -> Result<Self, String> {
        if data.len() > MAX_BYTES {
            return Err("Images must be at most 8 MiB".into());
        }
        png::Decoder::new(Cursor::new(data))
            .read_info()
            .map_err(|e| e.to_string())?;
        Ok(Self {
            name,
            data_url: format!("data:image/png;base64,{}", STANDARD.encode(data)),
        })
    }
}

/// One rendered preview in the timeline: `start` is its first line and `key`
/// identifies the message part whose protocol state is kept in [`Viewer`].
#[derive(Clone)]
pub struct Preview {
    pub start: usize,
    pub width: usize,
    pub rows: usize,
    pub key: String,
    pub data_url: String,
}

struct Cached {
    data_url: u64,
    protocol: StatefulProtocol,
}

/// The picker and per-image protocol states used to draw previews.
///
/// `Picker::from_query_stdio` runs once at startup (after the alternate screen
/// is entered and before input is read); tests, `--dump-frame` and any terminal
/// that does not answer fall back to half-blocks.
pub struct Viewer {
    picker: Picker,
    states: HashMap<String, Cached>,
    failed: HashMap<String, u64>,
}

impl Default for Viewer {
    fn default() -> Self {
        let mut picker = Picker::from_fontsize((10, 20));
        // Deterministic for tests and dumps; the interactive client replaces
        // this with the terminal's queried protocol.
        picker.set_protocol_type(ProtocolType::Halfblocks);
        Self {
            picker,
            states: HashMap::new(),
            failed: HashMap::new(),
        }
    }
}

impl Viewer {
    pub fn set_picker(&mut self, picker: Picker) {
        if self.picker.protocol_type() != picker.protocol_type()
            || self.picker.font_size() != picker.font_size()
        {
            self.states.clear();
            self.failed.clear();
        }
        self.picker = picker;
    }

    /// The protocol for `key`, decoding its data URL on first use. `None` means
    /// the image cannot be decoded (the marker in the text still shows).
    pub fn protocol(&mut self, key: &str, data_url: &str) -> Option<&mut StatefulProtocol> {
        let mut hasher = DefaultHasher::new();
        data_url.hash(&mut hasher);
        let hash = hasher.finish();
        if self
            .states
            .get(key)
            .is_some_and(|cached| cached.data_url == hash)
        {
            return self.states.get_mut(key).map(|cached| &mut cached.protocol);
        }
        self.states.remove(key);
        // A failed decode is only retried when the data changes.
        if self.failed.get(key) == Some(&hash) {
            return None;
        }
        let Some(image) = decode(data_url) else {
            self.failed.insert(key.to_owned(), hash);
            return None;
        };
        self.failed.remove(key);
        let protocol = self.picker.new_resize_protocol(image);
        self.states.insert(
            key.to_owned(),
            Cached {
                data_url: hash,
                protocol,
            },
        );
        self.states.get_mut(key).map(|cached| &mut cached.protocol)
    }
}

/// Cell size for an attachment preview, keeping its aspect ratio. `available`
/// is the width left inside the timeline.
pub fn preview_size(data_url: &str, available: usize) -> Option<(usize, usize)> {
    let (width, height) = dimensions(data_url)?;
    let cols = available.clamp(1, PREVIEW_MAX_COLS);
    let rows = ((cols as f64 * height as f64 / width as f64) / 2.0).ceil() as usize;
    let rows = rows.clamp(1, PREVIEW_MAX_ROWS);
    let cols = if rows == PREVIEW_MAX_ROWS {
        ((rows as f64 * 2.0 * width as f64 / height as f64).ceil() as usize).clamp(1, cols)
    } else {
        cols
    };
    Some((cols, rows))
}

/// Image dimensions from a data URL, reading only the header where possible.
fn dimensions(data_url: &str) -> Option<(u32, u32)> {
    let payload = payload(data_url);
    // PNG's signature and IHDR are in the first 24 bytes; the first 44 base64
    // characters decode them without touching the rest of a large image.
    if let Some(head) = payload.get(..44) {
        if let Ok(bytes) = STANDARD.decode(head) {
            if bytes.starts_with(b"\x89PNG\r\n\x1a\n") && bytes.len() >= 24 {
                let width = u32::from_be_bytes(bytes[16..20].try_into().ok()?);
                let height = u32::from_be_bytes(bytes[20..24].try_into().ok()?);
                if width > 0 && height > 0 {
                    return Some((width, height));
                }
            }
        }
    }
    let bytes = STANDARD.decode(payload).ok()?;
    image::ImageReader::new(Cursor::new(bytes))
        .with_guessed_format()
        .ok()?
        .into_dimensions()
        .ok()
}

fn payload(data_url: &str) -> &str {
    data_url
        .split_once(',')
        .map(|(_, payload)| payload)
        .unwrap_or(data_url)
        .trim()
}

fn decode(data_url: &str) -> Option<image::DynamicImage> {
    let bytes = STANDARD.decode(payload(data_url)).ok()?;
    image::load_from_memory(&bytes).ok()
}

pub async fn clipboard() -> Result<Option<Image>, String> {
    if std::env::var_os("SSH_CONNECTION").is_some() || std::env::var_os("SSH_TTY").is_some() {
        return Err("Image paste requires a local terminal".into());
    }
    let mut cmd;
    if cfg!(target_os = "macos") {
        cmd = tokio::process::Command::new("osascript");
        cmd.args(["-l", "JavaScript", "-e", r#"ObjC.import('AppKit');
const board = $.NSPasteboard.generalPasteboard;
let data = board.dataForType($.NSPasteboardTypePNG);
if (!data) { const tiff = board.dataForType($.NSPasteboardTypeTIFF); if (tiff) data = $.NSBitmapImageRep.imageRepWithData(tiff).representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $({})); }
if (data) { if (data.length > 8388608) throw Error('Image exceeds 8 MiB'); data.base64EncodedStringWithOptions(0).js; } else { ''; }"#]);
    } else if cfg!(target_os = "linux") {
        if std::env::var_os("WAYLAND_DISPLAY").is_some() {
            cmd = tokio::process::Command::new("wl-paste");
            cmd.args(["--no-newline", "--type", "image/png"]);
        } else {
            cmd = tokio::process::Command::new("xclip");
            cmd.args(["-selection", "clipboard", "-t", "image/png", "-o"]);
        }
    } else {
        return Err("Image paste is supported on macOS and Linux".into());
    }
    cmd.kill_on_drop(true)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null());
    let mut child = cmd
        .spawn()
        .map_err(|e| format!("Could not read clipboard: {e}"))?;
    let stdout = child.stdout.take().ok_or("No clipboard output")?;
    let mut bytes = Vec::new();
    let limit = MAX_BYTES * 4 / 3 + 128;
    let read = async {
        stdout
            .take(limit as u64 + 1)
            .read_to_end(&mut bytes)
            .await?;
        child.wait().await
    };
    let status = tokio::time::timeout(Duration::from_secs(5), read)
        .await
        .map_err(|_| "Clipboard read timed out")?
        .map_err(|e| e.to_string())?;
    if bytes.len() > limit {
        return Err("Clipboard image exceeds 8 MiB".into());
    }
    if !status.success() {
        return Err("Could not read clipboard image".into());
    }
    if cfg!(target_os = "macos") {
        bytes = STANDARD
            .decode(String::from_utf8_lossy(&bytes).trim())
            .map_err(|e| e.to_string())?;
    }
    if bytes.is_empty() {
        return Ok(None);
    }
    Image::from_png("clipboard.png".into(), &bytes).map(Some)
}
