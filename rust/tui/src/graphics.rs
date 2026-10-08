//! Terminal graphics detection for `ratatui-image` previews (docs/ratatui-parity.md).
//!
//! stdin/stdout are the bridge pipes, so the library's own stdio query cannot be
//! used. Instead the controlling terminal (`/dev/tty`) is asked once at startup,
//! before the event loop reads input: a Kitty graphics probe, the cell size in
//! pixels (`CSI 16 t`) and primary device attributes (`CSI c`, Sixel = 4). The DA1
//! reply ends the read; the wait is bounded by `TIMEOUT`. iTerm2 and tmux are
//! taken from the environment by `Picker::from_fontsize`. Anything undetected
//! falls back to half blocks, which every terminal draws. `NEXUS_IMAGE_PROTOCOL`
//! overrides detection for terminals that never answer the probe.
use ratatui_image::picker::{Picker, ProtocolType};
use std::sync::OnceLock;
use std::time::{Duration, Instant};

/// Used when the terminal does not report its cell size.
const FALLBACK_FONT: (u16, u16) = (10, 20);
const TIMEOUT: Duration = Duration::from_millis(400);
/// Longest reply read before giving up on a terminal that floods the line.
const MAX_REPLY: usize = 4096;

static PICKER: OnceLock<Picker> = OnceLock::new();

/// What a terminal's replies say about its graphics support.
#[derive(Debug, Default, PartialEq)]
pub struct Reply {
    pub kitty: bool,
    pub sixel: bool,
    pub font: Option<(u16, u16)>,
    /// DA1 arrived, so no further reply is coming.
    pub done: bool,
}

/// Parse the terminal replies collected so far.
pub fn parse(bytes: &[u8]) -> Reply {
    let text = String::from_utf8_lossy(bytes);
    let mut reply = Reply {
        kitty: text.contains("\x1b_Gi=31;OK"),
        ..Reply::default()
    };
    for part in text.split("\x1b[").skip(1) {
        if let Some(body) = part.strip_prefix('?') {
            if let Some(end) = body.find('c') {
                if body[..end].bytes().all(|b| b.is_ascii_digit() || b == b';') {
                    reply.done = true;
                    reply.sixel |= body[..end].split(';').skip(1).any(|p| p == "4");
                }
            }
        } else if let Some(body) = part.strip_prefix("6;") {
            if let Some(end) = body.find('t') {
                let mut values = body[..end].split(';').map(str::parse::<u16>);
                if let (Some(Ok(h)), Some(Ok(w))) = (values.next(), values.next()) {
                    if w > 0 && h > 0 {
                        reply.font = Some((w, h));
                    }
                }
            }
        }
    }
    reply
}

#[cfg(unix)]
fn query() -> Option<Reply> {
    use std::io::{Read, Write};
    use std::os::fd::AsRawFd;
    if std::env::var_os("TMUX").is_some() {
        return None; // tmux answers for itself; from_fontsize handles passthrough.
    }
    let mut tty = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open("/dev/tty")
        .ok()?;
    tty.write_all(b"\x1b_Gi=31,s=1,v=1,a=q,t=d,f=24;AAAA\x1b\\\x1b[16t\x1b[c")
        .ok()?;
    tty.flush().ok()?;
    let deadline = Instant::now() + TIMEOUT;
    let mut bytes = Vec::new();
    let mut buffer = [0u8; 256];
    loop {
        let left = deadline.saturating_duration_since(Instant::now());
        if left.is_zero() || bytes.len() > MAX_REPLY {
            break;
        }
        let mut fd = libc::pollfd {
            fd: tty.as_raw_fd(),
            events: libc::POLLIN,
            revents: 0,
        };
        // SAFETY: one valid pollfd for the duration of the call.
        let ready = unsafe { libc::poll(&mut fd, 1, left.as_millis() as libc::c_int) };
        if ready <= 0 {
            break;
        }
        let read = tty.read(&mut buffer).ok()?;
        if read == 0 {
            break;
        }
        bytes.extend_from_slice(&buffer[..read]);
        if parse(&bytes).done {
            break;
        }
    }
    Some(parse(&bytes))
}

#[cfg(not(unix))]
fn query() -> Option<Reply> {
    None
}

/// `NEXUS_IMAGE_PROTOCOL` forces a protocol (`kitty`, `sixel`, `iterm2`,
/// `halfblocks`) and skips the probe; unset or `auto` probes.
fn forced() -> Option<ProtocolType> {
    match std::env::var("NEXUS_IMAGE_PROTOCOL").ok()?.as_str() {
        "kitty" => Some(ProtocolType::Kitty),
        "sixel" => Some(ProtocolType::Sixel),
        "iterm2" => Some(ProtocolType::Iterm2),
        "halfblocks" => Some(ProtocolType::Halfblocks),
        _ => None,
    }
}

/// Probe once; the terminal must already be in raw mode with no reader running.
pub fn detect() {
    if let Some(protocol) = forced() {
        let mut picker = Picker::from_fontsize(FALLBACK_FONT);
        picker.set_protocol_type(protocol);
        let _ = PICKER.set(picker);
        return;
    }
    let reply = query().unwrap_or_default();
    let mut picker = Picker::from_fontsize(reply.font.unwrap_or(FALLBACK_FONT));
    if reply.kitty {
        picker.set_protocol_type(ProtocolType::Kitty);
    } else if picker.protocol_type() == ProtocolType::Halfblocks && reply.sixel {
        picker.set_protocol_type(ProtocolType::Sixel);
    }
    let _ = PICKER.set(picker);
}

/// The detected picker, or the half-block fallback when detection never ran.
pub fn picker() -> Picker {
    PICKER
        .get()
        .cloned()
        .unwrap_or_else(|| Picker::from_fontsize(FALLBACK_FONT))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_kitty_cell_size_and_sixel_replies() {
        let reply = parse(b"\x1b_Gi=31;OK\x1b\\\x1b[6;20;10t\x1b[?62;4;22c");
        assert_eq!(
            reply,
            Reply {
                kitty: true,
                sixel: true,
                font: Some((10, 20)),
                done: true
            }
        );
        let plain = parse(b"\x1b[?1;2c");
        assert!(plain.done && !plain.kitty && !plain.sixel && plain.font.is_none());
        assert!(!parse(b"\x1b[6;18").done);
    }
}
