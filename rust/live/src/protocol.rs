//! stdio JSONL protocol between a Nexus client and `nexus-live`.
//!
//! Contract (plan section 4.2): one JSON object per line. stdin carries
//! [`Command`]s, stdout carries [`Event`]s, stderr is diagnostics only. Lines
//! are bounded at [`MAX_LINE`] bytes; longer lines are discarded and reported.

use serde::{Deserialize, Serialize};
use tokio::io::{AsyncBufRead, AsyncBufReadExt};

/// Longest accepted stdin line (an SDP offer/answer fits comfortably).
pub const MAX_LINE: usize = 64 * 1024;

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Command {
    Offer,
    Answer { sdp: String },
    Mute { on: bool },
    Stop,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Phase {
    Listening,
    Speaking,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Event {
    Offer {
        sdp: String,
    },
    Ready,
    Levels {
        #[serde(rename = "in")]
        input: f32,
        out: f32,
    },
    Phase {
        phase: Phase,
    },
    DcError {
        message: String,
    },
    Dropped {
        frames: u64,
    },
    Error {
        message: String,
    },
    Closed {
        reason: String,
    },
}

impl Event {
    pub fn to_line(&self) -> String {
        serde_json::to_string(self).unwrap_or_else(|_| {
            r#"{"type":"error","message":"event serialization failed"}"#.to_string()
        })
    }
}

pub fn parse_command(line: &str) -> Result<Command, String> {
    serde_json::from_str::<Command>(line).map_err(|e| format!("invalid command: {e}"))
}

/// What the stdin reader produced.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Input {
    Line(String),
    /// A line exceeded [`MAX_LINE`]; it was skipped up to its newline.
    TooLong,
    Eof,
}

/// Read one newline-terminated line without ever buffering more than `max`
/// bytes of it. An over-long line is consumed to its newline and reported as
/// [`Input::TooLong`]. A final unterminated line is returned as a line.
pub async fn read_bounded_line<R: AsyncBufRead + Unpin>(
    reader: &mut R,
    max: usize,
) -> std::io::Result<Input> {
    let mut line: Vec<u8> = Vec::new();
    let mut too_long = false;
    let mut seen_any = false;
    loop {
        let buf = reader.fill_buf().await?;
        if buf.is_empty() {
            if too_long {
                return Ok(Input::TooLong);
            }
            if seen_any {
                return Ok(Input::Line(decode(&line)));
            }
            return Ok(Input::Eof);
        }
        seen_any = true;
        let (chunk, found, consumed) = match buf.iter().position(|b| *b == b'\n') {
            Some(i) => (&buf[..i], true, i + 1),
            None => (buf, false, buf.len()),
        };
        if !too_long {
            if line.len() + chunk.len() > max {
                too_long = true;
                line.clear();
            } else {
                line.extend_from_slice(chunk);
            }
        }
        reader.consume(consumed);
        if found {
            return Ok(if too_long {
                Input::TooLong
            } else {
                Input::Line(decode(&line))
            });
        }
    }
}

fn decode(bytes: &[u8]) -> String {
    let s = String::from_utf8_lossy(bytes);
    s.trim_end_matches('\r').to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_every_command() {
        assert_eq!(parse_command(r#"{"type":"offer"}"#), Ok(Command::Offer));
        assert_eq!(
            parse_command(r#"{"type":"answer","sdp":"v=0"}"#),
            Ok(Command::Answer { sdp: "v=0".into() })
        );
        assert_eq!(
            parse_command(r#"{"type":"mute","on":true}"#),
            Ok(Command::Mute { on: true })
        );
        assert_eq!(parse_command(r#"{"type":"stop","extra":1}"#), Ok(Command::Stop));
    }

    #[test]
    fn rejects_bad_commands() {
        assert!(parse_command("not json").is_err());
        assert!(parse_command(r#"{"type":"nope"}"#).is_err());
        assert!(parse_command(r#"{"type":"mute"}"#).is_err());
        assert!(parse_command(r#"{"type":"answer"}"#).is_err());
    }

    #[test]
    fn serializes_events_with_documented_shape() {
        let v = |e: Event| serde_json::from_str::<serde_json::Value>(&e.to_line()).unwrap();
        assert_eq!(v(Event::Ready), serde_json::json!({"type":"ready"}));
        assert_eq!(
            v(Event::Offer { sdp: "x".into() }),
            serde_json::json!({"type":"offer","sdp":"x"})
        );
        assert_eq!(
            v(Event::Levels { input: 0.5, out: 0.25 }),
            serde_json::json!({"type":"levels","in":0.5,"out":0.25})
        );
        assert_eq!(
            v(Event::Phase { phase: Phase::Speaking }),
            serde_json::json!({"type":"phase","phase":"speaking"})
        );
        assert_eq!(
            v(Event::DcError { message: "m".into() }),
            serde_json::json!({"type":"dc_error","message":"m"})
        );
        assert_eq!(
            v(Event::Dropped { frames: 3 }),
            serde_json::json!({"type":"dropped","frames":3})
        );
        assert_eq!(
            v(Event::Closed { reason: "stop".into() }),
            serde_json::json!({"type":"closed","reason":"stop"})
        );
        assert!(!Event::Ready.to_line().contains('\n'));
    }

    #[tokio::test]
    async fn bounded_reader_splits_lines_and_rejects_long_ones() {
        let mut data = Vec::new();
        data.extend_from_slice(b"{\"type\":\"stop\"}\r\n");
        data.extend(std::iter::repeat(b'a').take(100));
        data.extend_from_slice(b"\nlast");
        let mut r = tokio::io::BufReader::with_capacity(16, &data[..]);
        assert_eq!(
            read_bounded_line(&mut r, 32).await.unwrap(),
            Input::Line("{\"type\":\"stop\"}".into())
        );
        assert_eq!(read_bounded_line(&mut r, 32).await.unwrap(), Input::TooLong);
        assert_eq!(
            read_bounded_line(&mut r, 32).await.unwrap(),
            Input::Line("last".into())
        );
        assert_eq!(read_bounded_line(&mut r, 32).await.unwrap(), Input::Eof);
    }

    #[tokio::test]
    async fn exact_limit_is_accepted_and_over_limit_is_not() {
        let ok = format!("{}\n", "x".repeat(MAX_LINE));
        let bad = format!("{}\n", "x".repeat(MAX_LINE + 1));
        let mut r = tokio::io::BufReader::new(ok.as_bytes());
        assert!(matches!(
            read_bounded_line(&mut r, MAX_LINE).await.unwrap(),
            Input::Line(l) if l.len() == MAX_LINE
        ));
        let mut r = tokio::io::BufReader::new(bad.as_bytes());
        assert_eq!(
            read_bounded_line(&mut r, MAX_LINE).await.unwrap(),
            Input::TooLong
        );
    }
}
