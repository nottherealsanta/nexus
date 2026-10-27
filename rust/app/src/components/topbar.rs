//! Top bar: title, workspace, connection state, phase and model.

use nsh_base::theme;
use nsh_core::prelude::*;

use crate::present::sessions::basename;
use crate::present::timeline::{model_of, phase_of};
use crate::store::{Conn, Store};

fn conn_text(c: &Conn) -> String {
    match c {
        Conn::Connecting => "connecting…".to_owned(),
        Conn::Live => String::new(),
        Conn::Reconnecting(m) => format!("reconnecting: {m}"),
        Conn::Offline(m) => m.clone(),
    }
}

pub fn topbar(store: Store) -> impl IntoElement {
    view()
        .flex_row()
        .items_center()
        .gap_3()
        .px_4()
        .py_2()
        .border_b(px(1.0))
        .border_b_color(move || theme().border)
        .child(text("Nexus").font_weight(FontWeight::SEMIBOLD))
        .child(
            text(move || {
                let b = store.workspace.with(|w| basename(w));
                if b.is_empty() { "-".to_owned() } else { b }
            })
            .text_sm()
            .text_color(move || theme().muted_foreground),
        )
        .child(view().flex_1())
        .child(text(move || store.conn.with(conn_text)).text_sm().text_color(move || theme().destructive))
        .child(text(move || store.view.with(phase_of)).text_sm())
        .child(text(move || store.view.with(model_of)).text_sm().text_color(move || theme().muted_foreground))
}
