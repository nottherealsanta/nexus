//! Local clipboard images; no daemon clipboard access.
use base64::{engine::general_purpose::STANDARD, Engine};
use serde::{Deserialize, Serialize};
use std::{io::Cursor, time::Duration};
use tokio::io::AsyncReadExt;

const MAX_BYTES: usize = 8 * 1024 * 1024;
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
