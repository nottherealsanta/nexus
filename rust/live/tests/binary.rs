//! Process-level checks of the real `nexus-live` binary over stdio.

use std::io::{Read, Write};
use std::process::{Command, Stdio};

fn spawn() -> std::process::Child {
    Command::new(env!("CARGO_BIN_EXE_nexus-live"))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .unwrap()
}

#[test]
fn stdin_eof_emits_one_closed_and_exits() {
    let mut child = spawn();
    drop(child.stdin.take());
    let mut out = String::new();
    child.stdout.take().unwrap().read_to_string(&mut out).unwrap();
    assert!(child.wait().unwrap().success());
    assert_eq!(out.trim(), r#"{"type":"closed","reason":"stdin_eof"}"#);
}

#[test]
fn oversized_line_is_rejected_then_stop_closes_once() {
    let mut child = spawn();
    {
        let mut stdin = child.stdin.take().unwrap();
        let big = "x".repeat(70 * 1024);
        writeln!(stdin, "{big}").unwrap();
        writeln!(stdin, "{{\"type\":\"stop\"}}").unwrap();
        writeln!(stdin, "{{\"type\":\"stop\"}}").unwrap();
    }
    let mut out = String::new();
    child.stdout.take().unwrap().read_to_string(&mut out).unwrap();
    child.wait().unwrap();
    let lines: Vec<&str> = out.lines().collect();
    assert_eq!(lines.len(), 2, "{out}");
    assert!(lines[0].starts_with(r#"{"type":"error""#));
    assert_eq!(lines[1], r#"{"type":"closed","reason":"stop"}"#);
}

#[test]
fn version_flag() {
    let out = Command::new(env!("CARGO_BIN_EXE_nexus-live")).arg("--version").output().unwrap();
    assert!(String::from_utf8_lossy(&out.stdout).starts_with("nexus-live "));
}
