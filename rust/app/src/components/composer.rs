//! Composer: draft input and Send. The draft is kept when a send fails; a send
//! is never retried automatically.

use nsh_base::{input, theme};
use nsh_core::prelude::*;
use serde_json::json;

use crate::store::Store;

pub fn composer(store: Store) -> impl IntoElement {
    let draft = signal(String::new());
    let sending = signal(false);

    let send = move || {
        let Some(id) = store.selected.get_untracked() else { return };
        let content = draft.get_untracked();
        if content.trim().is_empty() || sending.get_untracked() {
            return;
        }
        sending.set(true);
        spawn(async move {
            let cmd = json!({"type": "SessionEnqueue", "session": id, "content": content, "mode": "steer"});
            match store.command(cmd).await {
                Ok(_) => {
                    store.notice.set(String::new());
                    if store.selected.get_untracked().as_deref() == Some(id.as_str()) {
                        draft.set(String::new());
                    }
                }
                Err(e) => store.notice.set(format!("Send failed (draft kept): {e}")),
            }
            sending.set(false);
        });
    };

    view()
        .p_3()
        .gap_2()
        .border_t(px(1.0))
        .border_t_color(move || theme().border)
        .child(text(move || store.notice.get()).text_sm().text_color(move || theme().destructive))
        .child(
            view()
                .flex_row()
                .gap_2()
                .items_end()
                .child(
                    input()
                        .flex_1()
                        .multiline(true)
                        .placeholder("Message (Enter to send)")
                        .submit_behavior(SubmitBehavior::Submit)
                        .value(move || draft.get())
                        .on_change(move |s: &str| draft.set(s.to_owned()))
                        .on_submit(move |_: &str| send()),
                )
                .child(
                    view()
                        .px_4()
                        .py_2()
                        .rounded(move || theme().radius)
                        .bg(move || theme().primary)
                        .opacity(move || {
                            if store.selected.get().is_some() && !draft.with(|d| d.trim().is_empty()) { 1.0_f32 } else { 0.4_f32 }
                        })
                        .active(|s| s.opacity(0.6))
                        .on_press(move || send())
                        .child(text("Send").text_color(move || theme().primary_foreground)),
                ),
        )
}
