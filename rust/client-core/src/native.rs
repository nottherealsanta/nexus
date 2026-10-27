//! Bounded local daemon protocol transport. The daemon must already be running.
use serde_json::{json, Value};
use std::{
    io::{Read, Write},
    os::unix::net::UnixStream,
    path::PathBuf,
    time::Duration,
};
const MAX_FRAME: usize = 16 * 1024 * 1024;
fn socket() -> Result<PathBuf, String> {
    if let Some(path) = std::env::var_os("NEXUS_DAEMON_SOCKET") {
        return Ok(path.into());
    }
    let home = std::env::var_os("NEXUS_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".nexus")))
        .ok_or("Nexus home unavailable")?;
    let mut sockets: Vec<_> = std::fs::read_dir(home.join("daemon"))
        .map_err(|_| "Daemon socket not found; start nexus daemon or set NEXUS_DAEMON_SOCKET")?
        .filter_map(Result::ok)
        .map(|e| e.path())
        .filter(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .is_some_and(|n| n.starts_with("shared-v7-") && n.ends_with(".sock"))
        })
        .collect();
    if sockets.len() != 1 {
        return Err(
            "Expected one daemon socket; set NEXUS_DAEMON_SOCKET to select the install".into(),
        );
    }
    Ok(sockets.remove(0))
}
fn write(stream: &mut UnixStream, v: Value) -> Result<(), String> {
    let body = serde_json::to_vec(&v).map_err(|e| e.to_string())?;
    if body.len() > MAX_FRAME {
        return Err("Frame too large".into());
    }
    stream
        .write_all(&(body.len() as u32).to_be_bytes())
        .and_then(|_| stream.write_all(&body))
        .map_err(|e| e.to_string())
}
fn read(stream: &mut UnixStream) -> Result<Value, String> {
    let mut size = [0; 4];
    stream.read_exact(&mut size).map_err(|e| e.to_string())?;
    let n = u32::from_be_bytes(size) as usize;
    if n > MAX_FRAME {
        return Err("Frame too large".into());
    }
    let mut data = vec![0; n];
    stream.read_exact(&mut data).map_err(|e| e.to_string())?;
    serde_json::from_slice(&data).map_err(|e| e.to_string())
}
pub fn request(path: &str, body: Option<Value>) -> Result<Value, String> {
    let mut stream = UnixStream::connect(socket()?).map_err(|e| e.to_string())?;
    stream
        .set_read_timeout(Some(Duration::from_secs(10)))
        .map_err(|e| e.to_string())?;
    stream
        .set_write_timeout(Some(Duration::from_secs(10)))
        .map_err(|e| e.to_string())?;
    let focus = std::env::current_dir()
        .map_err(|e| e.to_string())?
        .to_string_lossy()
        .into_owned();
    write(
        &mut stream,
        json!({"type":"hello","version":7,"focus":focus,"client":"nsh"}),
    )?;
    let welcome = read(&mut stream)?;
    if welcome["type"] != "welcome" {
        return Err(format!(
            "Daemon rejected connection: {}",
            welcome["reason"].as_str().unwrap_or("unexpected response")
        ));
    }
    let command = if let Some(id) = path.strip_prefix("session-view?session=") {
        json!({"type":"SessionInspect","session":id,"namespace":"main"})
    } else {
        body.ok_or("Unsupported native request")?
    };
    write(
        &mut stream,
        json!({"type":"command","id":"1","command":command}),
    )?;
    loop {
        let frame = read(&mut stream)?;
        if frame["type"] == "result" && frame["id"] == "1" {
            let result = frame["result"].clone();
            if result["type"] == "ErrorResult" {
                return Err(result["message"]
                    .as_str()
                    .unwrap_or("Command failed")
                    .into());
            }
            return Ok(result);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn frames_round_trip_and_reject_oversize() {
        let (mut sender, mut receiver) = UnixStream::pair().unwrap();
        let value = json!({"type":"hello", "version":7});
        write(&mut sender, value.clone()).unwrap();
        assert_eq!(read(&mut receiver).unwrap(), value);
        sender
            .write_all(&((MAX_FRAME + 1) as u32).to_be_bytes())
            .unwrap();
        assert_eq!(read(&mut receiver).unwrap_err(), "Frame too large");
    }
}
