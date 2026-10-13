//! `nexus-live` entry point: wires stdin/stdout JSONL to [`nexus_live::app::run`].

use nexus_live::app::{run, Options};
use nexus_live::audio::{AudioBackend, Pipes};
use nexus_live::protocol::{read_bounded_line, Event, Input, MAX_LINE};
use std::io::Write;
use tokio::sync::mpsc::unbounded_channel;

fn backend() -> Box<dyn AudioBackend> {
    #[cfg(feature = "audio")]
    {
        Box::new(nexus_live::audio::CpalBackend)
    }
    #[cfg(not(feature = "audio"))]
    {
        Box::new(nexus_live::audio::UnavailableBackend)
    }
}

#[tokio::main]
async fn main() {
    if let Some(arg) = std::env::args().nth(1) {
        match arg.as_str() {
            "--version" | "-V" => {
                println!("nexus-live {}", env!("CARGO_PKG_VERSION"));
                return;
            }
            "--help" | "-h" => {
                println!("nexus-live: audio/WebRTC helper for Nexus GPT Live.\nSpeaks JSONL on stdin/stdout; see rust/live/README.md.");
                return;
            }
            other => {
                eprintln!("nexus-live: unknown argument {other}");
                std::process::exit(2);
            }
        }
    }

    let (itx, irx) = unbounded_channel::<Input>();
    let (etx, mut erx) = unbounded_channel::<Event>();

    tokio::spawn(async move {
        let mut reader = tokio::io::BufReader::new(tokio::io::stdin());
        loop {
            let item = match read_bounded_line(&mut reader, MAX_LINE).await {
                Ok(i) => i,
                Err(_) => Input::Eof,
            };
            let eof = item == Input::Eof;
            if itx.send(item).is_err() || eof {
                break;
            }
        }
    });

    let writer = tokio::task::spawn_blocking(move || {
        let stdout = std::io::stdout();
        while let Some(ev) = erx.blocking_recv() {
            let mut out = stdout.lock();
            if writeln!(out, "{}", ev.to_line()).and_then(|_| out.flush()).is_err() {
                break; // client went away
            }
        }
    });

    run(irx, etx, backend(), Pipes::new(), Options::default()).await;
    let _ = writer.await;
    // The stdin reader may still be blocked in a read; do not wait for it.
    std::process::exit(0);
}
