//! Bounded WebSocket transport for the native Manager audio bridge.
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tokio_tungstenite::tungstenite::Message;

const QUEUE_FRAMES: usize = 25;

pub enum Input {
    Audio(Vec<u8>),
    Mute(bool),
}

pub enum Output {
    Audio(Vec<u8>),
    Closed(String),
}

/// One socket to Core. Dropping the handle cancels the task and closes the socket.
pub struct LiveSocket {
    pub input: mpsc::Sender<Input>,
    pub output: mpsc::Receiver<Output>,
    task: JoinHandle<()>,
}

impl LiveSocket {
    pub async fn connect(url: String) -> Result<Self, String> {
        let (socket, _) = tokio::time::timeout(
            Duration::from_secs(10),
            tokio_tungstenite::connect_async(url),
        )
        .await
        .map_err(|_| "Core did not open the voice audio connection in time".to_owned())?
        .map_err(|_| "Could not open the voice audio connection to Core".to_owned())?;
        let (input, mut incoming) = mpsc::channel::<Input>(QUEUE_FRAMES);
        let (outgoing, output) = mpsc::channel::<Output>(QUEUE_FRAMES);
        let task = tokio::spawn(async move {
            let (mut tx, mut rx) = socket.split();
            let reason = loop {
                tokio::select! {
                    input = incoming.recv() => {
                        let Some(input) = input else { break None };
                        let message = match input {
                            Input::Audio(bytes) => Message::binary(bytes),
                            Input::Mute(muted) => Message::text(if muted { "mute" } else { "unmute" }),
                        };
                        if tx.send(message).await.is_err() { break Some("The voice connection to Core closed."); }
                    }
                    message = rx.next() => match message {
                        Some(Ok(Message::Binary(bytes))) => {
                            if outgoing.try_send(Output::Audio(bytes.to_vec())).is_err() && outgoing.is_closed() { break None; }
                        }
                        Some(Ok(Message::Close(_))) | None | Some(Err(_)) => break Some("The voice connection to Core closed."),
                        Some(Ok(_)) => {}
                    },
                }
            };
            if let Some(reason) = reason {
                let _ = outgoing.send(Output::Closed(reason.into())).await;
            }
            let _ = tx.close().await;
        });
        Ok(Self {
            input,
            output,
            task,
        })
    }
}

impl Drop for LiveSocket {
    fn drop(&mut self) {
        self.task.abort();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::net::TcpListener;

    #[tokio::test]
    async fn exchanges_pcm_and_mute_then_reports_close() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();
            assert_eq!(
                socket.next().await.unwrap().unwrap(),
                Message::binary(vec![1, 2, 3, 4])
            );
            assert_eq!(socket.next().await.unwrap().unwrap(), Message::text("mute"));
            socket.send(Message::binary(vec![5, 6])).await.unwrap();
            socket.close(None).await.unwrap();
        });
        let mut client = LiveSocket::connect(format!("ws://{address}/audio"))
            .await
            .unwrap();
        client
            .input
            .send(Input::Audio(vec![1, 2, 3, 4]))
            .await
            .unwrap();
        client.input.send(Input::Mute(true)).await.unwrap();
        match client.output.recv().await.unwrap() {
            Output::Audio(bytes) => assert_eq!(bytes, vec![5, 6]),
            _ => panic!("expected audio"),
        }
        assert!(matches!(
            client.output.recv().await,
            Some(Output::Closed(_))
        ));
        server.await.unwrap();
    }

    #[tokio::test]
    async fn connection_errors_do_not_echo_url_or_token() {
        let error = match LiveSocket::connect("ws://127.0.0.1:1/audio?token=secret".into()).await {
            Ok(_) => panic!("unexpected connection"),
            Err(error) => error,
        };
        assert!(!error.contains("secret"));
    }

    #[tokio::test]
    async fn dropping_the_handle_closes_the_connection() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();
            tokio::time::timeout(Duration::from_secs(2), socket.next()).await
        });
        let client = LiveSocket::connect(format!("ws://{address}/audio"))
            .await
            .unwrap();
        drop(client);
        assert!(server.await.unwrap().is_ok());
    }
}
