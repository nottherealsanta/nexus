//! Real Core PTY integration, using the same HTTP and VT adapters as the TUI.
use nexus_tui::{terminal_api::TerminalApi, terminal_screen::TerminalScreen, transport::Http};
use std::{
    io::{BufRead, BufReader},
    process::{Child, Command, Stdio},
    time::Duration,
};

struct Server(Child);
impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

#[tokio::test]
#[ignore = "requires local Python 3.14 and nexus-core/.venv dependencies"]
async fn real_core_terminal_accepts_input_and_renders_output() {
    let home = std::env::temp_dir().join(format!("nexus-terminal-test-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&home).unwrap();
    let script =
        std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/terminal_core_server.py");
    let python = std::env::var_os("NEXUS_TEST_PYTHON")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| {
            std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../nexus-core/.venv/bin/python")
        });
    let child = Command::new(python)
        .arg(script)
        .env("NEXUS10_HOME", &home)
        .env("SHELL", "/bin/sh")
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut server = Server(child);
    let mut line = String::new();
    BufReader::new(server.0.stdout.take().unwrap())
        .read_line(&mut line)
        .unwrap();
    let mut fields = line.split_whitespace();
    let url = fields.next().expect("Core did not start");
    let project = fields.next().unwrap();
    let workspace = fields.next().unwrap();
    let api = TerminalApi(Http::new(url, "test-secret").unwrap());
    let terminal = tokio::time::timeout(Duration::from_secs(10), async {
        loop {
            match api
                .create(project, workspace, None, "tui-test", 24, 80)
                .await
            {
                Ok(t) => break t,
                Err(_) => tokio::time::sleep(Duration::from_millis(50)).await,
            }
        }
    })
    .await
    .expect("Core did not become ready");
    let attachment = api.attach(&terminal.id, "tui-test").await.unwrap();
    let mut stream = api.stream(&terminal.id, &attachment, None).await.unwrap();
    api.control(&terminal.id, &attachment, true).await.unwrap();
    api.input(
        &terminal.id,
        &attachment,
        1,
        b"printf 'NEXUS_REAL_%s\\n' CORE_OK\r",
    )
    .await
    .unwrap();
    let mut screen = TerminalScreen::new(24, 80);
    tokio::time::timeout(Duration::from_secs(10), async {
        loop {
            let event = stream.next().await.unwrap().expect("terminal stream ended");
            screen
                .apply(&serde_json::from_str(&event.data).unwrap())
                .unwrap();
            if screen.screen().contents().contains("NEXUS_REAL_CORE_OK") {
                break;
            }
        }
    })
    .await
    .expect("shell output not rendered");
    api.close(&terminal.id).await.unwrap();
    drop(stream);
    drop(server);
    std::fs::remove_dir_all(home).unwrap();
}
