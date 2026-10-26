//! Nexus NSH root: browser API transport and a deliberately small labelled client.
#[cfg(target_arch = "wasm32")]
use futures::StreamExt;
use nexus_client_core::{command, project_conversation, readable};
use nsh_base::theme;
use nsh_core::prelude::*;
#[cfg(not(target_arch = "wasm32"))]
use serde_json::Value;
use serde_json::json;
#[cfg(target_arch = "wasm32")]
use wasm_bindgen::{JsCast, closure::Closure};
#[cfg(target_arch = "wasm32")]
mod web;
#[cfg(target_arch = "wasm32")]
use web::{STREAM, api, base};
#[cfg(not(target_arch = "wasm32"))]
async fn api(path: &str, body: Option<Value>, _: &str) -> Result<Value, String> {
    nexus_client_core::native::request(path, body)
}
fn spawn_local(f: impl std::future::Future<Output = ()> + 'static) {
    let _ = nsh_core::element::ambient::spawn(f);
}
#[native_module("nexus/native")]
fn native(registry: &mut Registry) {
    registry.register::<App>();
}

#[component]
pub fn App() -> impl IntoElement {
    let csrf = signal(String::new());
    let session = signal(String::new());
    let active = signal(String::new());
    let generation = signal(0_u64);
    let draft = signal(String::new());
    let output = signal(String::from("Connecting…"));
    let sessions = signal(String::new());
    spawn_local(async move {
        #[cfg(target_arch = "wasm32")]
        let result = {
            let w = web_sys::window().unwrap();
            let hash = w.location().hash().unwrap_or_default();
            let ticket = hash
                .trim_start_matches('#')
                .strip_prefix("ticket=")
                .unwrap_or("");
            let result = if ticket.is_empty() {
                api("bootstrap", None, "").await
            } else {
                let _ = w.history().unwrap().replace_state_with_url(
                    &wasm_bindgen::JsValue::NULL,
                    "",
                    Some(&format!("{}/", base())),
                );
                api("ticket/redeem", Some(json!({"ticket":ticket})), "").await
            };
            result
        };
        #[cfg(not(target_arch = "wasm32"))]
        let result: Result<Value, String> = Ok(json!({}));
        match result {
            Ok(v) => {
                csrf.set(v["csrf"].as_str().unwrap_or("").into());
                match api("command", Some(command("SessionList", "", "")), &csrf.get()).await {
                    Ok(v) => sessions.set(readable(&v)),
                    Err(e) => sessions.set(e),
                };
                output.set("Enter a session ID to open, or a new ID to create.".into());
            }
            Err(e) => output.set(format!("Authentication failed: {e}. Relaunch nexus web.")),
        }
    });
    let open = move || {
        spawn_local(async move {
            let id = session.get();
            generation.update(|n| *n += 1);
            let request = generation.get();
            if id.is_empty()
                || id.len() > 80
                || !id
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_')
            {
                output.set("Invalid session ID".into());
                return;
            }
            match api(
                "command",
                Some(command("SessionOpen", &id, "")),
                &csrf.get(),
            )
            .await
            {
                Err(e) => output.set(e),
                Ok(result) => {
                    if generation.get() != request {
                        return;
                    }
                    let id = result["session"]["id"].as_str().unwrap_or(&id).to_owned();
                    active.set(id.clone());
                    match api(&format!("session-view?session={id}"), None, "").await {
                        Ok(v) => {
                            if generation.get() == request {
                                output.set(project_conversation(&v["view"]));
                            }
                        }
                        Err(e) => output.set(e),
                    }
                    #[cfg(target_arch = "wasm32")]
                    if let Ok(source) = web_sys::EventSource::new(&format!(
                        "{}/v1/web/session-events?session={id}",
                        base()
                    )) {
                        let (mut tx, mut rx) = futures::channel::mpsc::channel::<()>(1);
                        let cb = Closure::<dyn FnMut(web_sys::MessageEvent)>::new(move |_| {
                            let _ = tx.try_send(());
                        });
                        spawn_local(async move {
                            while rx.next().await.is_some() {
                                if active.get() != id || generation.get() != request {
                                    return;
                                }
                                match api(&format!("session-view?session={id}"), None, "").await {
                                    Ok(v) => {
                                        if generation.get() == request {
                                            output.set(project_conversation(&v["view"]));
                                        }
                                    }
                                    Err(e) => output.set(e),
                                }
                            }
                        });
                        let _ = source
                            .add_event_listener_with_callback("view", cb.as_ref().unchecked_ref());
                        STREAM.with(|slot| {
                            if let Some((old, _)) = slot.borrow_mut().take() {
                                old.close();
                            }
                            *slot.borrow_mut() = Some((source, cb));
                        });
                    }
                }
            }
        })
    };
    view()
        .flex_1()
        .bg(move || theme().background)
        .text_color(move || theme().foreground)
        .p_4()
        .gap_3()
        .child(text("▌ Nexus                                      NSH ▐").text_size(px(20.0)))
        .child(
            view()
                .flex_row()
                .flex_1()
                .gap_3()
                .child(
                    view()
                        .w(px(200.0))
                        .gap_3()
                        .child(text("SESSIONS"))
                        .child(scroll().flex_1().child(text(move || sessions.get())))
                        .child(
                            nsh_core::element::input()
                                .placeholder("Session ID")
                                .on_change(move |s: &str| session.set(s.into())),
                        )
                        .child(view().on_press(open).child(text("+ Open / create"))),
                )
                .child(
                    view()
                        .flex_1()
                        .gap_3()
                        .child(text(move || format!("CONTEXT  ·  {}", active.get())))
                        .child(scroll().flex_1().child(text(move || output.get())))
                        .child(
                            nsh_core::element::input()
                                .placeholder("Message")
                                .on_change(move |s: &str| draft.set(s.into())),
                        )
                        .child(
                            view()
                                .flex_row()
                                .gap_3()
                                .child(
                                    view()
                                        .on_press(move || {
                                            spawn_local(async move {
                                                if let Err(e) = api(
                                                    "command",
                                                    Some(command(
                                                        "SessionEnqueue",
                                                        &active.get(),
                                                        &draft.get(),
                                                    )),
                                                    &csrf.get(),
                                                )
                                                .await
                                                {
                                                    output.set(e);
                                                }
                                            })
                                        })
                                        .child(text("Send")),
                                )
                                .child(
                                    view()
                                        .on_press(move || {
                                            spawn_local(async move {
                                                if let Err(e) = api(
                                                    "command",
                                                    Some(command(
                                                        "SessionCancel",
                                                        &active.get(),
                                                        "",
                                                    )),
                                                    &csrf.get(),
                                                )
                                                .await
                                                {
                                                    output.set(e);
                                                }
                                            })
                                        })
                                        .child(text("Stop")),
                                ),
                        ),
                )
                .child(view().w(px(180.0)).child(text("DETAILS"))),
        )
}
